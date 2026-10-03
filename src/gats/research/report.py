"""The M3 report: statistics per event type, the G1 decision, CAR plots.

Regenerates from one command (``gats research event-study`` writes it after
a run; ``gats research report --run <id>`` rebuilds it from the saved
Parquet frame). The G1 verdict is computed mechanically from the
pre-registered rule; a human decides what to do with it.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from gats.research.stats import (
    bh_adjust,
    cluster_bootstrap_means,
    clustered_mean,
    fcr_lower_bound,
)
from gats.research.study import StatSpec, StudyConfig


class _Named(Protocol):
    @property
    def name(self) -> str: ...


class _Confirmatory(Protocol):
    @property
    def confirmatory(self) -> list[str]: ...


class GateConfig(Protocol):
    """What the gate rule reads from a study config (M3's and M5's fit)."""

    @property
    def events(self) -> _Confirmatory: ...

    @property
    def event_types(self) -> list[str]: ...

    @property
    def exits(self) -> Sequence[_Named]: ...

    @property
    def statistics(self) -> StatSpec: ...


# Exploratory liquidity buckets (20-session median traded value, Rs), fixed
# in code before the first real run; not part of the G1 rule.
LIQUIDITY_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("Rs 1-10 Cr", 1e7, 1e8),
    ("Rs 10-100 Cr", 1e8, 1e9),
    (">= Rs 100 Cr", 1e9, math.inf),
)


@dataclass
class Cell:
    """Statistics of one (event type, exit, period) cell."""

    event_type: str
    exit: str
    period: str
    confirmatory: bool
    n: int = 0
    clusters: int = 0
    mean_net: float = math.nan
    median_net: float = math.nan
    mean_gross: float = math.nan
    hit_rate: float = math.nan
    t: float = math.nan
    p: float = math.nan
    ci_low: float = math.nan
    ci_high: float = math.nan
    p_bh: float = math.nan
    fcr_low: float = math.nan
    passes: bool = False
    reasons: list[str] = field(default_factory=list)


Key = tuple[str, str, str]  # (event type, exit, period)


def cells_for(
    rows: list[dict[str, Any]], cfg: GateConfig
) -> tuple[dict[Key, Cell], dict[Key, Any]]:
    """All cells, and each cell's bootstrap means (needed for FCR bounds)."""
    confirmatory = set(cfg.events.confirmatory)
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        period = row.get("period")
        if row.get("filter_reason") is None and period in ("train", "test"):
            grouped[(str(row["event_type"]), str(period))].append(row)
    cells: dict[Key, Cell] = {}
    boots: dict[Key, Any] = {}
    stats = cfg.statistics
    for event_type in cfg.event_types:
        for period in ("train", "test"):
            members = grouped.get((event_type, period), [])
            for x in cfg.exits:
                key = (event_type, x.name, period)
                cell = Cell(event_type, x.name, period, event_type in confirmatory)
                pairs = [
                    (r[f"net_{x.name}"], r[f"abnormal_{x.name}"], r["entry_date"])
                    for r in members
                    if r.get(f"net_{x.name}") is not None
                ]
                if pairs:
                    net = [p[0] for p in pairs]
                    clusters = [p[2] for p in pairs]
                    result = clustered_mean(net, clusters)
                    boot = cluster_bootstrap_means(
                        net, clusters, stats.bootstrap_resamples, stats.bootstrap_seed
                    )
                    cell.n, cell.clusters = result.n, result.clusters
                    cell.mean_net, cell.median_net = result.mean, result.median
                    cell.hit_rate, cell.t, cell.p = result.hit_rate, result.t, result.p_one_sided
                    cell.mean_gross = sum(p[1] for p in pairs) / len(pairs)
                    cell.ci_low = _quantile(boot, 0.025)
                    cell.ci_high = _quantile(boot, 0.975)
                    boots[key] = boot
                cells[key] = cell
    return cells, boots


def _quantile(values: Any, q: float) -> float:
    import numpy as np

    return float(np.quantile(values, q))


