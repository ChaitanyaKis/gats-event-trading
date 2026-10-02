"""BSE corporate announcements.

Endpoint and field mapping verified against the live API on 2026-09-26:
50 rows per page, newest first. The API serves ONE DAY per query; a
multi-day range returns ``{}``. ``gats probe bse`` re-checks the mapping.

Past days come from a different row shape (verified 2026-10-02 on dates back
to 2018): no ``TotalPageCnt``, a few renamed fields, but ``Table1.ROWCNT``
still gives the day's total, so the page count is derived from it.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from gats.sources._util import clean_str, looks_like_html, pick, preview, to_int
from gats.sources.models import AnnouncementRecord, ParseResult, PayloadError
from gats.timeutil import parse_ist_datetime

SOURCE = "BSE"
KIND = "bse_ann"
PARSER_VERSION = "bse-ann-v1"
PAGE_SIZE = 50  # rows per page, verified 2026-09-26 and 2026-10-02

# Fields the mapping below understands; anything else is reported by `probe`.
KNOWN_FIELDS = frozenset(
    {
        "newsid",
        "scrip_cd",
        "slongname",
        "categoryname",
        "subcatname",
        "newssub",
        "headline",
        "attachmentname",
        "pdfflag",
        "news_dt",
        "dt_tm",
        "dissemdt",
        "news_submission_dt",
        "totalpagecnt",
    }
)


def request_params(start: date, end: date, page: int) -> dict[str, str]:
    """Query parameters. Pass ``start == end``: BSE rejects ranges with ``{}``."""
    return {
        "pageno": str(page),
        "strCat": "-1",
        "strPrevDate": start.strftime("%Y%m%d"),
        "strScrip": "",
        "strSearch": "P",
        "strToDate": end.strftime("%Y%m%d"),
        "strType": "C",
        "subcategory": "-1",
    }


def request_headers(referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Referer": referer,
        "Origin": referer.rstrip("/"),
    }


def attachment_candidates(url: str, live_base: str, hist_base: str) -> list[str]:
    """BSE serves attachments from a live folder that are later moved to a
    historical folder, so try both."""
    candidates = [url]
    if url.startswith(live_base):
        candidates.append(hist_base + url[len(live_base) :])
    elif url.startswith(hist_base):
        candidates.append(live_base + url[len(hist_base) :])
    return candidates


def _rows(payload: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if looks_like_html(payload):
        raise PayloadError(f"BSE returned HTML instead of JSON (blocked?): {preview(payload)}")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"BSE payload is not JSON ({exc}): {preview(payload)}") from exc
    if data == {}:
        # Verified 2026-09-26: BSE answers `{}` to queries it will not serve,
        # e.g. multi-day ranges, which is why callers query one day at a time.
        return [], {"empty_object": True}
    if not isinstance(data, dict) or not isinstance(data.get("Table"), list):
        raise PayloadError(f"BSE payload missing 'Table' list: {preview(payload)}")
    meta: dict[str, Any] = {}
    table1 = data.get("Table1")
    if isinstance(table1, list) and table1 and isinstance(table1[0], dict):
        meta["row_count"] = to_int(pick(table1[0], "ROWCNT"))
    return [row for row in data["Table"] if isinstance(row, dict)], meta


def parse_announcements(payload: bytes, *, attachment_base: str) -> ParseResult[AnnouncementRecord]:
    rows, meta = _rows(payload)
    result: ParseResult[AnnouncementRecord] = ParseResult(records=[], meta=meta)
    if meta.get("empty_object"):
        result.warnings.append("BSE returned {} (no data served for this query)")
    if rows:
        pages = to_int(pick(rows[0], "TotalPageCnt"))
        row_count = meta.get("row_count")
        if pages is None and row_count is not None:
            pages = max(1, -(-row_count // PAGE_SIZE))  # ceil division
        result.meta["total_pages"] = pages
    unknown: set[str] = set()

    for index, row in enumerate(rows):
        unknown.update(k.lower() for k in row if k.lower() not in KNOWN_FIELDS)
        news_id = clean_str(pick(row, "NEWSID"))
        if not news_id:
            result.warnings.append(f"row {index}: missing NEWSID, skipped")
            continue

        submitted = parse_ist_datetime(pick(row, "News_submission_dt"))
        disseminated = parse_ist_datetime(pick(row, "DissemDT"))
        fallback = parse_ist_datetime(pick(row, "NEWS_DT", "DT_TM"))
        event_ts = disseminated or fallback or submitted
        if event_ts is None:
            result.warnings.append(f"row {index} ({news_id}): no parseable timestamp")

        attachment = clean_str(pick(row, "ATTACHMENTNAME"))
        result.records.append(
            AnnouncementRecord(
                source=SOURCE,
                source_ann_id=news_id,
                symbol=None,
                scrip_code=clean_str(pick(row, "SCRIP_CD")),
                isin=None,
                company_name=clean_str(pick(row, "SLONGNAME")),
                category=clean_str(pick(row, "CATEGORYNAME")),
                subcategory=clean_str(pick(row, "SUBCATNAME")),
                subject=clean_str(pick(row, "NEWSSUB")),
                details=clean_str(pick(row, "HEADLINE")),
                attachment_url=attachment_base + attachment if attachment else None,
                exch_submitted_ts=submitted,
                exch_disseminated_ts=disseminated,
                event_ts=event_ts,
            )
        )
    result.meta["unknown_fields"] = sorted(unknown)
    return result
