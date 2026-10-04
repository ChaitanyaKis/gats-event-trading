"""Event-study engine (T3.3): planted effects, filters, splits and leak tests."""

from __future__ import annotations

import random
import statistics
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest
from sqlalchemy import Connection, Engine

from gats.db import repo
from gats.db.schema import announcement_event_types, announcement_security, market_holidays
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.refdata import master
from gats.refdata.actions import store_actions
from gats.refdata.calendar import TradingCalendar
from gats.research.event_study import (
    EventResult,
    effective_availability,
    entry_session,
    filter_counts,
    run_event_study,
    to_records,
    write_parquet,
)
from gats.research.study import StudyConfig
from gats.sources.models import AnnouncementRecord, EodRecord, InstrumentRecord
from gats.sources.nse_corp_actions import CorporateActionRecord
from gats.sources.nse_indices import IndexRecord
from gats.timeutil import IST, ist_datetime

START, END = date(2023, 11, 1), date(2024, 3, 29)
HOLIDAY = date(2024, 1, 26)  # Republic Day
VERSION = "test-taxonomy"
EFFECT = 0.03


def sessions() -> list[date]:
    days, d = [], START
    while d <= END:
        if d.weekday() < 5 and d != HOLIDAY:
            days.append(d)
        d += timedelta(days=1)
    return days


SESSIONS = sessions()


def config(**overrides: Any) -> StudyConfig:
    raw: dict[str, Any] = {
        "study": "test",
        "taxonomy_version": VERSION,
        "data": {
            "source": "NSE",
            "start": "2023-12-01",
            "end": "2024-03-15",
            "train_end": "2024-01-31",
            "test_start": "2024-02-01",
        },
        "events": {
            "confirmatory": ["ORDER_WIN", "PRESS_RELEASE"],
            "exploratory": ["RESULTS"],
            "exclude_categories": ["Daily Buy Back of equity shares"],
            "minute_precision_delay_s": 60,
            "same_type_cooldown_sessions": 5,
        },
        "entry": {"rule": "first open after", "price": "OPEN_PRICE"},
        "exits": [
            {"name": "d0", "sessions_after_entry": 0},
            {"name": "d1", "sessions_after_entry": 1},
            {"name": "d3", "sessions_after_entry": 3},
        ],
        "benchmark": {"index": "Nifty 500", "model": "market_adjusted"},
        "filters": {
            "series": ["EQ"],
            "min_median_turnover_rs": 1e7,
            "turnover_lookback_sessions": 20,
            "min_prev_close_rs": 10,
            "skip_locked_entry_day": True,
            "max_entry_gap": 0.195,
            "exclude_review_actions": True,
        },
        "costs": {"round_trip": 0.005},
        "statistics": {
            "cluster": "event_date",
            "bootstrap_resamples": 100,
            "bootstrap_seed": 1,
            "fdr": {"method": "bh", "q": 0.05, "family": "x"},
            "ci": "fcr",
            "min_test_events": 1,
        },
    }
    raw.update(overrides)
    return StudyConfig.model_validate(raw)


