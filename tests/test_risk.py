"""The risk engine (T6.4): a test for every rule, the veto path, and the engine using it."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from gats.backtest.engine import Engine, EngineConfig
from gats.risk.engine import Exposure, OrderIntent, RiskEngine, RiskLimits
from gats.strategy.base import Signal
from gats.timeutil import ist_datetime
from tests.test_engine import COSTS, Scripted, at, bars, flat

CONFIG = Path(__file__).parents[1] / "configs" / "risk.yaml"
KEY = "NSE_EQ|INE002A01018"
NOW = ist_datetime(date(2026, 7, 14), time(11, 0))


def engine(
    tmp_path: Path, liquidity: float | None = 5e7, flags: Any = frozenset(), **limits: Any
) -> RiskEngine:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) | limits
    return RiskEngine(
        RiskLimits.model_validate(raw),
        "test",
        liquidity=lambda key, day: liquidity,
        flags=lambda key, day: flags,
        root=tmp_path,
    )


def order(
    quantity: int = 100, price: float = 500.0, closes: bool = False, key: str = KEY
) -> OrderIntent:
    return OrderIntent(key, "sell" if closes else "buy", "intraday", quantity, price, closes, NOW)


def account(**changes: Any) -> Exposure:
    base: dict[str, Any] = {
        "equity": 1_000_000.0,
        "day_start_equity": 1_000_000.0,
        "last_data_at": {KEY: NOW - timedelta(seconds=30)},
    }
    return Exposure(**(base | changes))


def test_the_real_limits_load_and_a_normal_order_passes(tmp_path: Path) -> None:
    loaded = RiskEngine.load(CONFIG, liquidity=lambda k, d: 5e7, flags=lambda k, d: frozenset())
    assert loaded.version.startswith("risk-v1+")
    assert loaded.limits.max_orders_per_second <= 10  # NSE/INVG/67858
    assert loaded.check(order(), account()) is None
    assert engine(tmp_path).check(order(), account()) is None


def test_order_rate_binds_entries_and_exits(tmp_path: Path) -> None:
    busy = account(orders_this_second=5)
    assert "order rate" in (engine(tmp_path).check(order(), busy) or "")
    assert "order rate" in (engine(tmp_path).check(order(closes=True), busy) or "")
    assert engine(tmp_path).check(order(), account(orders_this_second=4)) is None


def test_kill_switch_file_stops_new_entries(tmp_path: Path) -> None:
    risk = engine(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "KILL").write_text("stop", encoding="utf-8")
    assert "kill switch" in (risk.check(order(), account()) or "")
    assert risk.check(order(closes=True), account()) is None  # getting out stays possible


def test_daily_loss_limit(tmp_path: Path) -> None:
    down = account(equity=979_000.0)  # 2.1% down today
    assert "daily loss" in (engine(tmp_path).check(order(), down) or "")
    assert engine(tmp_path).check(order(), account(equity=985_000.0)) is None


def test_open_position_limit_counts_working_buys(tmp_path: Path) -> None:
    full = account(positions={"a": 1e4, "b": 1e4, "c": 1e4}, buying={"d": 1e4, "e": 1e4})
    assert "open positions" in (engine(tmp_path).check(order(), full) or "")
    adding = account(positions={KEY: 1e4, "b": 1e4, "c": 1e4}, buying={"d": 1e4, "e": 1e4})
    assert engine(tmp_path).check(order(), adding) is None  # no new name


def test_position_size_against_equity(tmp_path: Path) -> None:
    assert "position size" in (engine(tmp_path).check(order(quantity=300), account()) or "")
    assert engine(tmp_path).check(order(quantity=200), account()) is None  # exactly 10%


def test_symbol_cap_adds_what_is_held_and_being_bought(tmp_path: Path) -> None:
    held = account(positions={KEY: 120_000.0}, buying={KEY: 40_000.0})
    assert "symbol cap" in (engine(tmp_path).check(order(), held) or "")  # 210,000 > 200,000
    assert engine(tmp_path).check(order(quantity=80), held) is None  # exactly 200,000


def test_liquidity_floor_and_unknown_liquidity(tmp_path: Path) -> None:
    assert "liquidity" in (engine(tmp_path, liquidity=5e6).check(order(), account()) or "")
    unknown = engine(tmp_path, liquidity=None).check(order(), account())
    assert unknown is not None and "unknown" in unknown  # fails closed


@pytest.mark.parametrize(
    ("flags", "unknown_flags", "refused"),
    [
        (frozenset({"ASM"}), "refuse", "surveillance: ASM"),
        (frozenset({"T2T", "GSM"}), "refuse", "surveillance: GSM, T2T"),
        (frozenset({"OTHER"}), "refuse", None),
        (None, "refuse", "surveillance status unknown"),
        (None, "allow", None),  # backtests before surveillance history exists
    ],
)
def test_surveillance_block(
    tmp_path: Path, flags: Any, unknown_flags: str, refused: str | None
) -> None:
    risk = engine(tmp_path, flags=flags, unknown_flags=unknown_flags)
    assert risk.check(order(), account()) == refused


def test_stale_data_halts_entries_in_market_hours(tmp_path: Path) -> None:
    old = account(last_data_at={KEY: NOW - timedelta(minutes=5)})
    assert "stale data" in (engine(tmp_path).check(order(), old) or "")
    assert "stale data" in (engine(tmp_path).check(order(), account(last_data_at={})) or "")
    overnight = account(last_data_at={KEY: NOW - timedelta(hours=17)}, in_session=False)
    assert engine(tmp_path).check(order(), overnight) is None  # an order for the next open


def test_exits_pass_every_rule_but_the_order_rate(tmp_path: Path) -> None:
    risk = engine(tmp_path, liquidity=None, flags=frozenset({"ASM"}))
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "KILL").touch()
    bad_day = account(equity=900_000.0, last_data_at={}, positions={str(i): 1e4 for i in range(9)})
    assert risk.check(order(closes=True, quantity=10_000), bad_day) is None


@pytest.mark.parametrize(
    "change",
    [{"max_orders_per_second": 11}, {"max_position_pct_equity": 1.5}, {"leverage": 5}],
)
def test_limits_outside_the_rules_are_rejected(change: dict[str, Any]) -> None:
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) | change
    with pytest.raises(ValidationError):
        RiskLimits.model_validate(raw)


# --- the backtest engine obeys it -------------------------------------------------------


def backtest(risk: RiskEngine, plan: dict[tuple[str, datetime], list[Signal]]) -> Any:
    day = bars(KEY, at(10, 0), flat(100, 6))
    cfg = EngineConfig(initial_cash=1_000_000.0, notional_per_trade=50_000.0)
    return Engine(Scripted(plan), COSTS, cfg, risk=risk).run(day)


def buy(quantity: int = 10) -> Signal:
    return Signal(KEY, "buy", "intraday", "test buy", at(10, 1), quantity=quantity)


def test_engine_orders_are_vetoed_with_the_reason(tmp_path: Path) -> None:
    thin = backtest(engine(tmp_path, liquidity=2e6), {(KEY, at(10, 0)): [buy()]})
    assert thin.executions == []
    assert thin.orders[0].status == "rejected" and thin.orders[0].note.startswith("risk: liquidity")
    fine = backtest(engine(tmp_path), {(KEY, at(10, 0)): [buy()]})
    assert fine.executions[0].side == "buy"


def test_engine_respects_the_order_rate(tmp_path: Path) -> None:
    burst = backtest(engine(tmp_path), {(KEY, at(10, 0)): [buy(1) for _ in range(7)]})
    notes = [o.note for o in burst.orders]
    assert sum(n.startswith("risk: order rate") for n in notes) == 2  # the 6th and 7th that second
    assert sum(o.status != "rejected" for o in burst.orders) == 5
