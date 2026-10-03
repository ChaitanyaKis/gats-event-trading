"""Upstox market data (M5): the instrument file and historical candles.

**Instrument file** (verified 2026-10-03):
``assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz`` is a
gzipped JSON list (74,201 rows) refreshed daily around 06:00 IST, with no
login. Equity keys are ISINs (``NSE_EQ|INE002A01018``), so they join the
ISIN-based security master directly and survive symbol changes (ZOMATO ->
ETERNAL, TATAMOTORS -> TMPV). Index keys are names (``NSE_INDEX|Nifty
50``). ``tick_size`` looks like paise (RELIANCE 10.0), which is not
verified, so it is stored as published and not used.

**Candles** (documented, NOT yet probed: needs the Analytics Token, a HUMAN
step): ``GET https://api.upstox.com/v3/historical-candle/{key}/{unit}/
{interval}/{to_date}/{from_date}`` with ``Authorization: Bearer``. The
reply is ``{"status": "success", "data": {"candles": [[timestamp, open,
high, low, close, volume, open_interest], ...]}}`` with IST timestamps
(``2025-01-01T09:15:00+05:30``). One-minute data starts January 2022, at
most one month per request.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import quote

from gats.sources.models import ParseResult, PayloadError
from gats.timeutil import to_utc

SOURCE = "UPSTOX"
INSTRUMENTS_KIND = "upstox_instruments"
CANDLES_KIND = "upstox_candles"
PARSER_VERSION = "upstox-v1"
SEGMENTS = frozenset({"NSE_EQ", "NSE_INDEX"})  # cash equities and indices only


@dataclass(frozen=True, slots=True)
class UpstoxInstrument:
    instrument_key: str
    segment: str
    instrument_type: str | None  # series for equities (EQ, BE, SM, ...); INDEX
    trading_symbol: str | None
    name: str | None
    isin: str | None
    exchange_token: str | None
    lot_size: int | None
    tick_size: float | None  # as published (apparently paise)


@dataclass(frozen=True, slots=True)
class Bar:
    instrument_key: str
    ts: datetime  # bar start, UTC
    open: float
    high: float
    low: float
    close: float
    volume: int
    open_interest: int


def equity_key(isin: str) -> str:
    return f"NSE_EQ|{isin}"


def candles_url(
    base: str, instrument_key: str, unit: str, interval: int, to_date: date, from_date: date
) -> str:
    """The V3 candle URL; the key's ``|`` must be escaped (``%7C``)."""
    return (
        f"{base.rstrip('/')}/v3/historical-candle/{quote(instrument_key, safe='')}/"
        f"{unit}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
    )


def _load_json(payload: bytes, what: str) -> Any:
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except (OSError, EOFError) as exc:
            raise PayloadError(f"{what}: bad gzip ({exc})") from exc
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PayloadError(f"{what}: not JSON ({exc}); first bytes {payload[:80]!r}") from exc


def _opt_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _opt_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def parse_instruments(payload: bytes) -> ParseResult[UpstoxInstrument]:
    """NSE cash equities and indices from the instrument file (gzipped or not)."""
    rows = _load_json(payload, "instrument file")
    if not isinstance(rows, list):
        raise PayloadError(f"instrument file: expected a list, got {type(rows).__name__}")
    result: ParseResult[UpstoxInstrument] = ParseResult(records=[])
    skipped = 0
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or row.get("segment") not in SEGMENTS:
            skipped += 1
            continue
        key = row.get("instrument_key")
        if not isinstance(key, str) or "|" not in key:
            result.warnings.append(f"row {index}: bad instrument_key {key!r}")
            continue
        result.records.append(
            UpstoxInstrument(
                instrument_key=key,
                segment=row["segment"],
                instrument_type=row.get("instrument_type"),
                trading_symbol=row.get("trading_symbol"),
                name=row.get("name"),
                isin=row.get("isin"),
                exchange_token=str(row["exchange_token"]) if row.get("exchange_token") else None,
                lot_size=_opt_int(row.get("lot_size")),
                tick_size=_opt_float(row.get("tick_size")),
            )
        )
    result.meta = {"rows": len(rows), "kept": len(result.records), "other_segments": skipped}
    return result


