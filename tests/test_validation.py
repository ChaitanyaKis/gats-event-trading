"""Backtest validation (T6.7): splits, the deflated Sharpe ratio, holdout
discipline, and a leak detector that must catch a planted look-ahead."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine as Database
from sqlalchemy import select

from gats.backtest.engine import BacktestResult, Engine, EngineConfig
from gats.backtest.runner import HoldoutError, design, run_backtest
from gats.backtest.validation import (
    Item,
    Split,
    deflated_sharpe,
    expected_max_sharpe,
    find_leak,
    probabilistic_sharpe,
    registry_trials,
    walk_forward,
)
from gats.db.schema import experiments
from gats.research.registry import experiment
from gats.strategy.base import BarEvent, Context, Signal
from gats.strategy.order_win import OrderWinDrift
from tests.test_engine import COSTS, D1, D2, ROOT, Scripted, golden_items

CFG = EngineConfig(initial_cash=1_000_000.0, notional_per_trade=100_000.0)
S1 = ROOT / "configs" / "strategies" / "order_win_drift.yaml"
DAYS = [date(2026, 1, 1) + timedelta(days=i) for i in range(20)]
NORMAL = statistics.NormalDist()


class TestWalkForward:
    def test_rolling_windows_never_train_on_the_future(self) -> None:
        splits = walk_forward(DAYS, train=10, test=3, embargo=1)
        assert splits == [
            Split(DAYS[0], DAYS[9], DAYS[11], DAYS[13]),
            Split(DAYS[3], DAYS[12], DAYS[14], DAYS[16]),
            Split(DAYS[6], DAYS[15], DAYS[17], DAYS[19]),
        ]
        assert all(s.train_end < s.test_start for s in splits)
        tested = [d for s in splits for d in DAYS if s.test_start <= d <= s.test_end]
        assert len(tested) == len(set(tested)) == 9  # each session is tested once

    def test_expanding_windows_keep_all_earlier_data(self) -> None:
        splits = walk_forward(DAYS, train=10, test=5, expanding=True)
        assert [(s.train_start, s.train_end) for s in splits] == [
            (DAYS[0], DAYS[9]),
            (DAYS[0], DAYS[14]),
        ]

    def test_purging_drops_trades_still_open_at_the_boundary(self) -> None:
        split = walk_forward(DAYS, train=10, test=3, embargo=1)[0]
        assert split.purged_train_end(DAYS, horizon=0) == DAYS[9]
        assert split.purged_train_end(DAYS, horizon=2) == DAYS[8]  # entered day 8, done by day 10
        with pytest.raises(ValueError, match="no training sessions"):
            split.purged_train_end(DAYS, horizon=11)

    def test_bad_arguments(self) -> None:
        with pytest.raises(ValueError):
            walk_forward(DAYS, train=0, test=3)
        assert walk_forward(DAYS, train=18, test=3) == []  # not enough data for one test window


class TestDeflatedSharpe:
    def test_probabilistic_sharpe_matches_a_hand_calculation(self) -> None:
        # 50 x (+2%, 0%): mean 1%, sample sd 1% x sqrt(100/99), so SR = sqrt(0.99); skew 0,
        # kurtosis 1, hence the variance term is 1 and z = (SR - 0.9) x sqrt(99) = 0.945111.
        returns = [0.02, 0.0] * 50
        assert probabilistic_sharpe(returns, benchmark=0.9) == pytest.approx(
            NORMAL.cdf(0.945111), abs=1e-5
        )
        assert probabilistic_sharpe(returns) == pytest.approx(1.0)

    def test_what_cannot_be_estimated_is_none(self) -> None:
        assert probabilistic_sharpe([0.01, 0.02]) is None  # too short
        assert probabilistic_sharpe([0.01] * 10) is None  # no variation

    def test_expected_maximum_of_unskilled_trials(self) -> None:
        # The expected maximum of 100 independent standard normals is about 2.508.
        assert expected_max_sharpe(1.0, 100) == pytest.approx(2.508, abs=0.03)
        assert expected_max_sharpe(1.0, 1) == 0.0
        assert expected_max_sharpe(0.25, 100) == pytest.approx(0.5 * expected_max_sharpe(1.0, 100))
        assert expected_max_sharpe(1.0, 1000) > expected_max_sharpe(1.0, 100)

    def test_the_same_returns_pass_alone_and_fail_as_the_best_of_many(self) -> None:
        returns = [0.0115, -0.0085] * 126  # a year of days, Sharpe about 0.15 a day
        alone = deflated_sharpe(returns, trial_variance=0.0, trials=1)
        best_of_100 = deflated_sharpe(returns, trial_variance=0.01, trials=100)
        assert alone == probabilistic_sharpe(returns) and alone is not None and alone > 0.95
        assert best_of_100 is not None and best_of_100 < 0.5  # luck explains it


# --- leaks -----------------------------------------------------------------------------


def run_s1(items: Sequence[Item]) -> BacktestResult:
    return Engine(OrderWinDrift.from_yaml(S1), COSTS, CFG).run(items)


class Peeker(Scripted):
    """A planted leak: buys when the close five minutes ahead is higher."""

    def __init__(self, everything: Sequence[Item]) -> None:
        super().__init__()
        self.future = {
            (b.instrument_key, b.start): b for b in everything if isinstance(b, BarEvent)
        }

    def on_bar(self, bar: BarEvent, ctx: Context) -> list[Signal]:
        ahead = self.future.get((bar.instrument_key, bar.start + timedelta(minutes=5)))
        key = bar.instrument_key
        if ahead is None or ahead.close <= bar.close or ctx.position(key) or ctx.pending(key):
            return []
        return [Signal(key, "buy", "intraday", "peek", ctx.now, quantity=10)]


def run_peeker(items: Sequence[Item]) -> BacktestResult:
    return Engine(Peeker(items), COSTS, CFG).run(items)


def test_s1_does_not_use_the_future() -> None:
    assert find_leak(run_s1, golden_items(), cuts=40, seed=1) is None


def test_a_planted_look_ahead_is_caught() -> None:
    leak = find_leak(run_peeker, golden_items(), cuts=40, seed=1)
    assert leak is not None
    assert leak.before != leak.after  # a decision at or before the cut depended on later data


# --- holdout discipline ------------------------------------------------------------------


def test_holdout_needs_a_pre_registered_design(engine: Database, tmp_path: Path) -> None:
    strategy = OrderWinDrift.from_yaml(S1)
    kwargs: dict[str, Any] = {"costs": COSTS, "config": CFG, "data_start": D1, "data_end": D2}
    with pytest.raises(HoldoutError, match="needs a pre-registration"):
        run_backtest(engine, strategy, golden_items(), holdout=True, **kwargs)
    other = tmp_path / "prereg.md"
    other.write_text("design 0000000000000000", encoding="utf-8")
    with pytest.raises(HoldoutError, match="does not record design"):
        run_backtest(engine, strategy, golden_items(), holdout=True, prereg=other, **kwargs)
    with engine.begin() as conn:
        assert conn.execute(select(experiments)).all() == []  # a refusal computes and logs nothing

    registered = tmp_path / "s1_prereg.md"
    registered.write_text(
        f"Design hash: {design(strategy, COSTS, CFG, 'none')[0]}", encoding="utf-8"
    )
    _, _, run_id = run_backtest(
        engine, strategy, golden_items(), holdout=True, prereg=registered, **kwargs
    )
    with engine.begin() as conn:
        row = conn.execute(select(experiments).where(experiments.c.id == run_id)).one()
    assert row.holdout and row.status == "done"


def test_trials_and_their_spread_come_from_the_registry(engine: Database) -> None:
    def log(params_hash: str, annual_sharpe: float | None) -> None:
        with experiment(
            engine, kind="backtest", name="s1", params_hash=params_hash, params={},
            data_start=D1, data_end=D2, holdout=False,
        ) as run:  # fmt: skip
            run.metrics = {"sharpe": annual_sharpe}

    with engine.begin() as conn:
        assert registry_trials(conn) == (0, None)
    log("a", 1.0)
    with engine.begin() as conn:
        assert registry_trials(conn) == (1, None)  # one design: no spread to estimate
    log("b", 3.0)
    log("b", 3.0)  # a rerun is not a new trial
    log("c", None)  # no Sharpe (too few days), but still a trial
    with engine.begin() as conn:
        trials, variance = registry_trials(conn, name="s1")
    assert trials == 3
    assert variance == pytest.approx(statistics.variance([1.0 / 252**0.5, 3.0 / 252**0.5]))
