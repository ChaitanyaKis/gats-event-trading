"""Time helpers.

Rules used across the codebase:
- Every stored timestamp is timezone-aware UTC.
- Exchange timestamps arrive as IST wall-clock strings and are converted here.
- Naive datetimes are rejected rather than silently guessed.

India observes no daylight saving, so IST is a fixed UTC+05:30 offset. Using a
fixed offset avoids depending on the tz database, which Windows lacks.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30), name="IST")

_EMPTY_MARKERS = frozenset({"", "-", "null", "none", "nan", "nat"})

# Formats seen in NSE/BSE feeds. Order matters only for speed.
_DATETIME_FORMATS = (
    "%d-%b-%Y %H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S.%f",
    "%d-%b-%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S",
)

_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d%m%Y", "%Y%m%d")


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_aware(value: datetime) -> datetime:
    """Return ``value`` unchanged if it is timezone-aware, else raise."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {value!r}")
    return value


def to_utc(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(UTC)


def to_ist(value: datetime) -> datetime:
    return ensure_aware(value).astimezone(IST)


def ist_today(now: datetime | None = None) -> date:
    return to_ist(now or utcnow()).date()


def ist_time_of_day(now: datetime | None = None) -> time:
    return to_ist(now or utcnow()).timetz().replace(tzinfo=None)


def ist_datetime(day: date, at: time) -> datetime:
    """The UTC instant for IST wall-clock ``at`` on ``day``."""
    return datetime.combine(day, at, tzinfo=IST).astimezone(UTC)


def _is_empty(raw: object) -> bool:
    return raw is None or str(raw).strip().lower() in _EMPTY_MARKERS


def parse_ist_datetime(raw: object) -> datetime | None:
    """Parse an exchange timestamp (IST wall clock unless it carries an offset).

    Returns an aware UTC datetime, or ``None`` for empty or unparseable input.
    Callers record unparseable values as parser warnings.
    """
    if _is_empty(raw):
        return None
    text = str(raw).strip()
    for fmt in _DATETIME_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=IST).astimezone(UTC)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(UTC)


def parse_date(raw: object) -> date | None:
    if _is_empty(raw):
        return None
    text = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def daterange(start: date, end: date) -> Iterator[date]:
    """Inclusive range of calendar days."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5