def parse_candles(payload: bytes, *, instrument_key: str) -> ParseResult[Bar]:
    """Bars from a candle reply, oldest first whatever order the reply uses."""
    body = _load_json(payload, "candles")
    if not isinstance(body, dict):
        raise PayloadError(f"candles: expected an object, got {type(body).__name__}")
    if body.get("status") != "success":
        raise PayloadError(
            f"candles: status {body.get('status')!r}; errors {body.get('errors')!r}"[:300]
        )
    data = body.get("data")
    candles = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(candles, list):
        raise PayloadError("candles: no data.candles list")
    result: ParseResult[Bar] = ParseResult(records=[])
    for index, candle in enumerate(candles):
        if not isinstance(candle, list) or len(candle) < 6:
            result.warnings.append(f"candle {index}: unexpected shape {candle!r}"[:200])
            continue
        try:
            stamp = datetime.fromisoformat(str(candle[0]))
            if stamp.tzinfo is None:
                raise ValueError("timestamp without an offset")
            prices = [float(x) for x in candle[1:5]]
            volume = int(candle[5])
            open_interest = int(candle[6]) if len(candle) > 6 and candle[6] is not None else 0
        except (TypeError, ValueError) as exc:
            result.warnings.append(f"candle {index}: {exc}"[:200])
            continue
        o, h, low, c = prices
        if not (low <= min(o, c) and max(o, c) <= h and low > 0):
            result.warnings.append(f"candle {index}: inconsistent OHLC {candle!r}"[:200])
            continue
        result.records.append(
            Bar(instrument_key, to_utc(stamp), o, h, low, c, volume, open_interest)
        )
    result.records.sort(key=lambda bar: bar.ts)
    result.meta = {"candles": len(candles)}
    return result


CHARGES_KIND = "upstox_charges"
# The broker's names for each charge -> the cost model's (gats.backtest.costs).
CHARGE_FIELDS = {
    "brokerage": "brokerage",
    "taxes.stt": "stt",
    "taxes.stamp_duty": "stamp_duty",
    "taxes.gst": "gst",
    "other_charges.transaction": "exchange_transaction",
    "other_charges.ipft": "ipft",
    "other_charges.sebi_turnover": "sebi_fee",
    "other_charges.clearing": "clearing",
}


def charges_url(
    base: str, instrument_key: str, quantity: int, product: str, side: str, price: float
) -> str:
    """The brokerage-calculator URL (``product`` D or I, ``side`` BUY or SELL)."""
    return (
        f"{base.rstrip('/')}/v2/charges/brokerage?instrument_token={quote(instrument_key, safe='')}"
        f"&quantity={quantity}&product={product}&transaction_type={side}&price={price}"
    )


def parse_charges(payload: bytes) -> dict[str, float]:
    """The broker's charge breakdown for one order, in the cost model's
    names (documented reply; not yet probed), plus ``total``."""
    body = _load_json(payload, "charges")
    if not isinstance(body, dict) or body.get("status") != "success":
        raise PayloadError(f"charges: unexpected reply {str(body)[:200]}")
    charges = (body.get("data") or {}).get("charges")
    if not isinstance(charges, dict):
        raise PayloadError("charges: no data.charges object")
    out: dict[str, float] = {}
    for path, name in CHARGE_FIELDS.items():
        node: Any = charges
        for part in path.split("."):
            node = node.get(part) if isinstance(node, dict) else None
        value = _opt_float(node)
        if value is not None:
            out[name] = value
    total = _opt_float(charges.get("total"))
    if total is None:
        raise PayloadError("charges: no total")
    out["total"] = total
    return out
