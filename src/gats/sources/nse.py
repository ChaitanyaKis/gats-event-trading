"""NSE corporate announcements.

Endpoint and field mapping verified against the live API (2026-09-26,
re-probed 2026-10-02). NSE only serves this API to sessions holding cookies
from the homepage, so requests go through ``PoliteClient.get(...,
warmup_url=...)``.

Timestamps (evidence in docs/DATA_SOURCES.md, T1.3): ``an_dt`` is the
exchange receipt time and ``exchdisstime`` the dissemination time. On 2,410
real rows ``difference`` equalled ``exchdisstime - an_dt`` exactly (0-3 s),
and ``dt``/``sort_date`` are reformatted copies of ``an_dt``.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from typing import Any

from gats.sources._util import clean_str, looks_like_html, pick, preview
from gats.sources.models import AnnouncementRecord, ParseResult, PayloadError
from gats.timeutil import parse_ist_datetime

SOURCE = "NSE"
KIND = "nse_ann"
PARSER_VERSION = "nse-ann-v3"  # v3: attachment size

KNOWN_FIELDS = frozenset(
    {
        "seq_id",
        "symbol",
        "sm_isin",
        "sm_name",
        "desc",
        "attchmnttext",
        "attchmntfile",
        "an_dt",
        "sort_date",
        "exchdisstime",
        "smindustry",
        # Redundant with an_dt/exchdisstime (see module docstring); `difference`
        # is used only as a consistency check.
        "dt",
        "difference",
        # Display size of the attachment, e.g. "1.27 MB"; fileSize repeats it.
        "attfilesize",
        "filesize",
    }
)

_SIZE_RE = re.compile(r"^\s*([\d.]+)\s*(bytes|kb|mb|gb)\s*$", re.IGNORECASE)
_UNITS = {"bytes": 1, "kb": 1024, "mb": 1024**2, "gb": 1024**3}


def parse_display_size(raw: object) -> int | None:
    """NSE's ``attFileSize`` ("165.57 KB", "1.27 MB") in bytes.

    Verified 2026-10-03: for the same PDF on both exchanges, BSE's exact
    ``Fld_Attachsize`` divided by 1024 (or 1024^2) rounds to NSE's figure,
    e.g. 169,542 bytes vs "165.57 KB". "0 Bytes" means no usable size.
    """
    text = clean_str(raw)
    match = _SIZE_RE.match(text) if text else None
    if match is None:
        return None
    size = round(float(match.group(1)) * _UNITS[match.group(2).lower()])
    return size or None


def request_params(start: date, end: date) -> dict[str, str]:
    return {
        "index": "equities",
        "from_date": start.strftime("%d-%m-%Y"),
        "to_date": end.strftime("%d-%m-%Y"),
    }


def request_headers(referer: str) -> dict[str, str]:
    return {"Accept": "application/json, text/plain, */*", "Referer": referer}


def _rows(payload: bytes) -> list[dict[str, Any]]:
    if looks_like_html(payload):
        raise PayloadError(f"NSE returned HTML instead of JSON (blocked?): {preview(payload)}")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"NSE payload is not JSON ({exc}): {preview(payload)}") from exc
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        raise PayloadError(
            f"NSE payload is neither a list nor {{'data': [...]}}: {preview(payload)}"
        )
    return [row for row in data if isinstance(row, dict)]


def _parse_hms(raw: object) -> timedelta | None:
    """``HH:MM:SS`` (optionally negative) as a timedelta, or None."""
    text = clean_str(raw)
    if text is None:
        return None
    sign = -1 if text.startswith("-") else 1
    try:
        hours, minutes, seconds = (int(part) for part in text.lstrip("-").split(":"))
    except ValueError:
        return None
    return sign * timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _fallback_id(row: dict[str, Any]) -> str:
    key = "|".join(
        str(pick(row, name) or "") for name in ("symbol", "an_dt", "desc", "attchmntFile")
    )
    return "h:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def row_id(row: dict[str, Any]) -> tuple[str, bool]:
    """``(announcement id, is_fallback)`` for one raw row."""
    ann_id = clean_str(pick(row, "seq_id"))
    return (ann_id, False) if ann_id else (_fallback_id(row), True)


def subset_payload(payload: bytes, keep_ids: set[str]) -> bytes:
    """The original rows whose id is in ``keep_ids``, as a JSON list.

    NSE returns the whole day on every poll. Storing the full payload each
    time something new appears would keep thousands of duplicate rows a day,
    so live ingestion stores only the new rows, unmodified. The subset parses
    exactly like the full payload.
    """
    rows = [row for row in _rows(payload) if row_id(row)[0] in keep_ids]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_announcements(payload: bytes) -> ParseResult[AnnouncementRecord]:
    rows = _rows(payload)
    result: ParseResult[AnnouncementRecord] = ParseResult(records=[])
    unknown: set[str] = set()

    for index, row in enumerate(rows):
        unknown.update(k.lower() for k in row if k.lower() not in KNOWN_FIELDS)
        ann_id, is_fallback = row_id(row)
        if is_fallback:
            result.warnings.append(f"row {index}: missing seq_id, using content hash {ann_id}")

        disseminated = parse_ist_datetime(pick(row, "exchdisstime"))
        received = parse_ist_datetime(pick(row, "an_dt", "sort_date"))
        event_ts = disseminated or received
        if event_ts is None:
            result.warnings.append(f"row {index} ({ann_id}): no parseable timestamp")
        reported = _parse_hms(pick(row, "difference"))
        # The receipt-time mapping rests on this identity; flag drift.
        if (
            disseminated
            and received
            and reported is not None
            and (disseminated - received != reported)
        ):
            result.warnings.append(
                f"row {index} ({ann_id}): difference {reported} != "
                f"exchdisstime - an_dt {disseminated - received}"
            )

        result.records.append(
            AnnouncementRecord(
                source=SOURCE,
                source_ann_id=ann_id,
                symbol=clean_str(pick(row, "symbol")),
                scrip_code=None,
                isin=clean_str(pick(row, "sm_isin")),
                company_name=clean_str(pick(row, "sm_name")),
                category=clean_str(pick(row, "desc")),
                subcategory=None,
                subject=clean_str(pick(row, "desc")),
                details=clean_str(pick(row, "attchmntText")),
                attachment_url=clean_str(pick(row, "attchmntFile")),
                attachment_size=parse_display_size(pick(row, "attFileSize", "fileSize")),
                exch_submitted_ts=received,
                exch_disseminated_ts=disseminated,
                event_ts=event_ts,
            )
        )
    result.meta["unknown_fields"] = sorted(unknown)
    return result
