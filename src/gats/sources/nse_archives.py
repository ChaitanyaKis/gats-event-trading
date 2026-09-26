"""NSE archive files: daily prices with delivery data, price bands and the
equity instrument list.

URLS AND COLUMN NAMES ARE UNVERIFIED from the build environment; confirm with
``gats probe eod|bands|instruments``. Parsers validate the header and fail
loudly when required columns are missing, instead of writing bad rows.
"""

from __future__ import annotations

from datetime import date

from gats.sources._util import clean_str, looks_like_html, read_csv, to_float, to_int
from gats.sources.models import (
    BandRecord,
    EodRecord,
    InstrumentRecord,
    ParseResult,
    PayloadError,
)
from gats.timeutil import parse_date

SOURCE = "NSE"

EOD_KIND = "nse_eod"
EOD_PARSER_VERSION = "nse-eod-v1"
BANDS_KIND = "nse_bands"
BANDS_PARSER_VERSION = "nse-bands-v1"
INSTRUMENTS_KIND = "nse_instruments"
INSTRUMENTS_PARSER_VERSION = "nse-instruments-v1"

_EOD_REQUIRED = frozenset({"SYMBOL", "SERIES", "CLOSE_PRICE"})
_BANDS_REQUIRED = frozenset({"SYMBOL", "SERIES", "BAND"})
_INSTRUMENTS_REQUIRED = frozenset({"SYMBOL", "SERIES", "ISIN NUMBER"})


def eod_url(template: str, day: date) -> str:
    return template.format(ddmmyyyy=day.strftime("%d%m%Y"))


def _checked_csv(payload: bytes, required: frozenset[str], what: str) -> list[dict[str, str]]:
    if looks_like_html(payload):
        raise PayloadError(f"{what}: got HTML instead of CSV (blocked or URL changed)")
    header, rows = read_csv(payload)
    missing = required - set(header)
    if missing:
        raise PayloadError(f"{what}: missing columns {sorted(missing)}; header={header}")
    return rows


def parse_eod(payload: bytes, *, trade_date: date) -> ParseResult[EodRecord]:
    """Parse ``sec_bhavdata_full_DDMMYYYY.csv``.

    The requested date is authoritative; rows whose own DATE1 disagrees are
    dropped with a warning (a mismatch means NSE served a different file).
    """
    rows = _checked_csv(payload, _EOD_REQUIRED, "EOD file")
    result: ParseResult[EodRecord] = ParseResult(records=[])
    for index, row in enumerate(rows):
        symbol, series = clean_str(row.get("SYMBOL")), clean_str(row.get("SERIES"))
        if not symbol or not series:
            result.warnings.append(f"row {index}: missing symbol/series")
            continue
        row_date = parse_date(row.get("DATE1"))
        if row_date is not None and row_date != trade_date:
            result.warnings.append(f"row {index} {symbol}: DATE1 {row_date} != {trade_date}")
            continue
        result.records.append(
            EodRecord(
                trade_date=trade_date,
                symbol=symbol,
                series=series,
                prev_close=to_float(row.get("PREV_CLOSE")),
                open=to_float(row.get("OPEN_PRICE")),
                high=to_float(row.get("HIGH_PRICE")),
                low=to_float(row.get("LOW_PRICE")),
                last=to_float(row.get("LAST_PRICE")),
                close=to_float(row.get("CLOSE_PRICE")),
                avg_price=to_float(row.get("AVG_PRICE")),
                volume=to_int(row.get("TTL_TRD_QNTY")),
                turnover_lacs=to_float(row.get("TURNOVER_LACS")),
                num_trades=to_int(row.get("NO_OF_TRADES")),
                deliv_qty=to_int(row.get("DELIV_QTY")),
                deliv_pct=to_float(row.get("DELIV_PER")),
            )
        )
    return result


def parse_bands(payload: bytes) -> ParseResult[BandRecord]:
    rows = _checked_csv(payload, _BANDS_REQUIRED, "price band file")
    result: ParseResult[BandRecord] = ParseResult(records=[])
    for index, row in enumerate(rows):
        symbol, series = clean_str(row.get("SYMBOL")), clean_str(row.get("SERIES"))
        if not symbol or not series:
            result.warnings.append(f"row {index}: missing symbol/series")
            continue
        result.records.append(
            BandRecord(
                symbol=symbol,
                series=series,
                band=clean_str(row.get("BAND")),
                remarks=clean_str(row.get("REMARKS")),
            )
        )
    return result


def parse_instruments(payload: bytes) -> ParseResult[InstrumentRecord]:
    rows = _checked_csv(payload, _INSTRUMENTS_REQUIRED, "instrument list")
    result: ParseResult[InstrumentRecord] = ParseResult(records=[])
    for index, row in enumerate(rows):
        symbol, series = clean_str(row.get("SYMBOL")), clean_str(row.get("SERIES"))
        if not symbol or not series:
            result.warnings.append(f"row {index}: missing symbol/series")
            continue
        result.records.append(
            InstrumentRecord(
                symbol=symbol,
                series=series,
                isin=clean_str(row.get("ISIN NUMBER")),
                name=clean_str(row.get("NAME OF COMPANY")),
                listing_date=parse_date(row.get("DATE OF LISTING")),
                face_value=to_float(row.get("FACE VALUE")),
                market_lot=to_int(row.get("MARKET LOT")),
            )
        )
    return result
