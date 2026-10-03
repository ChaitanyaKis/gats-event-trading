"""From the database and the bar files to the engine's input (T6.8).

Events are the typed, linked filings the event study and the bar fetcher
already use (:mod:`gats.marketdata.windows`), each carrying the facts
extracted from its own text. Bars are the one-minute files around them. The
risk engine's lookups read end-of-day data strictly before the day asked
about, so a backtest cannot size or filter with information from later.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date, time, timedelta
from pathlib import Path
from typing import Any

from gats.marketdata.bars import read_bars
from gats.marketdata.windows import EventWindow
from gats.pit import AsOf
from gats.strategy.base import BarEvent, MarketEvent
from gats.timeutil import ist_datetime


def market_events(
    windows: Sequence[EventWindow], facts: Mapping[int, Mapping[str, Any]]
) -> list[MarketEvent]:
    """One event per window, with whatever was extracted from the filing."""
    return [
        MarketEvent(
            w.announcement_id,
            w.security_id,
            w.instrument_key,
            w.event_type,
            w.available_at,
            dict(facts.get(w.announcement_id, {})),
        )
        for w in windows
    ]


def bar_events(bars_dir: Path, windows: Sequence[EventWindow]) -> list[BarEvent]:
    """The stored bars of every window session, each (stock, minute) once."""
    wanted = sorted({(w.instrument_key, day) for w in windows for day in w.sessions})
    events = []
    for key, day in wanted:
        start = ist_datetime(day, time())
        for bar in read_bars(bars_dir, key, start, start + timedelta(days=1)):
            events.append(BarEvent(key, bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume))
    return events


class Lookups:
    """Liquidity and surveillance lookups for the risk engine, by instrument
    key and day, read through the point-in-time clock."""

    def __init__(
        self,
        clock: AsOf,
        securities: Sequence[EventWindow] | Mapping[str, int],
        lookback: int = 20,
    ) -> None:
        """``securities``: the event windows, or instrument key -> security id."""
        self._clock = clock
        self._security = (
            dict(securities)
            if isinstance(securities, Mapping)
            else {w.instrument_key: w.security_id for w in securities}
        )
        self._lookback = lookback
        self._turnover: dict[str, list[tuple[date, float]]] = {}

    def _symbol(self, key: str, day: date) -> str | None:
        security_id = self._security.get(key)
        if security_id is None:
            return None
        return self._clock.resolver().identifier(security_id, "nse_symbol", day)

    def liquidity(self, key: str, day: date) -> float | None:
        """Median daily traded value over the last ``lookback`` sessions
        before ``day`` (None with fewer: fail closed)."""
        symbol = self._symbol(key, day)
        if symbol is None:
            return None
        if symbol not in self._turnover:
            self._turnover[symbol] = [
                (row.trade_date, (row.turnover_lacs or 0.0) * 1e5)
                for row in self._clock.eod_history(symbol)
            ]
        before = [value for when, value in self._turnover[symbol] if when < day]
        if len(before) < self._lookback:
            return None
        return statistics.median(before[-self._lookback :])

    def flags(self, key: str, day: date) -> frozenset[str] | None:
        symbol = self._symbol(key, day)
        return None if symbol is None else self._clock.restrictions_on(symbol, day)


def with_revenue_ratio(
    clock: AsOf, windows: Sequence[EventWindow], facts: Mapping[int, Mapping[str, Any]]
) -> dict[int, dict[str, Any]]:
    """Add ``amount_vs_revenue`` (the event's rupee amount over the company's
    trailing revenue as known when the filing appeared) where both exist."""
    out: dict[int, dict[str, Any]] = {}
    for window in windows:
        known = dict(facts.get(window.announcement_id, {}))
        amount = known.get("amount_inr")
        revenue = clock.trailing_revenue(window.security_id, window.available_at)
        if amount is not None and revenue is not None and revenue.rupees > 0:
            known["amount_vs_revenue"] = float(amount) / revenue.rupees
            known["trailing_revenue_rs"] = revenue.rupees
        if known:
            out[window.announcement_id] = known
    return out
