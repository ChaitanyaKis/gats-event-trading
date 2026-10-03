"""The experiment registry (T6.6): no run without a row, and honest trial counts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text
from typer.testing import CliRunner

from gats.backtest.engine import EngineConfig
from gats.backtest.runner import design, run_backtest
from gats.cli import app
from gats.db.schema import experiments
from gats.research.registry import experiment, git_state, recent, trial_count
from gats.strategy.base import Signal
from gats.strategy.order_win import OrderWinDrift
from tests.test_engine import COSTS, D1, D2, ROOT, A, Scripted, at, bars, flat, golden_items

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
CFG = EngineConfig(initial_cash=1_000_000.0, notional_per_trade=100_000.0)


def register(db: Engine, params_hash: str = "h1", **kw: Any) -> Any:
    return experiment(
        db,
        kind=kw.pop("kind", "backtest"),
        name=kw.pop("name", "s1"),
        params_hash=params_hash,
        params={"x": 1},
        data_start=D1,
        data_end=D2,
        holdout=kw.pop("holdout", False),
        clock=lambda: NOW,
        **kw,
    )


def rows(db: Engine) -> list[Any]:
    with db.begin() as conn:
        return list(conn.execute(select(experiments).order_by(experiments.c.id)).all())


def test_a_run_is_on_record_before_it_does_anything(engine: Engine) -> None:
    with register(engine) as run:
        (during,) = rows(engine)
        assert (during.id, during.status, during.finished_at) == (run.id, "running", None)
        run.metrics = {"trades": 3}
    (after,) = rows(engine)
    assert (after.status, after.metrics, after.error) == ("done", {"trades": 3}, None)
    assert after.finished_at == NOW and (after.data_start, after.data_end) == (D1, D2)


def test_a_crashed_run_still_counts(engine: Engine) -> None:
    with pytest.raises(ZeroDivisionError), register(engine):
        _ = 1 / 0
    (row,) = rows(engine)
    assert row.status == "failed" and row.error.startswith("ZeroDivisionError")
    with engine.begin() as conn:
        assert trial_count(conn) == 1


def test_trials_are_distinct_designs_not_reruns(engine: Engine) -> None:
    for params_hash, kind in [
        ("a", "backtest"),
        ("a", "backtest"),
        ("b", "backtest"),
        ("c", "event_study"),
    ]:
        with register(engine, params_hash, kind=kind):
            pass
    with engine.begin() as conn:
        assert trial_count(conn) == 3
        assert trial_count(conn, kind="backtest") == 2  # "a" twice is one design
        assert trial_count(conn, kind="backtest", name="other") == 0
        assert [r.params_hash for r in recent(conn, limit=2)] == ["c", "b"]


def test_git_state_names_the_code(tmp_path: Path) -> None:
    sha, dirty = git_state(ROOT)
    assert sha is not None and len(sha) == 40 and dirty in (True, False)
    assert git_state(tmp_path) == (None, None)  # not a checkout: still runs


# --- backtests go through the registry ---------------------------------------------------


def test_backtest_is_registered_with_its_design_and_metrics(engine: Engine) -> None:
    strategy = OrderWinDrift.from_yaml(ROOT / "configs" / "strategies" / "order_win_drift.yaml")
    result, metrics, run_id = run_backtest(
        engine, strategy, golden_items(), costs=COSTS, config=CFG,
        data_start=D1, data_end=D2, holdout=False, root=ROOT,
    )  # fmt: skip
    (row,) = rows(engine)
    assert row.id == run_id and row.status == "done" and row.kind == "backtest"
    assert row.name == "order_win_drift" and row.git_sha is not None
    assert row.params_hash == design(strategy, COSTS, CFG, "none")[0]
    assert row.params["strategy"] == strategy.version and row.params["costs"] == COSTS.version
    assert row.metrics["trades"] == metrics.trades == 2
    assert row.metrics["net_pnl"] == pytest.approx(result.cash - result.initial_cash, abs=0.01)


def test_changing_anything_that_decides_the_result_is_a_new_design() -> None:
    strategy = OrderWinDrift.from_yaml(ROOT / "configs" / "strategies" / "order_win_drift.yaml")
    base = design(strategy, COSTS, CFG, "risk-v1+abc")[0]
    assert design(strategy, COSTS, CFG, "risk-v1+abc")[0] == base
    slower = EngineConfig(initial_cash=1_000_000.0, notional_per_trade=100_000.0, latency_s=30.0)
    assert design(strategy, COSTS, slower, "risk-v1+abc")[0] != base
    assert design(strategy, COSTS, CFG, "risk-v1+xyz")[0] != base


def test_no_registry_no_backtest(engine: Engine) -> None:
    """If the row cannot be written, the strategy is never called."""
    calls: list[str] = []

    class Counting(Scripted):
        def on_bar(self, bar: Any, ctx: Any) -> list[Signal]:
            calls.append("bar")
            return []

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE experiments"))
    with pytest.raises(Exception, match="experiments"):
        run_backtest(
            engine, Counting(), bars(A, at(10, 0), flat(100, 3)), costs=COSTS, config=CFG,
            data_start=D1, data_end=D1, holdout=False,
        )  # fmt: skip
    assert calls == []


def test_failed_backtest_is_recorded(engine: Engine) -> None:
    class Broken(Scripted):
        def on_bar(self, bar: Any, ctx: Any) -> list[Signal]:
            raise RuntimeError("bug in the strategy")

    with pytest.raises(RuntimeError, match="bug in the strategy"):
        run_backtest(
            engine, Broken(), bars(A, at(10, 0), flat(100, 3)), costs=COSTS, config=CFG,
            data_start=D1, data_end=D1, holdout=False,
        )  # fmt: skip
    (row,) = rows(engine)
    assert (row.status, row.holdout) == ("failed", False) and "bug in the strategy" in row.error


def test_cli_lists_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    runner = CliRunner()
    assert "0 distinct design(s) tried" in runner.invoke(app, ["experiments", "list"]).output
    from gats.config import Settings
    from gats.db.engine import make_engine

    db = make_engine(Settings(_env_file=None).resolved_db_url)  # type: ignore[call-arg]
    with register(db, "abcdef1234567890", holdout=True) as run:
        run.metrics = {"trades": 7, "net_pnl": 12.5}
    db.dispose()
    shown = runner.invoke(app, ["experiments", "list", "--kind", "backtest"]).output
    assert "1 distinct design(s) tried of kind backtest" in shown
    assert "backtest s1 abcdef123456" in shown and "HOLDOUT" in shown and "'trades': 7" in shown
