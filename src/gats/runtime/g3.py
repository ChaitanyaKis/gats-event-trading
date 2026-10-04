"""Gate G3 (T7.5): a paper run judged against the backtest of the same days.

The paper side is the run itself, replayed from its journal (which also
proves the record still replays). The backtest side takes the *same*
filings and bars and runs them the way G2's backtest does: every filing at
the moment it became available plus an assumed delay, instead of the moment
the live runtime really handed it over. The difference between the two is
exactly what paper trading is for: what live timing, outages and late data
did to a strategy the backtest liked.

Tolerances come from ``configs/g3.yaml``, fixed before the run produced any
data. The verdict is mechanical; the decision at the gate is the human's.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, select

from gats.backtest.engine import BacktestResult, Engine, moment
from gats.backtest.feed import Lookups
from gats.backtest.ledger import build_trades
from gats.db.schema import paper_journal
from gats.pit import AsOf
from gats.risk.engine import RiskEngine
from gats.runtime.journal import DayClose, PaperRun, Reference, decode
from gats.runtime.paper import System
from gats.strategy.base import MarketEvent
from gats.timeutil import to_ist


class G3Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    min_days: int = Field(ge=0)
    min_trades: int = Field(ge=1)
    signal_count_tolerance: float = Field(ge=0)
    fill_rate_tolerance: float = Field(ge=0, le=1)
    slippage_tolerance_bps: float = Field(ge=0)
    backtest_latency_s: float = Field(gt=0)
    confidence: float = Field(gt=0.5, lt=1)
    bootstrap_resamples: int = Field(ge=100)
    bootstrap_seed: int


def load_spec(path: Path) -> G3Spec:
    return G3Spec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Side:
    """One side of the comparison."""

    entries: int  # entry orders that left for the market
    filled: int  # of those, the ones that got shares
    slippage_bps: float | None  # mean entry fill against the price it was decided on
    nets: list[float]  # net rupees of each closed trade
    days: list[Any]  # the day each of those trades closed

    @property
    def fill_rate(self) -> float | None:
        return self.filled / self.entries if self.entries else None


def measure(result: BacktestResult, band: float) -> Side:
    entries = [o for o in result.orders if not o.signal.closes and o.status != "rejected"]
    slips = []
    for order in entries:
        fills = [e for e in result.executions if e.order_id == order.order_id]
        shares = sum(e.quantity for e in fills)
        if shares:
            paid = sum(e.quantity * e.price for e in fills) / shares
            decided_on = order.limit / (1 + band)  # the limit is the reference plus the band
            slips.append((paid / decided_on - 1) * 1e4)
    trades, _ = build_trades(result.executions)
    return Side(
        entries=len(entries),
        filled=len(slips),
        slippage_bps=sum(slips) / len(slips) if slips else None,
        nets=[t.net for t in trades],
        days=[to_ist(t.closed_at).date() for t in trades],
    )


def backtest_of(
    conn: Connection, run_id: int, system: System, latency_s: float, now: datetime
) -> BacktestResult:
    """The journaled filings and bars, run as a backtest: each item at the
    moment it became known, not when the live runtime had it."""
    rows = conn.execute(
        select(paper_journal.c.kind, paper_journal.c.payload)
        .where(paper_journal.c.run_id == run_id)
        .order_by(paper_journal.c.seq)
    ).all()
    items = [decode(kind, payload) for kind, payload in rows]
    securities = {i.instrument_key: i.security_id for i in items if isinstance(i, MarketEvent)}
    lookups = Lookups(AsOf(conn, now), securities)
    risk = RiskEngine(
        system.limits,
        system.risk_version,
        liquidity=lookups.liquidity,
        flags=lookups.flags,
        halted=lambda: False,  # a backtest has no kill switch
    )
    engine = Engine(
        system.strategy, system.costs, replace(system.config, latency_s=latency_s), risk=risk
    )

    def known(item: Any) -> tuple[datetime, int]:
        if isinstance(item, Reference):
            return item.as_of, 0  # a previous close: known since that close
        return moment(item), 2 if isinstance(item, MarketEvent) else 1

    for item in sorted((i for i in items if not isinstance(i, DayClose)), key=known):
        if isinstance(item, Reference):
            engine.reference(item.instrument_key, item.price, item.as_of)
        else:
            engine.step(item)
    return engine.finish()


def expectancy_interval(
    nets: list[float], days: list[Any], spec: G3Spec
) -> tuple[float, float, float] | None:
    """(mean, lower, upper) of net rupees per trade, resampling whole days:
    trades of one day share that day's market and are not independent."""
    if not nets:
        return None
    by_day: dict[Any, list[float]] = {}
    for net, day in zip(nets, days, strict=True):
        by_day.setdefault(day, []).append(net)
    groups = [np.array(v) for _, v in sorted(by_day.items())]
    rng = np.random.default_rng(spec.bootstrap_seed)
    means = np.empty(spec.bootstrap_resamples)
    for i in range(spec.bootstrap_resamples):
        drawn = rng.integers(0, len(groups), len(groups))
        means[i] = np.concatenate([groups[j] for j in drawn]).mean()
    tail = (1 - spec.confidence) / 2
    low, high = np.quantile(means, [tail, 1 - tail])
    return float(np.mean(nets)), float(low), float(high)


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


