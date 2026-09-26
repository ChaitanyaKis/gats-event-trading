"""Point-in-time reads for research and backtests.

Everything a strategy or study sees must come through :class:`AsOf`, which
only returns rows whose ``available_at`` is at or before the simulated clock.
This is the structural guard against look-ahead bias: there is no API here
that can return a row the system could not have known at ``as_of``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, Row, select

from gats.db.schema import announcements, eod_prices, price_bands
from gats.timeutil import ensure_aware


class AsOf:
    def __init__(self, conn: Connection, as_of: datetime) -> None:
        self._conn = conn
        self._as_of = ensure_aware(as_of)

    @property
    def as_of(self) -> datetime:
        return self._as_of

    def advance(self, to: datetime) -> None:
        """Move the clock forward. Moving backward is a bug and raises."""
        to = ensure_aware(to)
        if to < self._as_of:
            raise ValueError(f"cannot move point-in-time clock backwards: {to} < {self._as_of}")
        self._as_of = to

    def announcements_since(
        self, since: datetime, *, include_backfill: bool = True
    ) -> list[Row[Any]]:
        """Announcements that became available in ``(since, as_of]``."""
        query = select(announcements).where(
            announcements.c.available_at > ensure_aware(since),
            announcements.c.available_at <= self._as_of,
        )
        if not include_backfill:
            query = query.where(announcements.c.ingest_mode != "backfill")
        return list(self._conn.execute(query.order_by(announcements.c.available_at)))

    def eod_history(
        self, symbol: str, series: str = "EQ", start: date | None = None
    ) -> list[Row[Any]]:
        query = select(eod_prices).where(
            eod_prices.c.symbol == symbol,
            eod_prices.c.series == series,
            eod_prices.c.available_at <= self._as_of,
        )
        if start is not None:
            query = query.where(eod_prices.c.trade_date >= start)
        return list(self._conn.execute(query.order_by(eod_prices.c.trade_date)))

    def latest_band(self, symbol: str, series: str = "EQ") -> Row[Any] | None:
        return self._conn.execute(
            select(price_bands)
            .where(
                price_bands.c.symbol == symbol,
                price_bands.c.series == series,
                price_bands.c.available_at <= self._as_of,
            )
            .order_by(price_bands.c.as_of_date.desc())
            .limit(1)
        ).first()
