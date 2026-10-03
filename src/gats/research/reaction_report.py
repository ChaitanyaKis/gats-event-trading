"""The M5 report: reaction curves and the G1b decision (T5.3, T5.4).

The decision uses M3's gate code unchanged (clustered tests, Benjamini-
Hochberg over the family, FCR-adjusted bounds, the minimum sample), applied
to this study's net abnormal returns. Everything else in the report is
context and is labelled as such; only the decision table decides.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from gats.research.reaction import SLIPPAGE_SENSITIVITY_BPS, Delay, Horizon, ReactionConfig
from gats.research.report import Cell, cells_for, evaluate_g1
from gats.research.study import StatSpec


@dataclass(frozen=True)
class _Scope:
    confirmatory: list[str]


@dataclass(frozen=True)
class GateView:
    """The study restricted to the run's scope, as the gate code reads it."""

    events: _Scope
    event_types: list[str]
    exits: Sequence[Horizon]
    statistics: StatSpec


def decide(
    rows: list[dict[str, Any]], cfg: ReactionConfig, scope: Sequence[str]
) -> tuple[dict[tuple[str, str, str], Cell], list[Cell]]:
    """All confirmatory cells and the judged family (scope x confirmatory exits, test)."""
    view = GateView(_Scope(list(scope)), list(scope), cfg.exits, cfg.statistics)
    cells, boots = cells_for(rows, view)
    return cells, evaluate_g1(cells, boots, view)


def _pct(x: float) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{x * 100:+.3f}%"


def _mean(rows: Sequence[dict[str, Any]], column: str) -> tuple[int, float]:
    values = [r[column] for r in rows if r.get(column) is not None]
    return len(values), (sum(values) / len(values) if values else math.nan)


def _kept(rows: Sequence[dict[str, Any]], period: str = "test") -> list[dict[str, Any]]:
    return [r for r in rows if r.get("filter_reason") is None and r.get("period") == period]


def build_reaction_report(
    rows: list[dict[str, Any]],
    cfg: ReactionConfig,
    *,
    digest: str,
    run_id: str,
    experiment_id: int,
    scope: Sequence[str],
    delay: Delay,
    median_rows: list[dict[str, Any]] | None = None,
    median_delay: Delay | None = None,
    m3_run_date: date | None = None,
) -> tuple[str, list[Cell]]:
    cells, family = decide(rows, cfg, scope)
    passed = [c for c in family if c.passes]
    verdict = (
        "PASS: " + ", ".join(f"{c.event_type} at {c.exit}" for c in passed)
        if passed
        else "no tradeable remainder found"
    )
    rule = cfg.entry
    lines = [
        "# M5 reaction curves (gate G1b)",
        "",
        f"Study `{cfg.study}`, config `{digest[:12]}`, run {run_id}, experiment #{experiment_id}.",
        "",
        f"**G1b: {verdict}.**",
        "",
        f"- Scope (fixed before returns were computed): {', '.join(scope)}.",
        f"- Decision delay: {delay.total_s:.1f} s = feed latency p{rule.latency_percentile} "
        f"{delay.feed_s:.1f} s (measured on {delay.filings} live filings) + "
        f"{rule.processing_allowance_s:.0f} s + {rule.order_allowance_s:.0f} s.",
        f"- Costs: `{cfg.costs.cost_file.as_posix()}` for a Rs {cfg.costs.notional_rs:,.0f} "
        f"intraday round trip + {cfg.costs.slippage_bps_per_side:g} bp slippage per side.",
        "",
        "## Decision table (test period, the only table that decides)",
        "",
        "| Type | Exit | N | Mean net | t | p | p (BH) | FCR lower | Result |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in family:
        result = "**PASS**" if c.passes else "; ".join(c.reasons)
        lines.append(
            f"| {c.event_type} | {c.exit} | {c.n} | {_pct(c.mean_net)} | {c.t:.2f} | {c.p:.4f} | "
            f"{c.p_bh:.4f} | {_pct(c.fcr_low)} | {result} |"
        )
    lines += [
        "",
        "## Reaction curves: train and test (context)",
        "",
        "| Type | Exit | Period | N | Mean abnormal, before costs | Mean net | Hit rate | 95% CI |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (event_type, exit_name, period), c in sorted(cells.items()):
        lines.append(
            f"| {event_type} | {exit_name} | {period} | {c.n} | {_pct(c.mean_gross)} | "
            f"{_pct(c.mean_net)} | {_pct(c.hit_rate)} | {_pct(c.ci_low)} to {_pct(c.ci_high)} |"
        )
    lines += ["", "## Reported only (test period, never part of the decision)", ""]
    lines += ["| Type | Measure | Exit | N | Mean net |", "|---|---|---|---|---|"]

    def show(event_type: str, measure: str, members: Sequence[dict[str, Any]], prefix: str,
             exits: Sequence[Horizon]) -> None:  # fmt: skip
        for x in exits:
            n, mean = _mean(members, f"{prefix}_{x.name}")
            lines.append(f"| {event_type} | {measure} | {x.name} | {n} | {_pct(mean)} |")

    test = _kept(rows)
    for event_type in scope:
        of_type = [r for r in test if r["event_type"] == event_type]
        show(event_type, "extra exits", of_type, "net", cfg.extra_exits)
        for stratum in ("session", "overnight"):
            members = [r for r in of_type if r.get("stratum") == stratum]
            show(event_type, f"{stratum} filings", members, "net", cfg.exits)
        for bps in SLIPPAGE_SENSITIVITY_BPS:
            show(event_type, f"{bps:g} bp slippage", of_type, f"net{int(bps)}", cfg.exits)
        if median_rows is not None and median_delay is not None:
            at_median = [r for r in _kept(median_rows) if r["event_type"] == event_type]
            label = f"median latency ({median_delay.total_s:.0f} s)"
            show(event_type, label, at_median, "net", cfg.exits)
        if m3_run_date is not None:
            later = [r for r in of_type if r["entry_date"] > m3_run_date]
            show(event_type, f"entries after {m3_run_date}", later, "net", cfg.exits)
    if m3_run_date is None:
        lines += ["", "M3 has no recorded run, so there is no post-M3 subsample to show."]
    reasons = Counter((r["event_type"], r.get("filter_reason") or "kept") for r in rows)
    lines += ["", "## Every event accounted for", ""]
    lines += ["| Type | Outcome | Events |", "|---|---|---|"]
    lines += [f"| {t} | {reason} | {n} |" for (t, reason), n in sorted(reasons.items())]
    lines.append("")
    return "\n".join(lines), family
