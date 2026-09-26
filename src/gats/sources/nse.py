"""NSE corporate announcements.

ENDPOINT AND FIELD NAMES ARE UNVERIFIED: they are based on the JSON API that
powers nseindia.com's corporate-filings page, which the build environment
could not reach. NSE only serves this API to sessions holding cookies from the
homepage, so requests go through ``PoliteClient.get(..., warmup_url=...)``.
Run ``gats probe nse`` before relying on it.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any

from gats.sources._util import clean_str, looks_like_html, pick
from gats.sources.models import AnnouncementRecord, ParseResult, PayloadError
from gats.timeutil import parse_ist_datetime

SOURCE = "NSE"
KIND = "nse_ann"
PARSER_VERSION = "nse-ann-v1"

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
    }
)


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
        raise PayloadError("NSE returned HTML instead of JSON (blocked or cookies missing)")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"NSE payload is not JSON: {exc}") from exc
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        raise PayloadError("NSE payload is neither a list nor {'data': [...]}")
    return [row for row in data if isinstance(row, dict)]


def _fallback_id(row: dict[str, Any]) -> str:
    key = "|".join(
        str(pick(row, name) or "") for name in ("symbol", "an_dt", "desc", "attchmntFile")
    )
    return "h:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:20]


def parse_announcements(payload: bytes) -> ParseResult[AnnouncementRecord]:
    rows = _rows(payload)
    result: ParseResult[AnnouncementRecord] = ParseResult(records=[])
    unknown: set[str] = set()

    for index, row in enumerate(rows):
        unknown.update(k.lower() for k in row if k.lower() not in KNOWN_FIELDS)
        ann_id = clean_str(pick(row, "seq_id"))
        if not ann_id:
            ann_id = _fallback_id(row)
            result.warnings.append(f"row {index}: missing seq_id, using content hash {ann_id}")

        disseminated = parse_ist_datetime(pick(row, "exchdisstime"))
        announced = parse_ist_datetime(pick(row, "an_dt", "sort_date"))
        event_ts = disseminated or announced
        if event_ts is None:
            result.warnings.append(f"row {index} ({ann_id}): no parseable timestamp")

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
                # NSE's receipt-time field is not identified yet; `probe` will show it.
                exch_submitted_ts=None,
                exch_disseminated_ts=disseminated,
                event_ts=event_ts,
            )
        )
    result.meta["unknown_fields"] = sorted(unknown)
    return result