@dataclass
class Market:
    """A synthetic market written through the real repository functions."""

    conn: Connection
    store: RawStore
    rng: random.Random = field(default_factory=lambda: random.Random(7))
    n: int = 0
    # (symbol, session) -> extra open->close return to plant
    planted: dict[tuple[str, date], float] = field(default_factory=dict)
    overrides: dict[tuple[str, date], dict[str, float]] = field(default_factory=dict)
    turnover_rs: dict[str, float] = field(default_factory=dict)
    taxonomy_version: str = VERSION  # the version its filings are typed under

    def doc(self) -> str:
        self.n += 1
        return repo.save_raw(
            self.conn,
            self.store,
            f"m{self.n}".encode(),
            kind="t",
            source="NSE",
            url="u",
            content_type=None,
            fetched_at=datetime(2024, 4, 1, tzinfo=UTC),
        )

    def list_stocks(self, symbols: list[str]) -> None:
        repo.upsert_instruments(
            self.conn,
            END,
            [
                InstrumentRecord(s, "EQ", f"INE{i:05d}A0101", f"{s} Ltd", None, 10.0, 1)
                for i, s in enumerate(symbols)
            ],
            raw_doc_id=self.doc(),
            parser_version="v",
            available_at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        master.build(self.conn, datetime(2024, 4, 1, tzinfo=UTC))

    def write_prices(self, symbols: list[str]) -> dict[date, float]:
        index_level, index_rows, index_ret = 20000.0, [], {}
        for day in SESSIONS:
            ret = self.rng.gauss(0, 0.008)
            index_rows.append(
                IndexRecord(
                    day,
                    "Nifty 500",
                    index_level,
                    None,
                    None,
                    index_level * (1 + ret),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                )
            )
            index_ret[day] = ret
            index_level *= 1 + ret
        repo.upsert_index_eod(
            self.conn,
            index_rows,
            raw_doc_id=self.doc(),
            parser_version="v",
            available_at=datetime(2024, 4, 1, tzinfo=UTC),
        )
        for symbol in symbols:
            close, rows = 100.0, []
            for day in SESSIONS:
                prev_close = close
                open_ = prev_close * (1 + self.rng.gauss(0, 0.004))
                move = (
                    index_ret[day] + self.rng.gauss(0, 0.01) + self.planted.get((symbol, day), 0.0)
                )
                close = open_ * (1 + move)
                values = {
                    "open": open_,
                    "high": max(open_, close) * 1.002,
                    "low": min(open_, close) * 0.998,
                    "close": close,
                    "prev_close": prev_close,
                }
                values.update(self.overrides.get((symbol, day), {}))
                close = values["close"]
                turnover = self.turnover_rs.get(symbol, 5e7)
                rows.append(
                    EodRecord(
                        day,
                        symbol,
                        "EQ",
                        values["prev_close"],
                        values["open"],
                        values["high"],
                        values["low"],
                        close,
                        close,
                        close,
                        1000,
                        turnover / 1e5,
                        100,
                        500,
                        50.0,
                    )
                )
            repo.upsert_eod(
                self.conn,
                rows,
                raw_doc_id=self.doc(),
                parser_version="v",
                available_at=datetime(2024, 4, 1, tzinfo=UTC),
            )
        self.conn.execute(
            market_holidays.insert().values(
                segment="CM",
                holiday_date=HOLIDAY,
                description="Republic Day",
                available_at=datetime(2023, 12, 1, tzinfo=UTC),
                raw_doc_id=self.doc(),
                parser_version="v",
            )
        )
        return index_ret

    def filing(
        self,
        symbol: str,
        event_type: str,
        at: datetime,
        *,
        category: str = "x",
        linked: bool = True,
        disseminated: bool = True,
    ) -> int:
        self.n += 1
        key = f"f{self.n}"
        record = AnnouncementRecord(
            source="NSE",
            source_ann_id=f"f{self.n}",
            symbol=symbol,
            scrip_code=None,
            isin=None,
            company_name=symbol,
            category=category,
            subcategory=None,
            subject=category,
            details=None,
            attachment_url=None,
            exch_submitted_ts=None,
            exch_disseminated_ts=at if disseminated else None,
            event_ts=at,
        )
        repo.insert_announcements(
            self.conn,
            [record],
            raw_doc_id=self.doc(),
            parser_version="v",
            mode="backfill",
            fetched_at=at,
            now=at,
        )
        from sqlalchemy import select

        from gats.db.schema import announcements

        ann_id = int(
            self.conn.execute(
                select(announcements.c.id).where(announcements.c.source_ann_id == key)
            ).scalar_one()
        )
        self.conn.execute(
            announcement_event_types.insert().values(
                announcement_id=ann_id,
                taxonomy_version=self.taxonomy_version,
                event_type=event_type,
                rule_no=0,
                classified_at=at,
            )
        )
        if linked:
            sid = master.Resolver.load(self.conn).resolve("nse_symbol", symbol, at.date())
            self.conn.execute(
                announcement_security.insert().values(
                    announcement_id=ann_id, security_id=sid, method="t", build_id=1, linked_at=at
                )
            )
        return ann_id


def after_close(day: date) -> datetime:
    return ist_datetime(day, time(16, 0))


@pytest.fixture
def conn(engine: Engine) -> Iterator[Connection]:
    with engine.begin() as connection:
        yield connection


def run(
    conn: Connection, cfg: StudyConfig | None = None, at: datetime | None = None
) -> dict[int, EventResult]:
    clock = AsOf(conn, at or datetime(2024, 4, 1, tzinfo=UTC))
    return {e.announcement_id: e for e in run_event_study(clock, cfg or config())}


def test_planted_effect_is_recovered_and_placebo_is_zero(conn: Connection, store: RawStore) -> None:
    symbols = [f"S{i:02d}" for i in range(30)]
    m = Market(conn, store)
    m.list_stocks(symbols)
    event_days = [SESSIONS[i] for i in range(30, 90, 4)]  # well inside the window
    plan: list[tuple[str, str, date]] = []
    for k, day in enumerate(event_days):
        for j in range(6):
            symbol = symbols[(k * 6 + j) % 30]
            entry = SESSIONS[SESSIONS.index(day) + 1]
            event_type = "ORDER_WIN" if j < 3 else "PRESS_RELEASE"
            if event_type == "ORDER_WIN":
                m.planted[(symbol, entry)] = EFFECT
            plan.append((symbol, event_type, day))
    m.write_prices(symbols)
    for symbol, event_type, day in plan:
        m.filing(symbol, event_type, after_close(day))
    results = list(run(conn).values())
    kept = [e for e in results if e.kept]
    assert len(kept) >= 0.9 * len(results)
    wins = [e.abnormal["d0"] for e in kept if e.event_type == "ORDER_WIN"]
    placebo = [e.abnormal["d0"] for e in kept if e.event_type == "PRESS_RELEASE"]
    assert statistics.mean(wins) == pytest.approx(EFFECT, abs=0.006)
    assert statistics.mean(placebo) == pytest.approx(0.0, abs=0.006)
    for e in kept:
        assert e.net["d0"] == pytest.approx(e.abnormal["d0"] - 0.005)  # type: ignore[operator]
        assert e.period in ("train", "test")


class TestEntryRule:
    def test_strictly_after_availability(self) -> None:
        cal = TradingCalendar(sessions=SESSIONS, holidays={HOLIDAY: "Republic Day"})
        thu = date(2024, 1, 25)
        assert entry_session(cal, ist_datetime(thu, time(9, 14, 59))) == thu
        assert entry_session(cal, ist_datetime(thu, time(9, 15))) == date(
            2024, 1, 29
        )  # Fri holiday
        assert entry_session(cal, ist_datetime(thu, time(23, 59))) == date(2024, 1, 29)

    def test_later_availability_never_enters_earlier(self) -> None:
        """Leak property: entry is monotone in availability."""
        cal = TradingCalendar(sessions=SESSIONS, holidays={HOLIDAY: "Republic Day"})
        rng = random.Random(3)
        base = datetime(2023, 12, 1, tzinfo=IST)
        for _ in range(2000):
            t = base + timedelta(minutes=rng.randrange(0, 60 * 24 * 90))
            later = t + timedelta(minutes=rng.randrange(0, 60 * 24 * 5))
            first, second = entry_session(cal, t), entry_session(cal, later)
            assert first is not None and second is not None and second >= first

    def test_minute_precision_filings_wait_for_the_end_of_the_minute(self) -> None:
        old = type("Row", (), {})()
        old.available_at = old.event_ts = ist_datetime(date(2020, 3, 2), time(9, 14))
        old.exch_disseminated_ts = None
        assert effective_availability(old, 60) == ist_datetime(date(2020, 3, 2), time(9, 15))
        new = type("Row", (), {})()
        new.available_at = new.event_ts = ist_datetime(date(2024, 3, 1), time(9, 14))
        new.exch_disseminated_ts = new.event_ts
        assert effective_availability(new, 60) == new.available_at


def test_filters_each_get_their_reason(conn: Connection, store: RawStore) -> None:
    symbols = ["OK", "THIN", "LOCK", "GAP", "PENNY", "DUP"]
    m = Market(conn, store)
    m.list_stocks(symbols)
    day = SESSIONS[40]
    entry = SESSIONS[41]
    m.turnover_rs["THIN"] = 1e6  # Rs 10 lakh median turnover
    m.overrides[("LOCK", entry)] = {"high": 120.0, "low": 120.0}
    m.overrides[("GAP", entry)] = {
        "open": 125.0,
        "prev_close": 100.0,
        "high": 126.0,
        "low": 120.0,
        "close": 124.0,
    }
    m.overrides[("PENNY", entry)] = {
        "prev_close": 5.0,
        "open": 5.0,
        "close": 5.1,
        "high": 5.2,
        "low": 4.9,
    }
    m.write_prices(symbols)
    ids = {s: m.filing(s, "ORDER_WIN", after_close(day)) for s in symbols}
    dup = m.filing("DUP", "ORDER_WIN", after_close(SESSIONS[43]))  # 2 sessions after the first
    unlinked = m.filing("OK", "PRESS_RELEASE", after_close(day), linked=False)
    excluded = m.filing(
        "OK", "ORDER_WIN", after_close(day), category="Daily Buy Back of equity shares"
    )
    early = m.filing("OK", "ORDER_WIN", after_close(date(2023, 11, 28)))  # enters before start
    results = run(conn)
    assert results[ids["OK"]].kept
    assert results[ids["THIN"]].filter_reason == "illiquid"
    assert results[ids["LOCK"]].filter_reason == "locked_entry"
    assert results[ids["GAP"]].filter_reason == "circuit_gap"
    assert results[ids["PENNY"]].filter_reason == "low_price"
    assert results[dup].filter_reason == "duplicate"
    assert results[unlinked].filter_reason == "unlinked"
    assert results[excluded].filter_reason == "excluded_category"
    assert results[early].filter_reason == "outside_window"
    counts = filter_counts(results.values())
    assert counts["ORDER_WIN"]["illiquid"] == 1 and counts["PRESS_RELEASE"] == {"unlinked": 1}


def test_bonus_inside_the_window_is_adjusted(conn: Connection, store: RawStore) -> None:
    m = Market(conn, store)
    m.list_stocks(["BONUS"])
    day, entry = SESSIONS[50], SESSIONS[51]
    ex_date = SESSIONS[53]  # inside d3
    m.write_prices(["BONUS"])
    # Halve every bar from the ex-date on, as a real 1:1 bonus does.
    from sqlalchemy import update

    from gats.db.schema import eod_prices

    conn.execute(
        update(eod_prices)
        .where(eod_prices.c.symbol == "BONUS", eod_prices.c.trade_date >= ex_date)
        .values(
            open=eod_prices.c.open / 2,
            high=eod_prices.c.high / 2,
            low=eod_prices.c.low / 2,
            close=eod_prices.c.close / 2,
        )
    )
    store_actions(
        conn,
        [
            CorporateActionRecord(
                "BONUS",
                "EQ",
                None,
                None,
                "Bonus 1:1",
                ex_date,
                None,
                10.0,
                "BONUS",
                2.0,
                None,
                False,
            )
        ],
        fetched_at=datetime(2024, 4, 1, tzinfo=UTC),
        raw_doc_id=m.doc(),
        parser_version="v",
    )
    ann = m.filing("BONUS", "ORDER_WIN", after_close(day))
    event = run(conn)[ann]
    assert event.kept and event.entry_date == entry
    assert event.ret["d3"] is not None and abs(event.ret["d3"]) < 0.15  # not a fake -50%


def test_future_prices_cannot_change_a_finished_trade(conn: Connection, store: RawStore) -> None:
    """Leak test: rewriting every price after an event's last exit leaves
    its results unchanged."""
    m = Market(conn, store)
    m.list_stocks(["A1"])
    m.write_prices(["A1"])
    ann = m.filing("A1", "ORDER_WIN", after_close(SESSIONS[45]))
    before = run(conn)[ann]
    last_exit = SESSIONS[SESSIONS.index(before.entry_date) + 3]  # type: ignore[arg-type]
    from sqlalchemy import update

    from gats.db.schema import eod_prices, index_eod

    conn.execute(
        update(eod_prices)
        .where(eod_prices.c.trade_date > last_exit)
        .values(close=eod_prices.c.close * 3, open=eod_prices.c.open * 3)
    )
    conn.execute(
        update(index_eod)
        .where(index_eod.c.trade_date > last_exit)
        .values(close=index_eod.c.close * 3)
    )
    after = run(conn)[ann]
    assert (after.ret, after.abnormal, after.entry_date) == (
        before.ret,
        before.abnormal,
        before.entry_date,
    )


def test_filings_not_yet_available_are_invisible(conn: Connection, store: RawStore) -> None:
    m = Market(conn, store)
    m.list_stocks(["V1"])
    m.write_prices(["V1"])
    early = m.filing("V1", "ORDER_WIN", after_close(SESSIONS[30]))
    late = m.filing("V1", "PRESS_RELEASE", after_close(SESSIONS[60]))
    seen = run(conn, at=after_close(SESSIONS[40]))
    assert early in seen and late not in seen


def test_records_and_parquet(conn: Connection, store: RawStore, tmp_path: Any) -> None:
    m = Market(conn, store)
    m.list_stocks(["P1"])
    m.write_prices(["P1"])
    m.filing("P1", "ORDER_WIN", after_close(SESSIONS[45]))
    rows = to_records(run(conn).values(), ["d0", "d1", "d3"])
    assert {"net_d0", "abnormal_d3", "bench_d1", "ret_d0", "filter_reason"} <= set(rows[0])
    path = tmp_path / "events.parquet"
    write_parquet(rows, path)
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    assert table.num_rows == 1 and "net_d0" in table.column_names
