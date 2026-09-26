"""BSE corporate announcements.

ENDPOINT AND FIELD NAMES ARE UNVERIFIED: they are based on the JSON API that
powers bseindia.com's announcements page, which the build environment could
not reach. Run ``gats probe bse`` first; it saves the raw payload and reports
unknown/missing fields so this mapping can be corrected. Because raw payloads
are always stored, ``gats reparse`` can then rebuild the tables.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from gats.sources._util import clean_str, looks_like_html, pick, to_int
from gats.sources.models import AnnouncementRecord, ParseResult, PayloadError
from gats.timeutil import parse_ist_datetime

SOURCE = "BSE"
KIND = "bse_ann"
PARSER_VERSION = "bse-ann-v1"

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
        raise PayloadError("BSE returned HTML instead of JSON (blocked or endpoint changed)")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"BSE payload is not JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("Table"), list):
        raise PayloadError("BSE payload missing 'Table' list")
    meta: dict[str, Any] = {}
    table1 = data.get("Table1")
    if isinstance(table1, list) and table1 and isinstance(table1[0], dict):
        meta["row_count"] = to_int(pick(table1[0], "ROWCNT"))
    return [row for row in data["Table"] if isinstance(row, dict)], meta


def parse_announcements(payload: bytes, *, attachment_base: str) -> ParseResult[AnnouncementRecord]:
    rows, meta = _rows(payload)
    result: ParseResult[AnnouncementRecord] = ParseResult(records=[], meta=meta)
    if rows:
        result.meta["total_pages"] = to_int(pick(rows[0], "TotalPageCnt"))
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
