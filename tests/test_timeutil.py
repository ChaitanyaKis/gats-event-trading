from __future__ import annotations

from datetime import UTC, date, datetime, time

import pytest

from gats.timeutil import (
    IST,
    daterange,
    ist_datetime,
    ist_time_of_day,
    ist_today,
    parse_date,
    parse_ist_datetime,
    to_utc,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("25-Sep-2026 10:15:30", datetime(2026, 9, 25, 4, 45, 30, tzinfo=UTC)),
        ("2026-09-25T10:15:30.123", datetime(2026, 9, 25, 4, 45, 30, 123000, tzinfo=UTC)),
        ("2026-09-25 10:15:30", datetime(2026, 9, 25, 4, 45, 30, tzinfo=UTC)),
        ("2026-09-25T10:15:30+00:00", datetime(2026, 9, 25, 10, 15, 30, tzinfo=UTC)),
    ],
)
def test_parse_ist_datetime_converts_to_utc(raw: str, expected: datetime) -> None:
    assert parse_ist_datetime(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "-", "null", "not a date", 12345])
def test_parse_ist_datetime_returns_none_for_bad_input(raw: object) -> None:
    assert parse_ist_datetime(raw) is None


def test_to_utc_rejects_naive() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_utc(datetime(2026, 1, 1))


def test_ist_helpers_cross_midnight() -> None:
    # 19:00 UTC is 00:30 IST the next day.
    now = datetime(2026, 9, 25, 19, 0, tzinfo=UTC)
    assert ist_today(now) == date(2026, 9, 26)
    assert ist_time_of_day(now) == time(0, 30)
    assert ist_datetime(date(2026, 9, 26), time(0, 30)) == now
    assert now.astimezone(IST).hour == 0


def test_parse_date_formats() -> None:
    assert parse_date("06-OCT-2008") == date(2008, 10, 6)
    assert parse_date("25-Sep-2026") == date(2026, 9, 25)
    assert parse_date("2026-09-25") == date(2026, 9, 25)
    assert parse_date("-") is None


def test_daterange_is_inclusive_and_validates() -> None:
    assert list(daterange(date(2026, 1, 30), date(2026, 2, 1))) == [
        date(2026, 1, 30),
        date(2026, 1, 31),
        date(2026, 2, 1),
    ]
    with pytest.raises(ValueError):
        list(daterange(date(2026, 2, 1), date(2026, 1, 1)))
