"""Backtest feed, risk lookups and the G2 report (T6.8 groundwork)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Connection, Engine

from gats.backtest import feed
from gats.backtest.engine import EngineConfig
from gats.backtest.ledger import Metrics
from gats.backtest.report import g2_checks, g2_verdict, render
from gats.backtest.runner import run_backtest
from gats.db.schema import extractions, surveillance_versions
from gats.marketdata.bars import write_month
from gats.marketdata.windows import event_windows
from gats.pit import AsOf
from gats.rawstore import RawStore
from gats.risk.engine import RiskEngine
from gats.sources.upstox import Bar
from gats.strategy.order_win import OrderWinDrift
from gats.timeutil import ist_datetime
from tests.test_engine import COSTS, ROOT
from tests.test_event_study import VERSION, Market

NOW = datetime(2024, 4, 1, tzinfo=UTC)
DAY = date(2024, 1, 10)


def world(conn: Connection, store: RawStore, bars_dir: Path) -> tuple[Market, AsOf, list[Any]]:
    """Two listed stocks with daily prices, one order win at 11:00 on AAA,
    its extracted facts, and AAA's minute bars for that morning."""
    market = Market(conn, store)
    market.list_stocks(["AAA", "BBB"])
    market.write_prices(["AAA", "BBB"])
    filing = market.filing("AAA", "ORDER_WIN", ist_datetime(DAY, time(11, 0)))
    conn.execute(
        extractions.insert().values(
            announcement_id=filing,
            extractor_version="cascade:test",
            event_type="ORDER_WIN",
            method="rules_annexure",
            fields={"amount_inr": 5e8, "amount_vs_revenue": 0.5},
            amount_inr=5e8,
            confidence=0.9,
            created_at=NOW,
        )
    )
    clock = AsOf(conn, NOW)
    windows, _ = event_windows(
        clock,
        event_types={"ORDER_WIN"},
        taxonomy_version=VERSION,
        start=date(2023, 12, 1),
        end=date(2024, 3, 15),
    )
    key = windows[0].instrument_key
    first = ist_datetime(DAY, time(10, 30))
    minute_bars = [
        Bar(key, first + timedelta(minutes=i), 100 + i * 0.05, 100.3 + i * 0.05,
            99.9 + i * 0.05, 100.1 + i * 0.05, 20_000, 0)
        for i in range(150)
    ]  # fmt: skip
    write_month(bars_dir, key, date(2024, 1, 1), minute_bars)
    return market, clock, windows


@pytest.fixture
def conn(engine: Engine) -> Any:
    with engine.begin() as connection:
        yield connection


def test_events_carry_facts_and_bars_come_from_the_files(
    conn: Connection, store: RawStore, tmp_path: Path
) -> None:
    _, clock, windows = world(conn, store, tmp_path)
    facts = clock.extracted_facts([w.announcement_id for w in windows], "cascade:")
    (event,) = feed.market_events(windows, facts)
    assert event.facts == {"amount_inr": 5e8, "amount_vs_revenue": 0.5}
    assert event.available_at == ist_datetime(DAY, time(11, 0))
    bars = feed.bar_events(tmp_path, windows)
    assert len(bars) == 150 and len({b.start for b in bars}) == 150
    assert bars[0].start == ist_datetime(DAY, time(10, 30)) and bars[0].volume == 20_000


def test_facts_of_a_filing_not_yet_available_are_invisible(
    conn: Connection, store: RawStore, tmp_path: Path
) -> None:
    _, clock, windows = world(conn, store, tmp_path)
    ids = [w.announcement_id for w in windows]
    earlier = AsOf(conn, ist_datetime(DAY, time(10, 59)))
    assert earlier.extracted_facts(ids, "cascade:") == {}
    assert clock.extracted_facts(ids, "llm:") == {}  # another extractor's rows are not mixed in


def test_liquidity_uses_only_sessions_before_the_day(
    conn: Connection, store: RawStore, tmp_path: Path
) -> None:
    _, clock, windows = world(conn, store, tmp_path)
    lookups = feed.Lookups(clock, windows)
    key = windows[0].instrument_key
    assert lookups.liquidity(key, DAY) == pytest.approx(5e7)  # the synthetic market's turnover
    assert lookups.liquidity(key, date(2023, 11, 10)) is None  # under 20 sessions of history
    assert lookups.liquidity("NSE_EQ|UNKNOWN", DAY) is None


