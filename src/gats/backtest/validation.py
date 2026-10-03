"""Validation tools for backtests (T6.7): the checks behind gate G2.

- **Walk-forward splits** with an embargo: parameters are chosen on a
  training window and judged on the sessions after it, never the reverse.
  Trades take time to resolve, so a gap (embargo) and :meth:`Split.purged_train_end`
  keep any trade that was still open at the boundary out of training.
- **Deflated Sharpe ratio** (Bailey and Lopez de Prado, "The Deflated Sharpe
  Ratio", Journal of Portfolio Management 40(5), 2014): the probability
  that the true Sharpe ratio is above what the best of N unskilled trials
  would show by luck, corrected for skewed and fat-tailed returns. N comes
  from the experiment registry.
- **Leak detection**: change everything after a moment, run again, and
  every decision made up to that moment must be identical. A strategy that
  peeks at the future fails.

Sharpe ratios here are per period (not annualised); deflating mixes ratios,
so all of them must use the same period.
"""

from __future__ import annotations

import math
import random
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import zip_longest
from typing import Any

from sqlalchemy import Connection, select

from gats.backtest.engine import BacktestResult, moment
from gats.backtest.ledger import TRADING_DAYS
from gats.db.schema import experiments
from gats.research.registry import trial_count
from gats.strategy.base import BarEvent, MarketEvent

_NORMAL = statistics.NormalDist()
_EULER_GAMMA = 0.5772156649015329
Item = MarketEvent | BarEvent


# --- walk-forward -------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    train_start: date
    train_end: date
    test_start: date
    test_end: date

    def purged_train_end(self, sessions: Sequence[date], horizon: int) -> date:
        """The last training session whose trades (lasting up to ``horizon``
        sessions) finish before the test window opens."""
        days = list(sessions)
        cutoff = days.index(self.test_start) - horizon - 1
        last = min(cutoff, days.index(self.train_end))
        if last < days.index(self.train_start):
            raise ValueError("the horizon leaves no training sessions before the test window")
        return days[last]


def walk_forward(
    sessions: Sequence[date], *, train: int, test: int, embargo: int = 0, expanding: bool = False
) -> list[Split]:
    """Consecutive test windows of ``test`` sessions, each preceded (after
    ``embargo`` sessions) by ``train`` training sessions, or by everything
    before it when ``expanding``."""
    if min(train, test) < 1 or embargo < 0:
        raise ValueError("train and test must be at least 1 session, embargo at least 0")
    days = list(sessions)
    splits = []
    start = train
    while start + embargo + test <= len(days):
        first = 0 if expanding else start - train
        splits.append(
            Split(
                days[first],
                days[start - 1],
                days[start + embargo],
                days[start + embargo + test - 1],
            )
        )
        start += test
    return splits


# --- deflated Sharpe ----------------------------------------------------------------


def sharpe_per_period(returns: Sequence[float]) -> float | None:
    if len(returns) < 2:
        return None
    spread = statistics.stdev(returns)
    return None if spread == 0 else statistics.fmean(returns) / spread


def probabilistic_sharpe(returns: Sequence[float], benchmark: float = 0.0) -> float | None:
    """P(true Sharpe > ``benchmark``), allowing for the sample's length,
    skewness and kurtosis. None when it cannot be estimated."""
    n = len(returns)
    ratio = sharpe_per_period(returns)
    if ratio is None or n < 3:
        return None
    mean = statistics.fmean(returns)
    spread = statistics.pstdev(returns)
    skew = statistics.fmean(((r - mean) / spread) ** 3 for r in returns)
    kurt = statistics.fmean(((r - mean) / spread) ** 4 for r in returns)
    variance = 1 - skew * ratio + (kurt - 1) / 4 * ratio**2
    if variance <= 0:
        return None
    return _NORMAL.cdf((ratio - benchmark) * math.sqrt(n - 1) / math.sqrt(variance))


