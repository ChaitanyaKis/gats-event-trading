"""NSE trading holidays: ``www.nseindia.com/api/holiday-master?type=trading``.

Verified 2026-10-03: JSON object keyed by segment (``CM`` = equity cash
market, also ``FO``, ``CD``, ``COM``, ...); each entry has ``tradingDate``
(``DD-Mon-YYYY``), ``weekDay``, ``description``, ``Sr_no`` and
``morning_session``/``evening_session`` (null on every 2026 row, even the
Diwali entry marked ``*`` for a Muhurat session). Only the current calendar
year is served, so history comes from the EOD files instead
(``gats.refdata.calendar``). Weekend holidays are listed too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from gats.sources._util import clean_str, looks_like_html, pick, preview
from gats.sources.models import ParseResult, PayloadError
from gats.timeutil import parse_date

SOURCE = "NSE"
KIND = "nse_holidays"
PARSER_VERSION = "nse-holidays-v1"


@dataclass(frozen=True, slots=True)
class HolidayRecord:
    segment: str
    holiday_date: date
    description: str | None


def request_params() -> dict[str, str]:
    return {"type": "trading"}


def request_headers(referer: str) -> dict[str, str]:
    return {"Accept": "application/json, text/plain, */*", "Referer": referer}


def parse_holidays(payload: bytes) -> ParseResult[HolidayRecord]:
    if looks_like_html(payload):
        raise PayloadError(f"NSE holidays: got HTML instead of JSON: {preview(payload)}")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"NSE holidays: not JSON ({exc}): {preview(payload)}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("CM"), list):
        raise PayloadError(f"NSE holidays: no 'CM' segment list: {preview(payload)}")
    result: ParseResult[HolidayRecord] = ParseResult(records=[])
    for segment, rows in sorted(data.items()):
        if not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                continue
            day = parse_date(pick(row, "tradingDate"))
            if day is None:
                result.warnings.append(f"{segment} row {index}: unparseable tradingDate")
                continue
            result.records.append(
                HolidayRecord(str(segment), day, clean_str(pick(row, "description")))
            )
    return result
