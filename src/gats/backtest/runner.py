"""The one way research code runs a backtest (T6.6).

:func:`run_backtest` registers the run before the engine starts, so there
is no unlogged backtest to forget when the trials are counted. The design's
hash covers everything that decides the result: the strategy and its
parameters, the cost table, the risk limits and the engine settings.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import Engine as Database

from gats.backtest.costs import CostModel
from gats.backtest.engine import BacktestResult, CircuitLimits, Engine, EngineConfig, RiskGate
from gats.backtest.ledger import Metrics, summarize
from gats.research.registry import experiment
from gats.strategy.base import BarEvent, MarketEvent, Strategy


def design(
    strategy: Strategy,  # type: ignore[type-arg]
    costs: CostModel,
    config: EngineConfig,
    risk_version: str,
) -> tuple[str, dict[str, Any]]:
    """(hash, description) of what is being run."""
    params = {
        "strategy": strategy.version,
        "costs": costs.version,
        "risk": risk_version,
        "engine": asdict(config),
    }
    canonical = json.dumps(params, sort_keys=True, default=str)
    stored: dict[str, Any] = json.loads(canonical)  # what is hashed is what is stored
    return hashlib.sha256(canonical.encode()).hexdigest()[:16], stored


def run_backtest(
    db: Database,
    strategy: Strategy,  # type: ignore[type-arg]
    items: Iterable[MarketEvent | BarEvent],
    *,
    costs: CostModel,
    config: EngineConfig,
    data_start: date,
    data_end: date,
    holdout: bool,
    risk: RiskGate | None = None,
    risk_version: str = "none",
    circuit_limits: CircuitLimits | None = None,
    sessions: Sequence[date] | None = None,
    root: Path = Path(),
) -> tuple[BacktestResult, Metrics, int]:
    """Register, run, summarise; returns (result, metrics, experiment id)."""
    params_hash, params = design(strategy, costs, config, risk_version)
    with experiment(
        db,
        kind="backtest",
        name=strategy.name,
        params_hash=params_hash,
        params=params,
        data_start=data_start,
        data_end=data_end,
        holdout=holdout,
        root=root,
    ) as run:
        engine = Engine(strategy, costs, config, risk=risk, circuit_limits=circuit_limits)
        result = engine.run(items)
        metrics = summarize(result, participation=config.participation, sessions=sessions)
        run.metrics = asdict(metrics)
    return result, metrics, run.id