def expected_max_sharpe(trial_variance: float, trials: int) -> float:
    """The Sharpe ratio the best of ``trials`` skill-less designs is expected
    to show, given the variance of Sharpe ratios across the trials."""
    if trials < 2 or trial_variance <= 0:
        return 0.0
    high = _NORMAL.inv_cdf(1 - 1 / trials)
    higher = _NORMAL.inv_cdf(1 - 1 / (trials * math.e))
    return math.sqrt(trial_variance) * ((1 - _EULER_GAMMA) * high + _EULER_GAMMA * higher)


def deflated_sharpe(
    returns: Sequence[float], *, trial_variance: float, trials: int
) -> float | None:
    """P(true Sharpe > the luck of ``trials`` tries). G2 asks for > 0.95."""
    return probabilistic_sharpe(returns, expected_max_sharpe(trial_variance, trials))


# --- leak detection -----------------------------------------------------------------


@dataclass(frozen=True)
class Leak:
    cut: datetime
    before: tuple[Any, ...]
    after: tuple[Any, ...]


def decisions_until(result: BacktestResult, cut: datetime) -> list[tuple[Any, ...]]:
    """What had been decided by ``cut``: orders sent by then (with their
    size, limit and any refusal), and fills in bars that had closed."""
    orders = [
        (
            "order",
            o.signal.instrument_key,
            o.signal.side,
            o.quantity,
            o.submitted_at,
            round(o.limit, 6),
            o.note if o.status == "rejected" else "",
        )
        for o in result.orders
        if o.submitted_at <= cut
    ]
    fills = [
        ("fill", e.instrument_key, e.side, e.quantity, e.at, round(e.price, 6))
        for e in result.executions
        if e.bar_volume > 0 and e.at.timestamp() + 60 <= cut.timestamp()
    ]
    return orders + fills


def perturb_after(items: Sequence[Item], cut: datetime, rng: random.Random) -> list[Item]:
    """The same history up to ``cut`` and a different future after it."""
    changed: list[Item] = []
    for item in items:
        if moment(item) <= cut:
            changed.append(item)
        elif isinstance(item, BarEvent):
            factor = rng.uniform(0.5, 1.5)
            changed.append(
                replace(
                    item,
                    open=item.open * factor,
                    high=item.high * factor,
                    low=item.low * factor,
                    close=item.close * factor,
                    volume=max(1, int(item.volume * rng.uniform(0.3, 3.0))),
                )
            )
        else:
            facts = {
                k: v * rng.uniform(0.1, 10.0) if isinstance(v, (int, float)) else v
                for k, v in item.facts.items()
            }
            changed.append(replace(item, facts=facts))
    return changed


def find_leak(
    run: Callable[[Sequence[Item]], BacktestResult],
    items: Sequence[Item],
    *,
    cuts: int = 25,
    seed: int = 0,
) -> Leak | None:
    """Try ``cuts`` random moments; return the first at which a changed
    future changed a past decision. ``run`` must build a fresh strategy and
    engine every time."""
    rng = random.Random(seed)
    baseline = run(items)
    moments = sorted({moment(item) for item in items})
    for cut in rng.sample(moments, min(cuts, len(moments))):
        other = run(perturb_after(items, cut, rng))
        before, after = decisions_until(baseline, cut), decisions_until(other, cut)
        if before != after:
            pairs = zip_longest(before, after, fillvalue=())
            return Leak(cut, *next((a, b) for a, b in pairs if a != b))
    return None


def registry_trials(conn: Connection, *, name: str | None = None) -> tuple[int, float | None]:
    """(distinct backtest designs tried, variance of their per-period Sharpe
    ratios) from the experiment registry. The variance is None when fewer
    than two designs produced a Sharpe ratio: it cannot be estimated."""
    query = select(experiments.c.params_hash, experiments.c.metrics).where(
        experiments.c.kind == "backtest", experiments.c.status == "done"
    )
    if name is not None:
        query = query.where(experiments.c.name == name)
    latest: dict[str, float] = {}
    for params_hash, metrics in conn.execute(query.order_by(experiments.c.id)):
        annual = (metrics or {}).get("sharpe")
        if annual is not None:
            latest[params_hash] = float(annual) / math.sqrt(TRADING_DAYS)
    variance = statistics.variance(latest.values()) if len(latest) >= 2 else None
    return trial_count(conn, kind="backtest", name=name), variance
