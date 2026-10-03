"""The G2 report: a backtest judged against the gate, mechanically (T6.8).

DESIGN's G2 row: walk-forward out-of-sample, full costs, at least 300
trades, deflated Sharpe > 0 at p < 0.05 (a deflated Sharpe ratio above
0.95), maximum drawdown within the configured limit, capacity at least
the planned size. Each criterion is computed, never argued; a criterion
that cannot be computed counts as not met, and a run that is not a
pre-registered holdout run is reported as a development run, not a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gats.backtest.ledger import Metrics

MIN_TRADES = 300
MIN_DEFLATED_SHARPE = 0.95
MIN_CAPACITY_MULTIPLE = 1.0


@dataclass(frozen=True)
class Check:
    name: str
    required: str
    observed: str
    passed: bool


def g2_checks(
    metrics: Metrics,
    *,
    holdout: bool,
    deflated_sharpe: float | None,
    trials: int,
    max_drawdown_limit: float,
) -> list[Check]:
    def shown(value: float | None, pattern: str) -> str:
        return "cannot be estimated" if value is None else format(value, pattern)

    capacity = metrics.capacity_multiple
    return [
        Check(
            "Out-of-sample, pre-registered design",
            "holdout run with a recorded design",
            "yes" if holdout else "no (development window)",
            holdout,
        ),
        Check("Trades", f">= {MIN_TRADES}", str(metrics.trades), metrics.trades >= MIN_TRADES),
        Check(
            f"Deflated Sharpe ratio ({trials} design(s) tried)",
            f"> {MIN_DEFLATED_SHARPE}",
            shown(deflated_sharpe, ".3f"),
            deflated_sharpe is not None and deflated_sharpe > MIN_DEFLATED_SHARPE,
        ),
        Check(
            "Maximum drawdown",
            f"<= {max_drawdown_limit:.1%}",
            f"{metrics.max_drawdown:.1%}",
            metrics.max_drawdown <= max_drawdown_limit,
        ),
        Check(
            "Capacity at the planned size",
            f">= {MIN_CAPACITY_MULTIPLE:.0f}x (25th percentile entry)",
            shown(capacity, ".1f") + ("x" if capacity is not None else ""),
            capacity is not None and capacity >= MIN_CAPACITY_MULTIPLE,
        ),
        Check(
            "Ledger reconciles to the paisa",
            "gap < Rs 0.005",
            f"Rs {abs(metrics.reconciliation_gap):.4f}",
            abs(metrics.reconciliation_gap) < 0.005,
        ),
    ]


def g2_verdict(checks: list[Check]) -> str:
    if all(c.passed for c in checks):
        return "PASS"
    if not checks[0].passed:
        return "NOT A G2 RUN"
    return "FAIL"


def render(
    *,
    run_id: int,
    design: dict[str, Any],
    window: str,
    metrics: Metrics,
    checks: list[Check],
) -> str:
    """The Markdown report. Numbers only: the decision is the human's."""
    verdict = g2_verdict(checks)

    def money(value: float | None) -> str:
        return "n/a" if value is None else f"Rs {value:,.2f}"

    def ratio(value: float | None, pattern: str = ".2f") -> str:
        return "n/a" if value is None else format(value, pattern)

    lines = [
        "# M6 backtest report (gate G2)",
        "",
        f"Experiment #{run_id} · window {window} · **{verdict}**",
        "",
        "## Design",
        "",
        *[f"- {name}: `{value}`" for name, value in design.items() if name != "engine"],
        f"- engine: `{design.get('engine')}`",
        "",
        "## G2 criteria (DESIGN, Gates)",
        "",
        "| Criterion | Required | Observed | Met |",
        "|---|---|---|---|",
        *[
            f"| {c.name} | {c.required} | {c.observed} | {'yes' if c.passed else 'NO'} |"
            for c in checks
        ],
        "",
        "## Results",
        "",
        f"- Trades: {metrics.trades} (win rate {ratio(metrics.win_rate, '.1%')}, "
        f"profit factor {ratio(metrics.profit_factor)})",
        f"- Net P&L: {money(metrics.net_pnl)} (gross {money(metrics.gross_pnl)}, "
        f"charges {money(metrics.charges)})",
        f"- Average net per trade: {money(metrics.avg_net_per_trade)}",
        f"- Total return: {metrics.total_return:.2%} over {metrics.days} sessions",
        f"- Sharpe {ratio(metrics.sharpe)}, Sortino {ratio(metrics.sortino)} "
        "(daily, annualised, idle sessions flat)",
        f"- Maximum drawdown: {metrics.max_drawdown:.2%} ({money(metrics.max_drawdown_rs)})",
        f"- Turnover: {money(metrics.turnover)} ({ratio(metrics.turnover_ratio, '.1f')}x equity)",
        "",
        "## Tax categories (informational only, no tax is computed)",
        "",
        *(
            [f"- {name}: {money(value)}" for name, value in sorted(metrics.tax.items())]
            or ["- none"]
        ),
        "",
    ]
    if verdict == "NOT A G2 RUN":
        lines += [
            "This run did not use the pre-registered holdout, so it decides nothing. "
            "It may guide development; it may not be tuned against a later holdout run.",
            "",
        ]
    return "\n".join(lines)
