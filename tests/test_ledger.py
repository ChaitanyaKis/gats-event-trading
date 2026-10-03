"""Ledger and metrics (T6.5): FIFO trades, reconciliation to the paisa, the ratios."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from gats.backtest.costs import Charges
from gats.backtest.engine import Execution
from gats.backtest.ledger import (
    build_trades,
    capacity_multiple,
    cash_flow,
    daily_returns,
    max_drawdown,
    reconciliation_gap,
    sharpe,
    sortino,
    summarize,
)
from gats.strategy.base import Product, Side
from gats.strategy.order_win import OrderWinDrift
from tests.test_engine import (
    D1,
    D2,
    ROOT,
    A,
    Scripted,
    at,
    bars,
    buy,
    flat,
    golden_items,
    run,
    sell,
)


def fill(
    side: Side,
    quantity: int,
    price: float,
    when: datetime,
    charges: float,
    product: Product = "intraday",
    bar_volume: int = 10_000,
) -> Execution:
    paid = Charges(charges, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return Execution(1, A, side, product, quantity, price, when, paid, f"{side} reason", bar_volume)


def test_fifo_trades_share_the_charges_pro_rata() -> None:
    fills = [
        fill("buy", 100, 10.0, at(10, 0), charges=2.0),
        fill("buy", 50, 12.0, at(10, 5), charges=1.0),
        fill("sell", 120, 15.0, at(11, 0), charges=3.0),
    ]
    closed, still_open = build_trades(fills)
    assert [(t.quantity, t.entry_price, t.gross) for t in closed] == [
        (100, 10.0, 500.0),
        (20, 12.0, 60.0),
    ]
    assert closed[0].charges == pytest.approx(2.0 + 3.0 * 100 / 120)
    assert closed[1].charges == pytest.approx(1.0 * 20 / 50 + 3.0 * 20 / 120)
    (lot,) = still_open
    assert (lot.quantity, lot.entry_price, lot.charges) == (30, 12.0, pytest.approx(0.6))
    assert abs(reconciliation_gap(fills)) < 0.005


def test_selling_what_is_not_held_is_an_error() -> None:
    with pytest.raises(ValueError, match="without holding"):
        build_trades([fill("sell", 10, 15.0, at(11, 0), charges=1.0)])


def test_the_golden_run_reconciles_to_the_paisa() -> None:
    strategy = OrderWinDrift.from_yaml(ROOT / "configs" / "strategies" / "order_win_drift.yaml")
    result = run(strategy, golden_items())
    assert abs(reconciliation_gap(result.executions)) < 0.005
    assert result.cash + result.unsettled == pytest.approx(
        result.initial_cash + cash_flow(result.executions), abs=0.005
    )
    metrics = summarize(result, participation=0.10)
    assert metrics.trades == 2 and metrics.win_rate == 1.0
    assert metrics.net_pnl == pytest.approx(result.cash - result.initial_cash, abs=0.005)
    assert metrics.charges == pytest.approx(result.charges)
    assert metrics.total_return == pytest.approx(0.0112537, abs=1e-6)
    assert metrics.max_drawdown == 0.0 and metrics.profit_factor is None  # no losing trade
    assert metrics.tax == {"speculative business income (intraday)": pytest.approx(metrics.net_pnl)}


def test_reconciles_with_shares_still_held_and_unsettled_cash() -> None:
    day1 = bars(A, at(10, 0, D1), flat(100, 4, volume=1_000))
    day2 = bars(A, at(10, 0, D2), flat(105, 4, volume=1_000))
    plan = {
        (A, at(10, 0, D1)): [buy(quantity=150, product="delivery")],  # fills over two bars
        (A, at(10, 0, D2)): [sell(quantity=60, product="delivery", when=at(10, 0, D2))],
    }
    result = run(Scripted(plan), day1 + day2)
    assert result.positions[A].quantity == 90 and result.unsettled > 0
    assert abs(reconciliation_gap(result.executions)) < 0.005
    assert result.cash + result.unsettled == pytest.approx(
        result.initial_cash + cash_flow(result.executions), abs=0.005
    )
    closed, still_open = build_trades(result.executions)
    assert sum(t.quantity for t in closed) == 60 and sum(lot.quantity for lot in still_open) == 90


def test_drawdown_is_peak_to_trough() -> None:
    assert max_drawdown([100, 110, 99, 105, 120, 90, 95]) == (
        pytest.approx(0.25),
        pytest.approx(30),
    )
    assert max_drawdown([100, 101, 102]) == (0.0, 0.0)


def test_sharpe_and_sortino() -> None:
    assert sharpe([0.02, -0.01]) == pytest.approx(3.74166, abs=1e-4)
    assert sortino([0.02, -0.01]) == pytest.approx(11.2250, abs=1e-3)
    assert sortino([0.01, 0.03]) is None  # no down day: no downside risk to divide by
    assert sharpe([0.01]) is None and sharpe([0.01, 0.01]) is None


def test_idle_sessions_count_as_flat_days() -> None:
    d3 = D2 + timedelta(days=1)
    equity = {D1: 101.0, d3: 102.0}
    assert daily_returns(equity, 100.0) == pytest.approx([0.01, 102 / 101 - 1])
    assert daily_returns(equity, 100.0, [D1, D2, d3]) == pytest.approx([0.01, 0.0, 102 / 101 - 1])


def test_tax_categories_are_informational_buckets() -> None:
    def trade(product: Product, opened: date, closed: date) -> str:
        fills = [
            fill("buy", 10, 100.0, at(10, 0, opened), 1.0, product),
            fill("sell", 10, 110.0, at(11, 0, closed), 1.0, product),
        ]
        return build_trades(fills)[0][0].tax_category

    assert trade("intraday", D1, D1).startswith("speculative")
    assert trade("delivery", D1, D1 + timedelta(days=3)).startswith("short-term")
    assert trade("delivery", date(2025, 7, 14), date(2026, 7, 14)).startswith(
        "short-term"
    )  # 12 months
    assert trade("delivery", date(2025, 7, 14), date(2026, 7, 15)).startswith("long-term")
    assert trade("delivery", date(2024, 2, 29), date(2025, 3, 1)).startswith("long-term")


def test_capacity_is_the_headroom_under_the_participation_cap() -> None:
    fills = [
        fill("buy", 100, 10.0, at(10, 0), 1.0, bar_volume=10_000),  # could be 10x larger
        fill("buy", 500, 10.0, at(10, 1), 1.0, bar_volume=2_000),  # the cap already binds: 0.4x
        fill("sell", 600, 10.0, at(11, 0), 1.0, bar_volume=50),
    ]
    assert capacity_multiple(fills, participation=0.10) == pytest.approx(0.4)
    assert capacity_multiple([], participation=0.10) is None