def evaluate_g1(cells: dict[Key, Cell], boots: dict[Key, Any], cfg: GateConfig) -> list[Cell]:
    """Apply the pre-registered gate rule (G1, and G1b with M5's config) to
    the confirmatory test cells."""
    keys = [(t, x.name, "test") for t in cfg.events.confirmatory for x in cfg.exits]
    family = [cells[k] for k in keys]
    adjusted = bh_adjust([c.p for c in family])
    q = cfg.statistics.fdr.q
    for cell, p_bh in zip(family, adjusted, strict=True):
        cell.p_bh = p_bh
    selected = {
        k for k, c in zip(keys, family, strict=True) if not math.isnan(c.p_bh) and c.p_bh <= q
    }
    for key, cell in zip(keys, family, strict=True):
        if key in selected and key in boots:
            cell.fcr_low = fcr_lower_bound(boots[key], len(selected), len(family), q)
        if cell.n < cfg.statistics.min_test_events:
            cell.reasons.append(f"N_test {cell.n} < {cfg.statistics.min_test_events}")
        if not (cell.mean_net > 0):
            cell.reasons.append("mean net abnormal return <= 0")
        if key not in selected:
            cell.reasons.append(f"not selected by BH at q={q}")
        elif not (cell.fcr_low > 0):
            cell.reasons.append("FCR-adjusted lower bound <= 0")
        cell.passes = not cell.reasons
    return family


def liquidity_table(
    rows: list[dict[str, Any]], cfg: StudyConfig
) -> list[tuple[str, str, str, int, float]]:
    """(type, bucket, exit, N, mean net) for confirmatory types, test period."""
    out = []
    for event_type in cfg.events.confirmatory:
        for label, low, high in LIQUIDITY_BUCKETS:
            members = [
                r
                for r in rows
                if r["event_type"] == event_type
                and r.get("filter_reason") is None
                and r.get("period") == "test"
                and r.get("median_turnover_rs") is not None
                and low <= r["median_turnover_rs"] < high
            ]
            for x in cfg.exits:
                values = [r[f"net_{x.name}"] for r in members if r.get(f"net_{x.name}") is not None]
                mean = sum(values) / len(values) if values else math.nan
                out.append((event_type, label, x.name, len(values), mean))
    return out


