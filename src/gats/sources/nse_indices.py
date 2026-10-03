"""NSE index closes: ``nsearchives.nseindia.com/content/indices/ind_close_all_DDMMYYYY.csv``.

Verified 2026-10-03: one row per index (167 on 2026-10-01, 79 on 2019-10-01),
header ``Index Name, Index Date (DD-MM-YYYY), Open/High/Low/Closing Index
Value, Points Change, Change(%), Volume, Turnover (Rs. Cr.), P/E, P/B, Div
Yield``. Weekends and holidays 404 (unlike ``sec_bhavdata_full``); special
sessions (2026-02-01, 2025-10-21) have files. Files exist back to at least
2012, but index names change over time (``S&P CNX Nifty`` 2012, ``CNX
Nifty`` 2013, ``Nifty 50`` by 2019), so names are stored as published.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date

from gats.sources._util import clean_str, looks_like_html, read_csv, to_float, to_int
from gats.sources.models import ParseResult, PayloadError
from gats.timeutil import parse_date

SOURCE = "NSE"
KIND = "nse_indices"
PARSER_VERSION = "nse-indices-v1"

_REQUIRED = frozenset({"INDEX NAME", "INDEX DATE", "CLOSING INDEX VALUE"})


@dataclass(frozen=True, slots=True)
class IndexRecord:
    trade_date: date
    index_name: str
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    points_change: float | None
    pct_change: float | None
    volume: int | None
    turnover_cr: float | None
    pe: float | None
    pb: float | None
    div_yield: float | None


def url(template: str, day: date) -> str:
    return template.format(ddmmyyyy=day.strftime("%d%m%Y"))


def parse_index_close(payload: bytes, *, trade_date: date) -> ParseResult[IndexRecord]:
    """Parse one day's file. Like the EOD parser, rows dated another day are
    dropped and ``meta["dates_seen"]`` reports the dates found."""
    if looks_like_html(payload):
        raise PayloadError("index close file: got HTML instead of CSV")
    header, rows = read_csv(payload)
    missing = _REQUIRED - set(header)
    if missing:
        raise PayloadError(f"index close file: missing columns {sorted(missing)}; header={header}")
    result: ParseResult[IndexRecord] = ParseResult(records=[])
    other_days: Counter[date] = Counter()
    seen: set[date] = set()
    for index, row in enumerate(rows):
        name = clean_str(row.get("INDEX NAME"))
        if not name:
            result.warnings.append(f"row {index}: missing index name")
            continue
        row_date = parse_date(row.get("INDEX DATE"))
        if row_date is not None:
            seen.add(row_date)
            if row_date != trade_date:
                other_days[row_date] += 1
                continue
        result.records.append(
            IndexRecord(
                trade_date=trade_date,
                index_name=name,
                open=to_float(row.get("OPEN INDEX VALUE")),
                high=to_float(row.get("HIGH INDEX VALUE")),
                low=to_float(row.get("LOW INDEX VALUE")),
                close=to_float(row.get("CLOSING INDEX VALUE")),
                points_change=to_float(row.get("POINTS CHANGE")),
                pct_change=to_float(row.get("CHANGE(%)")),
                volume=to_int(row.get("VOLUME")),
                turnover_cr=to_float(row.get("TURNOVER (RS. CR.)")),
                pe=to_float(row.get("P/E")),
                pb=to_float(row.get("P/B")),
                div_yield=to_float(row.get("DIV YIELD")),
            )
        )
    for other, count in sorted(other_days.items()):
        result.warnings.append(f"{count} rows dated {other}, not {trade_date}: dropped")
    result.meta["dates_seen"] = sorted(d.isoformat() for d in seen)
    return result
