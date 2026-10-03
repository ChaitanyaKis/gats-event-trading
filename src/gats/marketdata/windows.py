"""Which one-minute bars M5 needs: the sessions around each event (T5.2).

A reaction starts in the session the filing appears in (during market
hours) or at the next open (after hours), and T5.3 also wants the session
before as a baseline. So an event's window is the previous regular session,
the event session and the next one. Bars come by the month (the broker's
unit) for the stock and for the benchmark index.

Events are the typed, linked filings the event study reads, through
:class:`gats.pit.AsOf`; nothing here looks at prices or returns.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from gats.ingest import Services
from gats.marketdata.bars import INTERVAL, month_start
from gats.marketdata.upstox import FIRST_MINUTE_DATA, fetch_month, month_status
from gats.pit import AsOf
from gats.refdata.calendar import CalendarError
from gats.refdata.master import Resolver
from gats.research.event_study import effective_availability
from gats.sources.upstox import equity_key
from gats.timeutil import ist_datetime, to_ist

_EPOCH_DAY = date(1970, 1, 1)
_IST_OFFSET_S = 19_800  # +05:30


@dataclass(frozen=True)
class EventWindow:
    announcement_id: int
    event_type: str
    security_id: int
    instrument_key: str
    available_at: datetime
    event_session: date  # where the reaction starts
    sessions: tuple[date, date, date]  # previous, event, next


def event_windows(
    clock: AsOf,
    *,
    event_types: Collection[str],
    taxonomy_version: str,
    start: date,
    end: date,
    source: str = "NSE",
    minute_precision_delay_s: int = 60,
    data_start: date = FIRST_MINUTE_DATA,
    current_isins: Collection[str] = (),
) -> tuple[list[EventWindow], Counter[str]]:
    """Windows for filings available on IST dates [start, end], and how many
    were dropped for each reason. ``current_isins`` (listed today) settle
    securities for which the master holds two ISINs at once."""
    cal = clock.calendar()
    resolver = clock.resolver()
    windows: list[EventWindow] = []
    dropped: Counter[str] = Counter()
    rows = clock.typed_filings(
        ist_datetime(start, time()),
        ist_datetime(end + timedelta(days=1), time()),
        taxonomy_version,
        source,
    )
    for row in rows:
        if row.event_type not in event_types:
            continue
        if row.security_id is None:
            dropped["unlinked"] += 1
            continue
        available = effective_availability(row, minute_precision_delay_s)
        try:
            day = cal.session_of(available) or to_ist(cal.next_session_open(available)).date()
            sessions = (cal.shift(day, -1), day, cal.shift(day, 1))
        except CalendarError:
            dropped["outside_calendar"] += 1
            continue
        if sessions[0] < data_start:
            dropped["before_minute_data"] += 1
            continue
        isin = _isin_on(resolver, row.security_id, day, current_isins)
        if isin is None:
            many = len(resolver.windows_of(row.security_id, "isin")) > 1
            dropped["ambiguous_isin" if many else "no_isin"] += 1
            continue
        windows.append(
            EventWindow(
                row.id,
                row.event_type,
                row.security_id,
                equity_key(isin),
                available,
                day,
                sessions,
            )
        )
    return windows, dropped


def _isin_on(
    resolver: Resolver, security_id: int, day: date, current: Collection[str]
) -> str | None:
    """The security's ISIN on ``day``. When the master holds two at once (an
    ISIN change, e.g. after a split, that it cannot date), the one listed
    today is the one the broker serves."""
    isin = resolver.identifier(security_id, "isin", day)
    if isin is not None:
        return isin
    live = [
        value
        for value, valid_from, valid_to in resolver.windows_of(security_id, "isin")
        if valid_from <= day and (valid_to is None or day < valid_to) and value in current
    ]
    return live[0] if len(live) == 1 else None


def months_needed(windows: Collection[EventWindow], index_key: str | None) -> dict[str, set[date]]:
    """instrument_key -> the months its windows touch (and the index's)."""
    need: dict[str, set[date]] = defaultdict(set)
    for window in windows:
        for day in window.sessions:
            need[window.instrument_key].add(month_start(day))
            if index_key:
                need[index_key].add(month_start(day))
    return dict(need)


async def fetch_needed(
    svc: Services, need: Mapping[str, Collection[date]], *, limit: int | None = None
) -> Counter[str]:
    """Fetch every needed instrument-month that is not complete, making at
    most ``limit`` requests; resumable, so a later run continues."""
    statuses: Counter[str] = Counter()
    requests = 0
    for key in sorted(need):
        for month in sorted(need[key]):
            with svc.engine.begin() as conn:
                if month_status(conn, key, month) == "complete":
                    statuses["already complete"] += 1
                    continue
            if limit is not None and requests >= limit:
                statuses["left for a later run"] += 1
                continue
            result = await fetch_month(svc, key, month)
            requests += result.status != "skipped"
            statuses[result.status] += 1
    return statuses


def bars_per_day(bars_dir: Path) -> dict[tuple[str, date], int]:
    """(instrument_key, IST date) -> bars stored, in one scan of every file."""
    import duckdb

    files = sorted(p.as_posix() for p in (bars_dir / INTERVAL).rglob("*.parquet"))
    if not files:
        return {}
    with duckdb.connect() as con:
        rows = con.execute(
            "SELECT instrument_key, (epoch_us(ts) // 1000000 + ?) // 86400 AS day, count(*) "
            "FROM read_parquet(?) GROUP BY 1, 2",
            [_IST_OFFSET_S, files],
        ).fetchall()
    return {(key, _EPOCH_DAY + timedelta(days=int(day))): int(n) for key, day, n in rows}


@dataclass
class WindowCoverage:
    events: int = 0
    covered: int = 0
    by_year: dict[int, list[int]] = field(default_factory=dict)  # year -> [covered, events]
    missing: list[EventWindow] = field(default_factory=list)  # a sample

    @property
    def share(self) -> float:
        return self.covered / self.events if self.events else 0.0


def window_coverage(
    windows: Collection[EventWindow],
    counts: Mapping[tuple[str, date], int],
    index_key: str | None,
    *,
    min_bars: int = 1,
    samples: int = 10,
) -> WindowCoverage:
    """An event is covered when every session of its window has bars for
    the stock and, if given, the index."""
    cov = WindowCoverage()
    for window in windows:
        keys = [window.instrument_key] + ([index_key] if index_key else [])
        ok = all(counts.get((key, day), 0) >= min_bars for key in keys for day in window.sessions)
        year = cov.by_year.setdefault(window.event_session.year, [0, 0])
        cov.events += 1
        year[1] += 1
        if ok:
            cov.covered += 1
            year[0] += 1
        elif len(cov.missing) < samples:
            cov.missing.append(window)
    return cov
