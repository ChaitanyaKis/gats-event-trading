"""NSE surveillance lists: ASM (long and short term) and GSM.

Verified 2026-10-03 (both need the homepage cookies):

- ``www.nseindia.com/api/reportASM``: ``{"longterm": {"data": [...]},
  "shortterm": {"data": [...]}}``, rows with ``symbol, isin, companyName,
  asmSurvIndicator`` ("Stage I"), ``survCode`` ("LTASM - I (13)"),
  ``survDesc``, ``asmTime`` (``DD-Mon-YYYY``), ``series`` (null), ``srno``.
  126 long-term and 68 short-term entries on 2026-10-03.
- ``www.nseindia.com/api/reportGSM``: a list with ``symbol, isin,
  companyName, gsmStage`` ("0", "LXII", ...: a combined code),
  ``survCode`` ("IBC I & GSM 0 (58)"), ``survDesc``, ``gsmTime``. 77 entries.

Both publish only today's lists; history starts from the first recording
(versioned by ``gats.refdata.versions``). Trade-for-trade status is not in
these lists: it is the series (BE/BZ, and ST/SZ for SME shares, per NSE's
"Legend of series" page, checked 2026-10-03), already recorded daily in the
price-band file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from gats.sources._util import clean_str, looks_like_html, pick, preview
from gats.sources.models import ParseResult, PayloadError
from gats.timeutil import parse_date, parse_ist_datetime, to_ist

SOURCE = "NSE"
ASM_KIND = "nse_asm"
GSM_KIND = "nse_gsm"
PARSER_VERSION = "nse-surv-v1"

# Series that trade for trade (no intraday, delivery only), from NSE's legend.
TRADE_FOR_TRADE_SERIES = frozenset({"BE", "BZ", "ST", "SZ"})


@dataclass(frozen=True, slots=True)
class SurveillanceRecord:
    list_name: str  # LTASM | STASM | GSM
    symbol: str
    isin: str | None
    company: str | None
    stage: str | None
    surv_code: str | None
    surv_desc: str | None
    since: date | None  # the date NSE shows for the entry

    @property
    def key(self) -> str:
        return f"{self.list_name}:{self.symbol}"


def _load(payload: bytes, what: str) -> Any:
    if looks_like_html(payload):
        raise PayloadError(f"NSE {what}: got HTML instead of JSON: {preview(payload)}")
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise PayloadError(f"NSE {what}: not JSON ({exc}): {preview(payload)}") from exc


def _record(
    list_name: str, row: dict[str, Any], stage_field: str, time_field: str
) -> SurveillanceRecord | None:
    symbol = clean_str(pick(row, "symbol"))
    if not symbol:
        return None
    raw_time = pick(row, time_field)
    when = parse_ist_datetime(raw_time)
    since = to_ist(when).date() if when else parse_date(raw_time)
    return SurveillanceRecord(
        list_name=list_name,
        symbol=symbol.upper(),
        isin=clean_str(pick(row, "isin")),
        company=clean_str(pick(row, "companyName")),
        stage=clean_str(pick(row, stage_field)),
        surv_code=clean_str(pick(row, "survCode")),
        surv_desc=clean_str(pick(row, "survDesc")),
        since=since,
    )


def parse_asm(payload: bytes) -> ParseResult[SurveillanceRecord]:
    data = _load(payload, "ASM report")
    if not isinstance(data, dict) or not {"longterm", "shortterm"} <= set(data):
        raise PayloadError(f"NSE ASM report: no longterm/shortterm sections: {preview(payload)}")
    result: ParseResult[SurveillanceRecord] = ParseResult(records=[])
    for section, list_name in (("longterm", "LTASM"), ("shortterm", "STASM")):
        rows = data[section].get("data") if isinstance(data[section], dict) else None
        if not isinstance(rows, list):
            raise PayloadError(f"NSE ASM report: {section} has no data list")
        for index, row in enumerate(rows):
            record = (
                _record(list_name, row, "asmSurvIndicator", "asmTime")
                if isinstance(row, dict)
                else None
            )
            if record is None:
                result.warnings.append(f"{section} row {index}: no symbol")
                continue
            result.records.append(record)
    return result


def parse_gsm(payload: bytes) -> ParseResult[SurveillanceRecord]:
    data = _load(payload, "GSM report")
    if not isinstance(data, list):
        raise PayloadError(f"NSE GSM report: not a JSON list: {preview(payload)}")
    result: ParseResult[SurveillanceRecord] = ParseResult(records=[])
    for index, row in enumerate(data):
        record = _record("GSM", row, "gsmStage", "gsmTime") if isinstance(row, dict) else None
        if record is None:
            result.warnings.append(f"row {index}: no symbol")
            continue
        result.records.append(record)
    return result


def request_headers(referer: str) -> dict[str, str]:
    return {"Accept": "application/json, text/plain, */*", "Referer": referer}