def judge(paper: Side, backtest: Side, span_days: int, spec: G3Spec) -> list[Check]:
    checks = [
        Check(f"at least {spec.min_days} days of paper trading", span_days >= spec.min_days,
              f"{span_days} days"),
        Check(f"at least {spec.min_trades} closed trades", len(paper.nets) >= spec.min_trades,
              f"{len(paper.nets)} trades"),
    ]  # fmt: skip
    gap = abs(paper.entries - backtest.entries) / max(backtest.entries, 1)
    checks.append(
        Check(
            f"signal count within {spec.signal_count_tolerance:.0%} of the backtest",
            gap <= spec.signal_count_tolerance,
            f"paper {paper.entries}, backtest {backtest.entries}",
        )
    )
    ours, theirs = paper.fill_rate, backtest.fill_rate
    checks.append(
        Check(
            f"fill rate within {spec.fill_rate_tolerance:.0%} of the backtest",
            ours is not None
            and theirs is not None
            and abs(ours - theirs) <= spec.fill_rate_tolerance,
            f"paper {_share(ours)}, backtest {_share(theirs)}",
        )
    )
    worse = (
        None
        if paper.slippage_bps is None or backtest.slippage_bps is None
        else paper.slippage_bps - backtest.slippage_bps
    )
    checks.append(
        Check(
            f"entry slippage at most {spec.slippage_tolerance_bps:g} bps worse than the backtest",
            worse is not None and worse <= spec.slippage_tolerance_bps,
            f"paper {_bps(paper.slippage_bps)}, backtest {_bps(backtest.slippage_bps)}",
        )
    )
    interval = expectancy_interval(paper.nets, paper.days, spec)
    checks.append(
        Check(
            f"net expectancy: lower end of the {spec.confidence:.0%} interval not below zero",
            interval is not None and interval[1] >= 0,
            "no closed trades"
            if interval is None
            else f"Rs {interval[0]:,.2f} a trade ({interval[1]:,.2f} to {interval[2]:,.2f})",
        )
    )
    return checks


def _share(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1%}"


def _bps(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f} bps"


def verdict(checks: list[Check]) -> str:
    return "PASS" if all(c.passed for c in checks) else "FAIL"


def build_report(
    conn: Connection, system: System, name: str, spec: G3Spec, now: datetime
) -> tuple[str, list[Check]]:
    """The G3 report for the run called ``name``. Opening the run replays
    its journal, so a record the code no longer reproduces fails here."""
    run = PaperRun.open(
        conn,
        name=name,
        strategy=system.strategy.version,
        design_hash=system.design_hash,
        design=system.design,
        build=system.build,
        now=now,
        root=system.root,
    )
    band = system.config.protection_band
    paper = measure(run.engine.result(), band)
    backtest = measure(backtest_of(conn, run.id, system, spec.backtest_latency_s, now), band)
    span = (
        conn.execute(
            select(paper_journal.c.at)
            .where(paper_journal.c.run_id == run.id)
            .order_by(paper_journal.c.seq)
        )
        .scalars()
        .all()
    )
    span_days = (to_ist(span[-1]).date() - to_ist(span[0]).date()).days + 1 if span else 0
    checks = judge(paper, backtest, span_days, spec)
    lines = [
        f"# M7 paper trading: gate G3 for run `{name}`",
        "",
        f"Design `{system.design_hash}` ({system.strategy.version}); criteria `{spec.version}` "
        f"(configs/g3.yaml); written {to_ist(now):%Y-%m-%d %H:%M} IST.",
        "",
        f"**G3: {verdict(checks)}**",
        "",
        "The verdict is mechanical. Whether to go on to a live pilot is the human's decision "
        "(G4); a FAIL is a result, not a reason to change the criteria.",
        "",
        "| Criterion | Result | Measured |",
        "|---|---|---|",
        *[f"| {c.name} | {'pass' if c.passed else '**FAIL**'} | {c.detail} |" for c in checks],
        "",
        "## Paper against the backtest of the same filings and bars",
        "",
        "| | Paper (as handed over live) | Backtest (as the data became known) |",
        "|---|---|---|",
        f"| Entry orders sent | {paper.entries} | {backtest.entries} |",
        f"| Entries filled | {paper.filled} ({_share(paper.fill_rate)}) | "
        f"{backtest.filled} ({_share(backtest.fill_rate)}) |",
        f"| Mean entry slippage | {_bps(paper.slippage_bps)} | {_bps(backtest.slippage_bps)} |",
        f"| Closed trades | {len(paper.nets)} | {len(backtest.nets)} |",
        f"| Net result | Rs {sum(paper.nets):,.2f} | Rs {sum(backtest.nets):,.2f} |",
        "",
        "Fills on both sides are the same pessimistic model on one-minute bars, so this "
        "compares timing and data, not real execution: real slippage is unknown until real "
        "orders exist.",
        "",
    ]
    return "\n".join(lines), checks
