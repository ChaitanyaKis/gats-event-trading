"""Point-in-time reads for research and backtests.

Everything a strategy or study sees must come through :class:`AsOf`, which
only returns rows whose ``available_at`` is at or before the simulated clock.
This is the structural guard against look-ahead bias: there is no API here
that can return a row the system could not have known at ``as_of``.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Connection, Row, and_, func, select

from gats.db.schema import (
    announcement_event_group,
    announcement_event_types,
    announcement_security,
    announcements,
    eod_prices,
    index_eod,
    price_bands,
    surveillance_versions,
)
from gats.refdata.actions import ReturnAdjuster
from gats.refdata.calendar import TradingCalendar
from gats.refdata.master import Resolver
from gats.sources.nse_surveillance import TRADE_FOR_TRADE_SERIES
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

    def surveillance(self, symbol: str) -> dict[str, Any]:
        """What restricts trading in ``symbol`` as of the clock.

        ``lists``: ASM/GSM entries in force (stage per list); ``gsm_remark``:
        the GSM stage in the latest price-band file; ``trade_for_trade``: the
        latest band file lists it only in a BE/BZ/ST/SZ series (delivery only,
        no intraday). The risk engine blocks on any of these.
        """
        symbol = symbol.upper()
        today = to_ist(self._as_of).date()
        lists = {
            row.list_name: row.stage
            for row in self._conn.execute(
                select(surveillance_versions).where(
                    surveillance_versions.c.symbol == symbol,
                    surveillance_versions.c.valid_from <= today,
                    (surveillance_versions.c.valid_to.is_(None))
                    | (surveillance_versions.c.valid_to > today),
                    surveillance_versions.c.available_at <= self._as_of,
                )
            )
        }
        latest = self._conn.execute(
            select(func.max(price_bands.c.as_of_date)).where(
                price_bands.c.symbol == symbol, price_bands.c.available_at <= self._as_of
            )
        ).scalar()
        series: set[str] = set()
        remark = None
        if latest is not None:
            for row in self._conn.execute(
                select(price_bands).where(
                    price_bands.c.symbol == symbol, price_bands.c.as_of_date == latest
                )
            ):
                series.add(row.series)
                if row.remarks and "GSM" in row.remarks.upper():
                    remark = row.remarks
        return {
            "lists": lists,
            "gsm_remark": remark,
            "trade_for_trade": bool(series) and series <= TRADE_FOR_TRADE_SERIES,
            "band_date": latest,
        }

    def resolver(self) -> Resolver:
        """The security master's identifier lookup (built once per clock)."""
        if self._resolver is None:
            self._resolver = Resolver.load(self._conn)
        return self._resolver

    def typed_filings(
        self, start: datetime, end: datetime, taxonomy_version: str, source: str
    ) -> list[Row[Any]]:
        """Filings available in ``[start, end)`` (and by the clock) with their
        event type under ``taxonomy_version`` and their linked security."""
        a, t, link = announcements, announcement_event_types, announcement_security
        return list(
            self._conn.execute(
                select(
                    a.c.id,
                    a.c.source,
                    a.c.symbol,
                    a.c.category,
                    a.c.subject,
                    a.c.event_ts,
                    a.c.available_at,
                    a.c.exch_disseminated_ts,
                    t.c.event_type,
                    link.c.security_id,
                )
                .select_from(
                    a.join(
                        t,
                        and_(
                            t.c.announcement_id == a.c.id, t.c.taxonomy_version == taxonomy_version
                        ),
                    ).outerjoin(link, link.c.announcement_id == a.c.id)
                )
                .where(
                    a.c.source == source,
                    a.c.available_at >= ensure_aware(start),
                    a.c.available_at < ensure_aware(end),
                    a.c.available_at <= self._as_of,
                )
                .order_by(a.c.available_at, a.c.id)
            )
        )
