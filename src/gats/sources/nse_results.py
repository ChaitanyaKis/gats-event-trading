"""NSE financial results: which results a company filed, when, and its revenue.

Two regimes, both probed 2026-10-03 (see docs/DATA_SOURCES.md):

- **Legacy**: ``/api/corporates-financial-results?index=equities&symbol=X
  &period=Quarterly`` lists quarters from 2005 up to October-December 2024.
  Rows carry ``fromDate``/``toDate``, ``consolidated``, ``audited``,
  ``exchdisstime`` (dissemination; missing on the oldest rows) and an XBRL
  link (BSE taxonomy ``in-bse-fin``).
- **Integrated filing** (SEBI's regime, quarters ending March 2025 onward):
  ``/api/integrated-filing-results?index=equities&symbol=X&period_ended=all
  &type=Integrated Filing- Financials`` returns ``{data, page, size,
  totalCount}`` with ``qe_Date``, ``broadcast_Date``, ``creation_Date`` and
  an XBRL link (SEBI taxonomy ``in-capmkt``).

Both XBRL formats state the quarter's revenue as ``RevenueFromOperations``
in context ``OneD``, in full rupees. The context *dates* are not reliable
for telling the quarter from the year to date: in the legacy sample,
``FourD`` holds nine months under the quarter's dates. So the context id is
used, and the period is cross-checked against the index row by the caller.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from gats.sources._util import clean_str, looks_like_html, to_float
from gats.sources.models import ParseResult, PayloadError
from gats.timeutil import parse_date, parse_ist_datetime

SOURCE = "NSE"
LEGACY_KIND = "nse_results_index"
INTEGRATED_KIND = "nse_integrated_index"
XBRL_KIND = "nse_results_xbrl"
PARSER_VERSION = "nse-results-v1"
QUARTER_CONTEXT = "OneD"


@dataclass(frozen=True, slots=True)
class ResultFiling:
    """One results filing as the exchange lists it."""

    symbol: str
    isin: str | None
    company: str | None
    period_start: date | None  # the integrated index gives only the end
    period_end: date
    consolidated: bool
    audited: bool
    disseminated_ts: datetime | None  # UTC; None on the oldest legacy rows
    xbrl_url: str | None
    seq: str | None
    regime: str  # legacy | integrated
    revised: bool = False


@dataclass(frozen=True, slots=True)
class ResultFacts:
    """What a results XBRL states for its reporting quarter."""

    symbol: str | None
    isin: str | None
    period_start: date | None
    period_end: date | None
    consolidated: bool | None
    revenue: float | None  # RevenueFromOperations, rupees


def legacy_params(symbol: str) -> dict[str, str]:
    return {"index": "equities", "symbol": symbol, "period": "Quarterly"}


def integrated_params(symbol: str) -> dict[str, str]:
    return {
        "index": "equities",
        "symbol": symbol,
        "period_ended": "all",
        "type": "Integrated Filing- Financials",
    }


def _rows(payload: bytes, what: str) -> Any:
    if looks_like_html(payload):
        raise PayloadError(f"{what}: got HTML instead of JSON")
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PayloadError(f"{what}: not JSON ({exc})") from exc


def _link(value: Any) -> str | None:
    """The XBRL URL, or None for the placeholders of rows without a file
    (``.../xbrl/-``, ``.../null``)."""
    url = clean_str(value)
    if url is None or url.rsplit("/", 1)[-1].lower() in ("-", "null", ""):
        return None
    return url


def _is(value: Any, word: str) -> bool:
    return (clean_str(value) or "").lower() == word


def parse_legacy_index(payload: bytes) -> ParseResult[ResultFiling]:
    rows = _rows(payload, "financial results")
    if not isinstance(rows, list):
        raise PayloadError(f"financial results: expected a list, got {type(rows).__name__}")
    result: ParseResult[ResultFiling] = ParseResult(records=[])
    for index, row in enumerate(rows):
        symbol, end = clean_str(row.get("symbol")), parse_date(row.get("toDate"))
        if symbol is None or end is None:
            result.warnings.append(f"row {index}: no symbol or period end")
            continue
        if not _is(row.get("cumulative"), "non-cumulative"):
            continue  # year-to-date statements are not quarters
        result.records.append(
            ResultFiling(
                symbol=symbol,
                isin=clean_str(row.get("isin")),
                company=clean_str(row.get("companyName")),
                period_start=parse_date(row.get("fromDate")),
                period_end=end,
                consolidated=_is(row.get("consolidated"), "consolidated"),
                audited=_is(row.get("audited"), "audited"),
                disseminated_ts=parse_ist_datetime(row.get("exchdisstime")),
                xbrl_url=_link(row.get("xbrl")),
                seq=clean_str(row.get("seqNumber")),
                regime="legacy",
            )
        )
    result.meta = {"rows": len(rows)}
    return result


def parse_integrated_index(payload: bytes) -> ParseResult[ResultFiling]:
    body = _rows(payload, "integrated filing")
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise PayloadError("integrated filing: no data list")
    result: ParseResult[ResultFiling] = ParseResult(records=[])
    for index, row in enumerate(rows):
        symbol, end = clean_str(row.get("symbol")), parse_date(row.get("qe_Date"))
        if symbol is None or end is None:
            result.warnings.append(f"row {index}: no symbol or quarter end")
            continue
        # Two clocks a second or so apart; the later one is when it was public.
        stamps = [
            t
            for t in (
                parse_ist_datetime(row.get("broadcast_Date")),
                parse_ist_datetime(row.get("creation_Date")),
            )
            if t is not None
        ]
        result.records.append(
            ResultFiling(
                symbol=symbol,
                isin=None,
                company=clean_str(row.get("cmName")),
                period_start=None,
                period_end=end,
                consolidated=_is(row.get("consolidated"), "consolidated"),
                audited=_is(row.get("audited"), "audited"),
                disseminated_ts=max(stamps) if stamps else None,
                xbrl_url=_link(row.get("xbrl")),
                seq=clean_str(row.get("seq_Id")),
                regime="integrated",
                revised=not _is(row.get("type_Sub"), "original"),
            )
        )
    result.meta = {
        "rows": len(rows),
        "total": body.get("totalCount") if isinstance(body, dict) else None,
    }
    return result


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_results_xbrl(payload: bytes) -> ResultFacts:
    """The reporting quarter's revenue and identity from a results XBRL."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise PayloadError(f"results XBRL: not XML ({exc})") from exc
    start = end = None
    for context in root.iter():
        if _local(context.tag) == "context" and context.get("id") == QUARTER_CONTEXT:
            for node in context.iter():
                if _local(node.tag) == "startDate":
                    start = parse_date(node.text)
                elif _local(node.tag) == "endDate":
                    end = parse_date(node.text)
    facts: dict[str, str] = {}
    for node in root.iter():
        name = _local(node.tag)
        if node.get("contextRef") == QUARTER_CONTEXT and name not in facts and node.text:
            facts[name] = node.text.strip()
    nature = facts.get("NatureOfReportStandaloneConsolidated", "").lower()
    return ResultFacts(
        symbol=clean_str(facts.get("Symbol")),
        isin=clean_str(facts.get("ISIN")),
        period_start=parse_date(facts.get("DateOfStartOfReportingPeriod")) or start,
        period_end=parse_date(facts.get("DateOfEndOfReportingPeriod")) or end,
        consolidated=None if not nature else nature == "consolidated",
        revenue=to_float(facts.get("RevenueFromOperations")),
    )