def test_surveillance_is_unknown_before_its_history_starts(
    conn: Connection, store: RawStore, tmp_path: Path
) -> None:
    market, clock, windows = world(conn, store, tmp_path)
    lookups = feed.Lookups(clock, windows)
    key = windows[0].instrument_key
    assert lookups.flags(key, DAY) is None  # nothing was recorded then
    conn.execute(
        surveillance_versions.insert().values(
            entity_key="LTASM:AAA", valid_from=date(2024, 1, 5), valid_to=date(2024, 1, 20),
            list_name="LTASM", symbol="AAA", available_at=datetime(2024, 1, 5, tzinfo=UTC),
            raw_doc_id=market.doc(), parser_version="v",
        )
    )  # fmt: skip
    assert lookups.flags(key, date(2024, 1, 4)) is None
    assert lookups.flags(key, DAY) == frozenset({"ASM"})
    assert lookups.flags(key, date(2024, 1, 20)) == frozenset()  # off the list again
    assert clock.restrictions_on("BBB", DAY) == frozenset()


def test_end_to_end_from_database_to_report(
    engine: Engine, store: RawStore, tmp_path: Path
) -> None:
    with engine.begin() as conn:
        _, clock, windows = world(conn, store, tmp_path)
        facts = clock.extracted_facts([w.announcement_id for w in windows], "cascade:")
        items = [*feed.market_events(windows, facts), *feed.bar_events(tmp_path, windows)]
        sessions = clock.calendar().trading_days(date(2024, 1, 1), date(2024, 1, 31))
    with engine.begin() as conn:
        lookups = feed.Lookups(AsOf(conn, NOW), windows)
        base = RiskEngine.load(ROOT / "configs" / "risk.yaml")
        risk = RiskEngine(
            base.limits.model_copy(update={"unknown_flags": "allow"}), "test",
            liquidity=lookups.liquidity, flags=lookups.flags,
        )  # fmt: skip
        strategy = OrderWinDrift.from_yaml(ROOT / "configs" / "strategies" / "order_win_drift.yaml")
        config = EngineConfig(initial_cash=1_000_000.0, notional_per_trade=50_000.0)
        result, metrics, run_id = run_backtest(
            engine, strategy, items, costs=COSTS, config=config, data_start=date(2024, 1, 1),
            data_end=date(2024, 1, 31), holdout=False, risk=risk, risk_version=risk.version,
            sessions=sessions, root=ROOT,
        )  # fmt: skip
    assert [e.side for e in result.executions] == [
        "buy",
        "sell",
    ]  # bought the order win, held an hour
    assert result.executions[0].at == ist_datetime(DAY, time(11, 1))
    assert metrics.trades == 1 and metrics.days == len(sessions) and metrics.net_pnl > 0
    checks = g2_checks(
        metrics, holdout=False, deflated_sharpe=None, trials=1, max_drawdown_limit=0.10
    )
    report = render(run_id=run_id, design={"strategy": strategy.version, "engine": {}},
                    window="2024-01", metrics=metrics, checks=checks)  # fmt: skip
    assert "**NOT A G2 RUN**" in report and "| Trades | >= 300 | 1 | NO |" in report
    assert "speculative business income" in report


METRICS = Metrics(
    trades=420, net_pnl=50_000.0, gross_pnl=90_000.0, charges=40_000.0, win_rate=0.55,
    avg_net_per_trade=119.0, profit_factor=1.3, turnover=8e7, turnover_ratio=80.0,
    total_return=0.05, max_drawdown=0.04, max_drawdown_rs=41_000.0, sharpe=1.4, sortino=2.0,
    days=480, capacity_multiple=3.2, reconciliation_gap=0.0,
)  # fmt: skip


@pytest.mark.parametrize(
    ("holdout", "deflated", "metrics", "verdict"),
    [
        (True, 0.97, METRICS, "PASS"),
        (False, 0.97, METRICS, "NOT A G2 RUN"),
        (True, 0.90, METRICS, "FAIL"),  # luck is not ruled out
        (True, None, METRICS, "FAIL"),  # what cannot be computed is not met
        (True, 0.97, replace(METRICS, trades=120), "FAIL"),
        (True, 0.97, replace(METRICS, max_drawdown=0.15), "FAIL"),
        (True, 0.97, replace(METRICS, capacity_multiple=0.4), "FAIL"),
    ],
)
def test_g2_is_judged_mechanically(
    holdout: bool, deflated: float | None, metrics: Metrics, verdict: str
) -> None:
    checks = g2_checks(
        metrics, holdout=holdout, deflated_sharpe=deflated, trials=7, max_drawdown_limit=0.10
    )
    assert g2_verdict(checks) == verdict