def car_plot(cells: dict[Key, Cell], cfg: StudyConfig, path: Path) -> None:
    """Mean gross abnormal return by holding horizon, train vs test."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    horizons = [x.sessions_after_entry for x in cfg.exits]
    types = cfg.events.confirmatory
    fig, axes = plt.subplots(1, len(types), figsize=(3.2 * len(types), 3), sharey=True)
    for ax, event_type in zip(axes, types, strict=True):
        for period, style in (("train", "--"), ("test", "-")):
            ys = [cells[(event_type, x.name, period)].mean_gross * 100 for x in cfg.exits]
            ax.plot(horizons, ys, style, marker="o", label=period)
        ax.axhline(0, color="grey", linewidth=0.8)
        ax.set_title(event_type, fontsize=9)
        ax.set_xlabel("sessions after entry")
    axes[0].set_ylabel("mean abnormal return, %")
    axes[0].legend(fontsize=8)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=120)
    plt.close(fig)


def _pct(x: float) -> str:
    return "n/a" if math.isnan(x) else f"{x * 100:+.2f}%"


def _num(x: float, digits: int = 3) -> str:
    return "n/a" if math.isnan(x) else f"{x:.{digits}f}"


def spot_check_events(
    rows: list[dict[str, Any]], cfg: StudyConfig, k: int = 3
) -> list[dict[str, Any]]:
    """Deterministic sample of kept confirmatory test events for a hand check."""
    kept = [
        r
        for r in rows
        if r.get("filter_reason") is None and r.get("confirmatory") and r.get("period") == "test"
    ]
    kept.sort(key=lambda r: r["announcement_id"])
    if len(kept) <= k:
        return kept
    step = len(kept) // k
    return [kept[i * step] for i in range(k)]


def build_report(
    rows: list[dict[str, Any]],
    cfg: StudyConfig,
    *,
    config_hash: str,
    run_id: str,
    fingerprint: dict[str, Any],
    figure: str | None,
) -> tuple[str, list[Cell]]:
    cells, boots = cells_for(rows, cfg)
    family = evaluate_g1(cells, boots, cfg)
    passing = [c for c in family if c.passes]
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        counts[row["event_type"]][row.get("filter_reason") or "kept"] += 1

    lines = [
        "# M3 event study: results",
        "",
        f"- Run `{run_id}`; config `{config_hash[:12]}…` (pre-registered: "
        "`docs/research/M3_prereg.md`); taxonomy "
        f"`{cfg.taxonomy_version}`.",
        f"- Data: {fingerprint}",
        f"- Train: entries ≤ {cfg.data.train_end}; **test (holdout): entries ≥ "
        f"{cfg.data.test_start}**, through {cfg.data.end}.",
        f"- Net = market-adjusted return − {cfg.costs.round_trip:.2%} flat round trip (an "
        "assumption until M6's verified cost model).",
        "",
        "## G1 decision (pre-registered rule, test period only)",
        "",
    ]
    if passing:
        lines.append(
            "**PASS** for: "
            + ", ".join(f"{c.event_type} {c.exit}" for c in passing)
            + ". These define M4's scope; the human decides."
        )
    else:
        lines.append(
            "**No confirmatory hypothesis passes: no edge found at the daily horizon.** "
            "Stop for the human (DESIGN §Backlog lists the next hypotheses)."
        )
    lines += [
        "",
        "| Type | Exit | N test | Mean net | 95% CI (bootstrap) | Hit rate | t | p (1-sided) "
        "| p (BH) | FCR lower | Verdict |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in family:
        verdict = "PASS" if c.passes else "fail: " + "; ".join(c.reasons)
        lines.append(
            f"| {c.event_type} | {c.exit} | {c.n} | {_pct(c.mean_net)} | "
            f"[{_pct(c.ci_low)}, {_pct(c.ci_high)}] | {_num(c.hit_rate, 2)} | {_num(c.t, 2)} | "
            f"{_num(c.p, 4)} | {_num(c.p_bh, 4)} | {_pct(c.fcr_low)} | {verdict} |"
        )
    lines += ["", "## All types, train vs test", ""]
    lines += [
        "| Type | Kind | Exit | Train N | Train mean net | Test N | Test mean net "
        "| Test median | Test gross |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for event_type in cfg.event_types:
        kind = "confirmatory" if event_type in cfg.events.confirmatory else "exploratory"
        for x in cfg.exits:
            tr, te = cells[(event_type, x.name, "train")], cells[(event_type, x.name, "test")]
            lines.append(
                f"| {event_type} | {kind} | {x.name} | {tr.n} | {_pct(tr.mean_net)} | {te.n} | "
                f"{_pct(te.mean_net)} | {_pct(te.median_net)} | {_pct(te.mean_gross)} |"
            )
    if figure:
        lines += ["", "## Mean abnormal return by horizon", "", f"![CAR]({figure})"]
    lines += [
        "",
        "## By liquidity (exploratory; test period, net)",
        "",
        "| Type | Median traded value | Exit | N | Mean net |",
        "|---|---|---|---|---|",
    ]
    for event_type, label, exit_name, n, mean in liquidity_table(rows, cfg):
        lines.append(f"| {event_type} | {label} | {exit_name} | {n} | {_pct(mean)} |")
    lines += ["", "## Where every filing went", "", "| Type | " + " | ".join(_REASONS) + " |"]
    lines.append("|---|" + "---|" * len(_REASONS))
    for event_type in cfg.event_types:
        lines.append(
            f"| {event_type} | "
            + " | ".join(str(counts[event_type].get(r, 0)) for r in _REASONS)
            + " |"
        )
    lines += [
        "",
        "## Spot check (verify by hand against the raw bhavcopy)",
        "",
        "| Announcement | Type | Symbol | Entry date | Entry open | d1 return | d1 index "
        "| d1 net |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in spot_check_events(rows, cfg):
        lines.append(
            f"| {r['announcement_id']} | {r['event_type']} | {r['symbol']} | {r['entry_date']} | "
            f"{r['entry_open']} | {_pct(r.get('ret_d1') or math.nan)} | "
            f"{_pct(r.get('bench_d1') or math.nan)} | {_pct(r.get('net_d1') or math.nan)} |"
        )
    lines.append("")
    return "\n".join(lines), family


_REASONS = (
    "kept",
    "excluded_category",
    "outside_window",
    "unlinked",
    "no_nse_listing",
    "no_entry_price",
    "low_price",
    "illiquid",
    "locked_entry",
    "circuit_gap",
    "review_action",
    "duplicate",
)


def trial_row(run_id: str, cfg: StudyConfig, family: list[Cell], today: date) -> str:
    passed = [f"{c.event_type} {c.exit}" for c in family if c.passes]
    verdict = "PASS: " + ", ".join(passed) if passed else "no edge found"
    return (
        f"| {run_id} | {today} | M3_prereg.md ({cfg.study}) | "
        f"{len(family)} confirmatory tests: {', '.join(cfg.events.confirmatory)} x "
        f"{', '.join(x.name for x in cfg.exits)} | train ≤ {cfg.data.train_end} / "
        f"test ≥ {cfg.data.test_start} | {verdict} | reports/M3_event_study.md |"
    )
