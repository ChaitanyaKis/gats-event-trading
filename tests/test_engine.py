"""Backtest engine and SimBroker (T6.3): one test per fill rule, plus a golden run.

All market data here is SYNTHETIC: small, hand-made bars that isolate a rule.
Timing to keep in mind: a decision at a bar's close (start + 1 min) reaches
the market 5 s later, when the next bar has already started, so it fills in
the bar after that.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest

from gats.backtest.costs import CostModel
from gats.backtest.engine import (
    BacktestResult,
    Engine,
    EngineConfig,
    EngineState,
    Execution,
    Order,
)
from gats.strategy.base import (
    BarEvent,
    Context,
    MarketEvent,
    Signal,
    Strategy,
    StrategyParams,
)
from gats.strategy.order_win import OrderWinDrift
from gats.timeutil import ist_datetime

ROOT = Path(__file__).parents[1]
COSTS = CostModel.load(ROOT / "configs" / "costs" / "india_equity.yaml")
A, B = "NSE_EQ|INE000000A01", "NSE_EQ|INE000000B01"
D1, D2 = date(2026, 7, 14), date(2026, 7, 15)
GOLDEN = ROOT / "tests" / "fixtures" / "golden" / "backtest_tiny.json"
Row = tuple[float, float, float, float, int]
REF: Row = (100, 100, 100, 100, 10_000)
NEUTRAL: Row = (100, 100.4, 99.6, 100, 10_000)


def at(hh: int, mm: int, day: date = D1, ss: int = 0) -> datetime:
    return ist_datetime(day, time(hh, mm, ss))


def bars(key: str, start: datetime, rows: Sequence[Row]) -> list[BarEvent]:
    return [
        BarEvent(key, start + timedelta(minutes=i), o, h, low, c, v)
        for i, (o, h, low, c, v) in enumerate(rows)
    ]


def flat(price: float, n: int, volume: int = 10_000) -> list[Row]:
    return [(price, price + 0.5, price - 0.5, price, volume)] * n


class Scripted(Strategy):  # type: ignore[type-arg]
    """Emits planned signals: on bars by (key, bar start), on events by id."""

    name: ClassVar[str] = "scripted"
    params_model: ClassVar[type[StrategyParams]] = StrategyParams

    def __init__(
        self,
        on_bars: dict[tuple[str, datetime], list[Signal]] | None = None,
        on_events: dict[int, list[Signal]] | None = None,
    ) -> None:
        super().__init__(StrategyParams(), "scripted:v0")
        self.on_bars = on_bars or {}
        self.on_events = on_events or {}

    def on_event(self, event: MarketEvent, ctx: Context) -> list[Signal]:
        return self.on_events.get(event.event_id, [])

    def on_bar(self, bar: BarEvent, ctx: Context) -> list[Signal]:
        return self.on_bars.get((bar.instrument_key, bar.start), [])


def buy(key: str = A, when: datetime | None = None, **kw: Any) -> Signal:
    product = kw.pop("product", "intraday")
    return Signal(key, "buy", product, "test buy", when or at(10, 0), **kw)


def sell(key: str = A, when: datetime | None = None, **kw: Any) -> Signal:
    product = kw.pop("product", "intraday")
    return Signal(key, "sell", product, "test sell", when or at(10, 0), closes=True, **kw)


def run(
    strategy: Strategy, items: Sequence[MarketEvent | BarEvent], **config: Any
) -> BacktestResult:  # type: ignore[type-arg]
    cfg = {"initial_cash": 1_000_000.0, "notional_per_trade": 100_000.0} | config
    return Engine(strategy, COSTS, EngineConfig(**cfg)).run(items)


def buys(result: BacktestResult) -> list[Execution]:
    return [e for e in result.executions if e.side == "buy"]


def slipped(price: float, quantity: int, volume: int) -> float:
    return price * (1 + (5 + 50 * quantity / volume) / 1e4)


def test_latency_means_a_later_bar_never_the_running_one() -> None:
    day = bars(A, at(10, 0), [REF, NEUTRAL, (101.5, 102, 101, 101.8, 10_000)])
    (fill,) = buys(run(Scripted({(A, at(10, 0)): [buy(quantity=10)]}), day))
    # Decided at 10:01:00, at the market 10:01:05: the 10:01 bar was already running.
    assert fill.at == at(10, 2)
    assert fill.price == pytest.approx(slipped(101.5, 10, 10_000))


def test_protection_band_turns_a_gap_into_no_fill() -> None:
    gap: Row = (103, 104, 102.5, 103, 10_000)
    day = bars(A, at(10, 0), [REF, NEUTRAL, gap, *flat(101.9, 40)])
    result = run(Scripted({(A, at(10, 0)): [buy(quantity=10)]}), day, order_ttl_minutes=1)
    assert result.executions == []  # 103 is beyond 100 x 1.02; a minute later the order expired
    (order,) = result.orders
    assert (order.status, order.note) == ("expired", "time to live ran out")
    assert order.limit == pytest.approx(102)


def test_limit_touched_inside_the_bar_fills_at_the_limit() -> None:
    day = bars(A, at(10, 0), [REF, NEUTRAL, (103, 104, 101, 103, 10_000)])
    (fill,) = buys(run(Scripted({(A, at(10, 0)): [buy(quantity=10)]}), day))
    assert fill.price == pytest.approx(102.0)


def test_participation_cap_spreads_a_large_order() -> None:
    day = bars(A, at(10, 0), [(100, 100.5, 99.5, 100, 1_000)] * 6)
    result = run(Scripted({(A, at(10, 0)): [buy(quantity=250)]}), day)
    assert [e.quantity for e in buys(result)] == [100, 100, 50]
    assert result.orders[0].status == "filled"
    assert buys(result)[0].price == pytest.approx(slipped(100, 100, 1_000))  # 10%: 10 bps


def test_no_buy_into_an_upper_circuit() -> None:
    rows: list[Row] = [REF, NEUTRAL, (110, 110, 110, 110, 50), (109, 110, 108, 109, 10_000)]
    engine = Engine(
        Scripted({(A, at(10, 0)): [buy(quantity=10)]}),
        COSTS,
        EngineConfig(initial_cash=1e6, notional_per_trade=1e5, protection_band=0.2),
        circuit_limits=lambda key, day: (90.0, 110.0),
    )
    (fill,) = buys(engine.run(bars(A, at(10, 0), rows)))
    assert fill.at == at(10, 3)  # the bar locked at 110 was skipped


def test_flat_bar_at_the_day_high_counts_as_locked() -> None:
    rows: list[Row] = [
        (100, 100.5, 99.5, 100, 10_000),
        NEUTRAL,
        (101, 101, 101, 101, 10),
        (100.5, 101, 100, 100.5, 10_000),
    ]
    (fill,) = buys(run(Scripted({(A, at(10, 0)): [buy(quantity=10)]}), bars(A, at(10, 0), rows)))
    assert fill.at == at(10, 3)  # 10:02 was flat at the day's high


def test_intraday_positions_are_squared_off() -> None:
    day = bars(A, at(15, 17), flat(100, 5))
    result = run(Scripted({(A, at(15, 17)): [buy(quantity=10)]}), day)
    assert [(e.side, e.at, e.reason) for e in result.executions] == [
        ("buy", at(15, 19), "test buy"),
        ("sell", at(15, 20), "auto square-off"),
    ]
    assert result.positions == {}


def test_no_new_intraday_position_after_square_off_time() -> None:
    day = bars(A, at(15, 17), flat(100, 5))
    result = run(Scripted({(A, at(15, 19)): [buy(quantity=10)]}), day)
    assert result.executions == []
    assert result.orders[0].note == "square-off time: no new intraday positions"


def test_open_intraday_position_is_closed_when_the_data_ends() -> None:
    result = run(Scripted({(A, at(11, 0)): [buy(quantity=10)]}), bars(A, at(11, 0), flat(100, 3)))
    assert result.executions[-1].reason == "intraday position open at the end of the data"
    assert result.positions == {}


def test_delivery_is_t_plus_one() -> None:
    day1 = bars(A, at(10, 0, D1), flat(100, 3))
    day2 = bars(A, at(10, 0, D2), flat(105, 3))
    plan = {
        (A, at(10, 0, D1)): [buy(quantity=10, product="delivery")],
        (A, at(10, 2, D1)): [sell(product="delivery")],  # bought this morning: refused
        (A, at(10, 0, D2)): [sell(product="delivery", when=at(10, 0, D2))],
    }
    result = run(Scripted(plan), day1 + day2)
    assert [o.note for o in result.orders if o.status == "rejected"] == ["T+1: bought today"]
    sold = [e for e in result.executions if e.side == "sell"]
    assert len(sold) == 1 and sold[0].at.date() == D2 and sold[0].charges.dp == 20.0
    bought = result.executions[0]
    proceeds = sold[0].price * 10 - sold[0].charges.total
    assert result.cash == pytest.approx(1e6 - bought.price * 10 - bought.charges.total)  # unsettled
    assert result.equity[D2] == pytest.approx(result.cash + proceeds)


def test_after_hours_signal_fills_at_the_next_open() -> None:
    evening = MarketEvent(1, 1, A, "ORDER_WIN", at(18, 0, D1))
    day1 = bars(A, at(15, 28, D1), flat(100, 2))
    day2 = bars(A, at(9, 15, D2), [(104, 105, 103, 104, 10_000), *flat(104, 2)])
    strategy = Scripted(on_events={1: [buy(when=at(18, 0, D1), quantity=10)]})
    result = run(strategy, [*day1, evening, *day2], protection_band=0.05)
    (first,) = buys(result)
    assert first.at == at(9, 15, D2)  # waited overnight; its time to live started at the open
    assert first.price == pytest.approx(slipped(104, 10, 10_000))


def test_not_enough_cash_and_risk_vetoes() -> None:
    day = bars(A, at(10, 0), flat(100, 4))
    poor = run(Scripted({(A, at(10, 0)): [buy(quantity=50)]}), day, initial_cash=2_000.0)
    assert sum(e.quantity for e in buys(poor)) == 19  # what Rs 2,000 buys, with charges

    class NoNewPositions:
        def refuse(self, order: Order, state: EngineState) -> str | None:
            return None if order.signal.closes else "new positions are paused"

    engine = Engine(
        Scripted({(A, at(10, 0)): [buy(quantity=10)]}),
        COSTS,
        EngineConfig(initial_cash=1e6, notional_per_trade=1e5),
        risk=NoNewPositions(),
    )
    result = engine.run(day)
    assert result.executions == [] and result.orders[0].note == "risk: new positions are paused"


def test_sizing_and_charges_reconcile_cash() -> None:
    day = bars(A, at(10, 0), flat(100, 5))
    plan = {(A, at(10, 0)): [buy()], (A, at(10, 2)): [sell(when=at(10, 3))]}
    result = run(Scripted(plan), day)
    assert [e.quantity for e in result.executions] == [1000, 1000]  # Rs 1 lakh at Rs 100
    flows = sum(
        (-1 if e.side == "buy" else 1) * e.price * e.quantity - e.charges.total
        for e in result.executions
    )
    assert result.cash == pytest.approx(1_000_000 + flows)
    assert result.charges == pytest.approx(sum(e.charges.total for e in result.executions))


# --- golden: S1 on a tiny two-day market -------------------------------------------


def golden_items() -> list[MarketEvent | BarEvent]:
    rising: list[Row] = [
        (100 + i * 0.1, 100.6 + i * 0.1, 99.8 + i * 0.1, 100.3 + i * 0.1, 20_000) for i in range(80)
    ]
    b_next: list[Row] = [(256, 258, 255, 257, 30_000)] + [
        (257 + i * 0.2, 258 + i * 0.2, 256 + i * 0.2, 257.5 + i * 0.2, 8_000) for i in range(70)
    ]
    events = [
        MarketEvent(1, 1, A, "ORDER_WIN", at(10, 5, D1, 30), {"amount_vs_revenue": 0.5}),
        MarketEvent(2, 2, B, "ORDER_WIN", at(18, 10, D1), {"amount_vs_revenue": 0.3}),
        MarketEvent(3, 1, A, "ORDER_WIN", at(10, 50, D1), {"amount_vs_revenue": 0.02}),  # small
    ]
    return [
        *bars(A, at(10, 0, D1), rising),
        *bars(B, at(15, 25, D1), flat(250, 5, 5_000)),
        *bars(B, at(9, 15, D2), b_next),
        *events,
    ]


def summary(result: BacktestResult) -> dict[str, Any]:
    return {
        "executions": [
            {k: v for k, v in asdict(e).items() if k != "charges"}
            | {"charges": round(e.charges.total, 4)}
            for e in result.executions
        ],
        "orders": [
            [o.order_id, o.signal.side, o.quantity, o.filled, o.status, o.note]
            for o in result.orders
        ],
        "cash": round(result.cash, 4),
        "equity": {d.isoformat(): round(v, 4) for d, v in result.equity.items()},
    }


def test_golden_tiny_backtest() -> None:
    """S1 end to end. Regenerate deliberately: GATS_UPDATE_GOLDEN=1 pytest -k golden."""
    strategy = OrderWinDrift.from_yaml(ROOT / "configs" / "strategies" / "order_win_drift.yaml")
    got = json.loads(json.dumps(summary(run(strategy, golden_items())), default=str))
    if os.environ.get("GATS_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(got, indent=1) + "\n", encoding="utf-8", newline="\n")
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_a_repeated_exit_is_not_sent_twice() -> None:
    day = bars(A, at(10, 0), flat(100, 8))
    plan = {
        (A, at(10, 0)): [buy(quantity=10)],
        (A, at(10, 2)): [sell(when=at(10, 3))],
        (A, at(10, 3)): [sell(when=at(10, 4))],  # the first exit has not filled yet
    }
    result = run(Scripted(plan), day)
    assert result.duplicates == 1
    assert [o.signal.side for o in result.orders] == ["buy", "sell"]
