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


class HoldoutError(RuntimeError):
    """A holdout run whose design was not pre-registered."""


def require_preregistration(prereg: Path | None, params_hash: str) -> None:
    """The holdout is spent once per design, and only on designs written
    down beforehand: the document must contain the design's hash."""
    if prereg is None or not prereg.exists():
        raise HoldoutError(
            "a holdout run needs a pre-registration document that records its design hash "
            f"({params_hash})"
        )
    if params_hash not in prereg.read_text(encoding="utf-8"):
        raise HoldoutError(
            f"{prereg} does not record design {params_hash}: pre-register the design before "
            "testing it on the holdout"
        )


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
    prereg: Path | None = None,
    root: Path = Path(),
) -> tuple[BacktestResult, Metrics, int]:
    """Register, run, summarise; returns (result, metrics, experiment id).
    A holdout run is refused, before anything is computed or logged, unless
    ``prereg`` records this design."""
    params_hash, params = design(strategy, costs, config, risk_version)
    if holdout:
        require_preregistration(prereg, params_hash)
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
