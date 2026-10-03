"""The order-magnitude re-test (T4.7): M3's study with one registered filter.

docs/research/M4_prereg.md fixes the design: keep an ORDER_WIN filing only
if its extracted rupee value is at least a set share of the company's
trailing revenue, both as known when the filing appeared. Everything else
(entry, exits, benchmark, filters, statistics, the gate rule) is M3's code,
reused unchanged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from gats.pit import AsOf
from gats.research.report import Cell, cells_for, evaluate_g1
from gats.research.study import (
    RegistrationError,
    StudyConfig,
    _Strict,
    config_hash,
    registered_hash,
)

Keep = Callable[[Any, datetime], str | None]


class MagnitudeSpec(_Strict):
    min_amount_vs_revenue: float = Field(gt=0)
    extractor: str
    exploratory_thresholds: list[float] = Field(default_factory=list)


class MagnitudeConfig(StudyConfig):
    magnitude: MagnitudeSpec


def verify_magnitude(config: Path, prereg: Path) -> tuple[MagnitudeConfig, str]:
    """Load the study only if the config is exactly the registered one."""
    actual, expected = config_hash(config), registered_hash(prereg)
    if actual != expected:
        raise RegistrationError(
            f"{config} (sha256 {actual[:12]}...) is not the pre-registered config "
            f"({expected[:12]}...): a changed design is a new study and a new trial"
        )
    raw: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    return MagnitudeConfig.model_validate(raw), actual


def size_filter(clock: AsOf, cfg: MagnitudeConfig, threshold: float | None = None) -> Keep:
    """The registered filter: None to keep an event, else why it is dropped.
    Both the order value and the revenue are read as of the event."""
    minimum = cfg.magnitude.min_amount_vs_revenue if threshold is None else threshold
    facts = clock.extracted_facts(None, cfg.magnitude.extractor)

    def keep(row: Any, available_at: datetime) -> str | None:
        amount = facts.get(row.id, {}).get("amount_inr")
        if amount is None:
            return "no_amount"
        revenue = clock.trailing_revenue(row.security_id, available_at)
        if revenue is None or revenue.rupees <= 0:
            return "no_revenue"
        return None if float(amount) / revenue.rupees >= minimum else "below_magnitude"

    return keep


def _pct(x: float) -> str:
    return "n/a" if x != x else f"{x * 100:+.2f}%"  # NaN is the only value unequal to itself


def build_magnitude_report(
    rows: list[dict[str, Any]],
    exploratory: Mapping[float, list[dict[str, Any]]],
    cfg: MagnitudeConfig,
    *,
    digest: str,
    run_id: str,
    experiment_id: int,
    accuracy_note: str,
) -> tuple[str, list[Cell]]:
    cells, boots = cells_for(rows, cfg)
    family = evaluate_g1(cells, boots, cfg)
    passed = [c for c in family if c.passes]
    verdict = (
        "PASS at " + ", ".join(c.exit for c in passed) if passed else "size does not help (no pass)"
    )
    spec = cfg.magnitude
    lines = [
        "# M4 magnitude study: do large orders earn more?",
        "",
        f"Study `{cfg.study}`, config `{digest[:12]}` (pre-registered: "
        f"`docs/research/M4_prereg.md`), run {run_id}, experiment #{experiment_id}.",
        "",
        f"**Result: {verdict}.**",
        "",
        f"- Kept: ORDER_WIN filings with order value / trailing revenue >= "
        f"{spec.min_amount_vs_revenue:.0%}, both as known at the filing.",
        f"- Second study on this test period: BH at q = {cfg.statistics.fdr.q}.",
        f"- Extraction accuracy (T4.6): {accuracy_note}",
        "",
        "## Decision table (test period)",
        "",
        "| Exit | N | Mean net | t | p | p (BH) | FCR lower | Result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for c in family:
        result = "**PASS**" if c.passes else "; ".join(c.reasons)
        lines.append(
            f"| {c.exit} | {c.n} | {_pct(c.mean_net)} | {c.t:.2f} | {c.p:.4f} | {c.p_bh:.4f} | "
            f"{_pct(c.fcr_low)} | {result} |"
        )
    lines += ["", "## Train and test (context)", ""]
    lines += [
        "| Exit | Period | N | Mean abnormal | Mean net | Hit rate |",
        "|---|---|---|---|---|---|",
    ]
    for (_, exit_name, period), c in sorted(cells.items()):
        lines.append(
            f"| {exit_name} | {period} | {c.n} | {_pct(c.mean_gross)} | {_pct(c.mean_net)} | "
            f"{_pct(c.hit_rate)} |"
        )
    lines += ["", "## Other thresholds (reported only, test period)", ""]
    lines += ["| Threshold | Exit | N | Mean net |", "|---|---|---|---|"]
    for threshold, other in sorted(exploratory.items()):
        other_cells, _ = cells_for(other, cfg)
        for x in cfg.exits:
            c = other_cells[("ORDER_WIN", x.name, "test")]
            lines.append(f"| >= {threshold:.0%} | {x.name} | {c.n} | {_pct(c.mean_net)} |")
    reasons = Counter(r.get("filter_reason") or "kept" for r in rows)
    lines += ["", "## Every event accounted for", "", "| Outcome | Events |", "|---|---|"]
    lines += [f"| {reason} | {n} |" for reason, n in sorted(reasons.items())]
    lines.append("")
    return "\n".join(lines), family
