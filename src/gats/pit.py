"""Point-in-time reads for research and backtests.

Everything a strategy or study sees must come through :class:`AsOf`, which
only returns rows whose ``available_at`` is at or before the simulated clock.
This is the structural guard against look-ahead bias: there is no API here
that can return a row the system could not have known at ``as_of``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Connection, Row, func, select

from gats.db.schema import (
    announcement_event_group,
    announcement_security,
    announcements,
    eod_prices,
    index_eod,
    price_bands,
)
from gats.refdata.actions import ReturnAdjuster
from gats.refdata.calendar import TradingCalendar
from gats.refdata.master import Resolver
from gats.timeutil import ensure_aware, to_ist


class AsOf:
    def __init__(self, conn: Connection, as_of: datetime) -> None:
        self._conn = conn
        self._as_of = ensure_aware(as_of)
        self._resolver: Resolver | None = None
        self._calendar: TradingCalendar | None = None
        self._adjuster: ReturnAdjuster | None = None

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

    def security(
        self, id_type: str, value: str, on: date | None = None, *, strict: bool = False
    ) -> int | None:
        """The security an identifier meant on ``on`` (default: the clock's IST date).

        Asking about a date after the clock raises: tomorrow's symbol map is
        future information. ``strict=True`` also hides identifier links the
        system had not yet observed (live simulation). The default accepts the
        documented backward extension of mappings first seen in today's
        reference files, which research on backfilled history needs; those
        links carry no price information.
        """
        today = to_ist(self._as_of).date()
        day = on or today
        if day > today:
            raise ValueError(f"cannot resolve identifiers for {day}, after the clock ({today})")
        if self._resolver is None:
            self._resolver = Resolver.load(self._conn)
        return self._resolver.resolve(id_type, value, day, known_at=self._as_of if strict else None)

    def events_since(self, since: datetime) -> list[Row[Any]]:
        """Events (a disclosure, merged across BSE and NSE) that became
        available in ``(since, as_of]``.

        Only member filings available by the clock count, so a twin filed on
        the other exchange later can neither make an event visible sooner nor
        move its time. Filings not yet grouped (``gats refdata dedupe``) are
        not returned.
        """
        a, g, link = announcements, announcement_event_group, announcement_security
        return list(
            self._conn.execute(
                select(
                    g.c.event_group_id,
                    func.min(a.c.event_ts).label("event_ts"),
                    func.min(a.c.available_at).label("available_at"),
                    func.max(link.c.security_id).label("security_id"),
                    func.count().label("n_filings"),
                )
                .select_from(
                    a.join(g, g.c.announcement_id == a.c.id).outerjoin(
                        link, link.c.announcement_id == a.c.id
                    )
                )
                .where(a.c.available_at <= self._as_of)
                .group_by(g.c.event_group_id)
                .having(func.min(a.c.available_at) > ensure_aware(since))
                .order_by(func.min(a.c.available_at))
            )
        )

    def calendar(
        self, *, open_ist: time = time(9, 15), close_ist: time = time(15, 30)
    ) -> TradingCalendar:
        """The exchange calendar. Not clock-filtered: holidays are announced
        in advance, and an unscheduled closure can only delay an entry."""
        if self._calendar is None:
            self._calendar = TradingCalendar.load(
                self._conn, open_ist=open_ist, close_ist=close_ist
            )
        return self._calendar

    def index_history(self, index_name: str, start: date | None = None) -> list[Row[Any]]:
        """Daily closes of one NSE index (name as published, e.g. "Nifty 500")."""
        query = select(index_eod).where(
            index_eod.c.index_name == index_name, index_eod.c.available_at <= self._as_of
        )
        if start is not None:
            query = query.where(index_eod.c.trade_date >= start)
        return list(self._conn.execute(query.order_by(index_eod.c.trade_date)))

    def return_adjuster(self) -> ReturnAdjuster:
        """Split/bonus multipliers for actions knowable by the clock. Actions
        are announced before their ex-date, so this never hides one that
        affects a return already observed."""
        if self._adjuster is None:
            self._adjuster = ReturnAdjuster.load(self._conn, as_of=self._as_of)
        return self._adjuster
