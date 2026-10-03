"""Event-window bars (T5.2): which sessions each event needs, fetching, coverage."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime, time
from typing import Any, cast

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import Connection, Engine

from gats.ingest import Services
from gats.marketdata.bars import write_month
from gats.marketdata.windows import (
    EventWindow,
    _isin_on,
    bars_per_day,
    event_windows,
    fetch_needed,
    months_needed,
    window_coverage,
)
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata.master import Resolver
from gats.sources.upstox import Bar, candles_url
from gats.timeutil import ist_datetime
from tests.test_event_study import VERSION, Market, after_close

INDEX = "NSE_INDEX|Nifty 500"


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection


def windows_of(conn: Connection, **kwargs: Any) -> tuple[dict[int, EventWindow], Any]:
    found, dropped = event_windows(
        AsOf(conn, datetime(2024, 4, 1, tzinfo=UTC)),
        event_types={"ORDER_WIN"},
        taxonomy_version=VERSION,
        start=date(2023, 12, 1),
        end=date(2024, 3, 15),
        **kwargs,
    )
    return {w.announcement_id: w for w in found}, dropped


def test_windows_follow_the_calendar(conn: Connection, store: RawStore) -> None:
    market = Market(conn, store)
    market.list_stocks(["AAA", "BBB"])
    market.write_prices(["AAA", "BBB"])
    in_hours = market.filing("AAA", "ORDER_WIN", ist_datetime(date(2024, 1, 10), time(11, 0)))
    after = market.filing("AAA", "ORDER_WIN", after_close(date(2024, 1, 10)))
    # Thursday after close; Friday 26 January is Republic Day.
    long_weekend = market.filing("BBB", "ORDER_WIN", after_close(date(2024, 1, 25)))
    market.filing("BBB", "RESULTS", after_close(date(2024, 1, 10)))  # not in scope
    market.filing("AAA", "ORDER_WIN", after_close(date(2024, 1, 11)), linked=False)

    found, dropped = windows_of(conn)
    assert set(found) == {in_hours, after, long_weekend}
    assert dict(dropped) == {"unlinked": 1}
    assert found[in_hours].sessions == (date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11))
    assert found[after].event_session == date(2024, 1, 11)
    assert found[long_weekend].sessions == (
        date(2024, 1, 25),
        date(2024, 1, 29),
        date(2024, 1, 30),
    )
    assert found[in_hours].instrument_key.startswith("NSE_EQ|INE")

    later, dropped = windows_of(conn, data_start=date(2024, 1, 11))
    assert set(later) == {long_weekend} and dropped["before_minute_data"] == 2


class FakeResolver:
    """Two ISINs valid at once: an ISIN change the master cannot date."""

    def identifier(self, security_id: int, id_type: str, on: date) -> str | None:
        return None

    def windows_of(self, security_id: int, id_type: str) -> list[tuple[str, date, date | None]]:
        return [("INE257A01018", date(1900, 1, 1), None), ("INE257A01026", date(1900, 1, 1), None)]


def test_two_isins_are_settled_by_todays_listing() -> None:
    resolver = cast(Resolver, FakeResolver())
    day = date(2026, 6, 5)
    assert _isin_on(resolver, 1, day, {"INE257A01026", "INE002A01018"}) == "INE257A01026"
    assert _isin_on(resolver, 1, day, ()) is None  # cannot tell: dropped, not guessed


def window(key: str, *days: date) -> EventWindow:
    first, mid, last = days
    return EventWindow(
        1, "ORDER_WIN", 1, key, datetime(2024, 1, 1, tzinfo=UTC), mid, (first, mid, last)
    )


def test_months_needed_cover_the_stock_and_the_index() -> None:
    key = "NSE_EQ|INE002A01018"
    need = months_needed(
        [window(key, date(2024, 1, 30), date(2024, 1, 31), date(2024, 2, 1))], INDEX
    )
    assert need == {
        key: {date(2024, 1, 1), date(2024, 2, 1)},
        INDEX: {date(2024, 1, 1), date(2024, 2, 1)},
    }


@respx.mock
async def test_fetching_is_capped_and_resumable(svc: Services) -> None:
    svc.settings.upstox_analytics_token = SecretStr("tok")
    key = "NSE_EQ|INE002A01018"
    body = b'{"status": "success", "data": {"candles": []}}'
    for month, last in (
        (date(2026, 7, 1), date(2026, 7, 31)),
        (date(2026, 8, 1), date(2026, 8, 31)),
    ):
        respx.get(candles_url(svc.settings.upstox_api_base, key, "minutes", 1, last, month)).mock(
            return_value=httpx.Response(200, content=body)
        )
    need = {key: {date(2026, 7, 1), date(2026, 8, 1)}}
    first = await fetch_needed(svc, need, limit=1)
    assert dict(first) == {"complete": 1, "left for a later run": 1}
    second = await fetch_needed(svc, need)
    assert dict(second) == {"already complete": 1, "complete": 1}


def test_coverage_needs_every_session_for_stock_and_index(tmp_path: Any) -> None:
    key = "NSE_EQ|INE002A01018"
    days = (date(2024, 1, 9), date(2024, 1, 10), date(2024, 1, 11))
    full = window(key, *days)
    partial = window("NSE_EQ|INE009A01021", *days)

    def minute(day: date) -> datetime:
        return ist_datetime(day, time(9, 15))

    def bar(instrument: str, day: date) -> Bar:
        return Bar(instrument, minute(day), 1, 1, 1, 1, 1, 0)

    write_month(tmp_path, key, date(2024, 1, 1), [bar(key, d) for d in days])
    write_month(tmp_path, INDEX, date(2024, 1, 1), [bar(INDEX, d) for d in days])
    write_month(
        tmp_path, partial.instrument_key, date(2024, 1, 1), [bar(partial.instrument_key, days[0])]
    )
    counts = bars_per_day(tmp_path)
    assert counts[(key, date(2024, 1, 10))] == 1
    cov = window_coverage([full, partial], counts, INDEX)
    assert (cov.covered, cov.events, cov.by_year) == (1, 2, {2024: [1, 2]})
    assert cov.missing == [partial] and cov.share == 0.5
    assert window_coverage([full], counts, "NSE_INDEX|Nifty 50").covered == 0  # no index bars
    assert bars_per_day(tmp_path / "empty") == {}
