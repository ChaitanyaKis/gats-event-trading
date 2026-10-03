"""NSE equity trading calendar.

Which dates had a session, in order of evidence:

1. **Daily files.** A date with stock or index closes (``eod_days`` /
   ``index_days`` row ``loaded``) was a session. This also finds special
   sessions: Diwali Muhurat trading and weekend budget-day sessions publish
   their own files.
2. **Closed.** The day's file 404'd or held another day's rows (NSE's
   bhavcopy answer for weekday holidays; the index file simply 404s), or
   NSE's holiday list names the date.
3. **Weekends** are closed.
4. Any other weekday is assumed open (future dates, dates before the data).

A session on a weekend or listed holiday is *special*. NSE does not publish
its hours in machine-readable form (the holiday list's session fields are
null), so ``next_session_open`` skips special sessions: an entry is then
later than it could have been, never earlier. Regular hours come from
``Settings`` (NSE market-timings page, checked 2026-10-03: 09:15-15:30 IST).

The calendar is exchange schedule information, known in advance, so it is not
point-in-time filtered: an unscheduled closure would be the only leak, and it
can only delay a simulated entry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date, datetime, time, timedelta

from sqlalchemy import Connection, select

from gats.db import repo
from gats.db.schema import eod_days, eod_prices, index_days, market_holidays
from gats.timeutil import ensure_aware, ist_datetime, ist_today, to_ist

# Searching further than this for the next session means the calendar has a
# hole (no NSE closure has ever lasted this long).
_MAX_SEARCH_DAYS = 30


class CalendarError(ValueError):
    pass


class TradingCalendar:
    def __init__(
        self,
        *,
        sessions: Iterable[date],
        closed: Iterable[date] = (),
        holidays: Mapping[date, str | None] | None = None,
        open_ist: time = time(9, 15),
        close_ist: time = time(15, 30),
    ) -> None:
        self._sessions = frozenset(sessions)
        self._closed = frozenset(closed) - self._sessions
        self._holidays = dict(holidays or {})
        self._open = open_ist
        self._close = close_ist

    @classmethod
    def load(
        cls,
        conn: Connection,
        *,
        open_ist: time = time(9, 15),
        close_ist: time = time(15, 30),
        today: date | None = None,
        max_attempts: int = 3,
        segment: str = "CM",
    ) -> TradingCalendar:
        today = today or ist_today()
        sessions = set(conn.execute(select(eod_prices.c.trade_date).distinct()).scalars())
        closed: set[date] = set()
        for table in (eod_days, index_days):
            for row in conn.execute(select(table)):
                if row.status == "loaded":
                    sessions.add(row.trade_date)
                elif repo.eod_day_settled(row, today=today, max_attempts=max_attempts):
                    closed.add(row.trade_date)
        holidays = {
            r.holiday_date: r.description
            for r in conn.execute(
                select(market_holidays.c.holiday_date, market_holidays.c.description).where(
                    market_holidays.c.segment == segment
                )
            )
        }
        return cls(
            sessions=sessions,
            closed=closed,
            holidays=holidays,
            open_ist=open_ist,
            close_ist=close_ist,
        )

    # --- days -------------------------------------------------------------------------

    @property
    def coverage(self) -> tuple[date, date] | None:
        """First and last date with recorded EOD data."""
        if not self._sessions:
            return None
        return min(self._sessions), max(self._sessions)

    def holiday_name(self, day: date) -> str | None:
        return self._holidays.get(day)

    def is_trading_day(self, day: date) -> bool:
        if day in self._sessions:
            return True
        if day in self._closed or day in self._holidays:
            return False
        return day.weekday() < 5

    def is_special_session(self, day: date) -> bool:
        """A session on a weekend or a listed holiday (hours not published)."""
        return day in self._sessions and (day.weekday() >= 5 or day in self._holidays)

    def is_regular_session(self, day: date) -> bool:
        return self.is_trading_day(day) and not self.is_special_session(day)

    def trading_days(self, start: date, end: date, *, include_special: bool = True) -> list[date]:
        """Sessions in ``[start, end]``. Special sessions are included by
        default: they have prices, so daily return series need them."""
        days = []
        day = start
        while day <= end:
            if self.is_trading_day(day) and (include_special or not self.is_special_session(day)):
                days.append(day)
            day += timedelta(days=1)
        return days

    def shift(self, day: date, n: int) -> date:
        """The ``n``-th regular session after ``day`` (before it if negative).
        ``shift(d, 0)`` is ``d`` itself when it is a regular session."""
        if n == 0:
            if not self.is_regular_session(day):
                raise CalendarError(f"{day} is not a regular session")
            return day
        step = 1 if n > 0 else -1
        remaining, current, searched = abs(n), day, 0
        while remaining:
            current += timedelta(days=step)
            searched += 1
            if searched > _MAX_SEARCH_DAYS * abs(n):
                raise CalendarError(f"no session found within {searched} days of {day}")
            if self.is_regular_session(current):
                remaining -= 1
        return current

    # --- times ------------------------------------------------------------------------

    def session_open(self, day: date) -> datetime:
        self._require_regular(day)
        return ist_datetime(day, self._open)

    def session_close(self, day: date) -> datetime:
        self._require_regular(day)
        return ist_datetime(day, self._close)

    def next_session_open(self, after: datetime) -> datetime:
        """The first regular session open strictly after ``after``.

        This is the earliest moment a decision made at ``after`` can trade at
        the open; an event at exactly 09:15:00 IST waits for the next session.
        """
        after = ensure_aware(after)
        day = to_ist(after).date()
        for _ in range(_MAX_SEARCH_DAYS):
            if self.is_regular_session(day):
                opens = ist_datetime(day, self._open)
                if opens > after:
                    return opens
            day += timedelta(days=1)
        raise CalendarError(f"no regular session within {_MAX_SEARCH_DAYS} days after {after}")

    def session_of(self, moment: datetime) -> date | None:
        """The regular session ``moment`` falls in (open inclusive, close
        exclusive), or None outside market hours."""
        local = to_ist(ensure_aware(moment))
        day = local.date()
        if not self.is_regular_session(day):
            return None
        clock = local.time().replace(tzinfo=None)
        return day if self._open <= clock < self._close else None

    def _require_regular(self, day: date) -> None:
        if not self.is_regular_session(day):
            kind = "special session" if self.is_special_session(day) else "not a session"
            raise CalendarError(f"{day}: {kind}; regular hours do not apply")
