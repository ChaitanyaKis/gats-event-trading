"""M5's exploratory arm B: does a filing make an opening gap fade?

M3 found that buying the open after a filing loses. The mirror idea: sell
the open short when a filing made outside market hours has gapped the stock
up, and buy back at the close. But ordinary gap-ups fade too (stocks gain
overnight and give some back in the day), so a filing counts only for what
it adds beyond a matched control: stocks with no filing, on the same date,
in the same liquidity bucket, with a gap of the same size.

Exploratory by construction: the idea came from M3's own test period, so
nothing computed here can confirm it. Daily prices only (the official open
and close); the registration is docs/research/M5_prereg.md, Amendment 1.
"""

from __future__ import annotations

import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import Field

from gats.backtest.costs import CostModel, Fill
from gats.pit import AsOf
from gats.refdata.calendar import CalendarError, TradingCalendar
from gats.research.event_study import effective_availability
from gats.research.stats import cluster_bootstrap_means, clustered_mean
from gats.research.study import (
    DataSpec,
    RegistrationError,
    _Strict,
    config_hash,
    registered_hash_for,
)
from gats.timeutil import ist_datetime, to_ist

REASONS = (
    "kept",
    "in_session",
    "too_late",
    "outside_window",
    "no_symbol",
    "no_price",
    "ex_date",
    "low_price",
    "illiquid",
    "locked",
    "no_gap_up",
    "circuit_gap",
    "near_band",
    "duplicate",
)


class GapFadeFilters(_Strict):
    series: list[str]
    min_median_turnover_rs: float
    turnover_lookback_sessions: int = Field(ge=1)
    min_prev_close_rs: float
    skip_locked_day: bool


class GapFadeCosts(_Strict):
    cost_file: Path
    notional_rs: float = Field(gt=0)
    slippage_bps_per_side: float = Field(ge=0)


class GapFadeStats(_Strict):
    bootstrap_resamples: int = Field(ge=100)
    bootstrap_seed: int
    confidence: float = Field(gt=0.5, lt=1)
    min_events: int = Field(ge=1)


class GapFadeConfig(_Strict):
    study: str
    taxonomy_version: str
    data: DataSpec
    event_types: list[str] = Field(min_length=1)
    minute_precision_delay_s: int
    # A filing must be public by this time of the entry day: later ones
    # miss the opening auction, whose price is the entry.
    latest_filing_ist: time
    min_gap: float = Field(gt=0)
    gap_bins: list[float] = Field(min_length=2)
    liquidity_edges_rs: list[float] = Field(min_length=1)
    band_margin: float = Field(ge=0)
    exclude_ex_dates: bool
    filters: GapFadeFilters
    costs: GapFadeCosts
    statistics: GapFadeStats


def verify_gap_fade(config: Path, prereg: Path) -> tuple[GapFadeConfig, str]:
    """Load the study only if the config is exactly the registered one."""
    actual, expected = config_hash(config), registered_hash_for(prereg, config)
    if actual != expected:
        raise RegistrationError(
            f"{config} (sha256 {actual[:12]}...) is not the pre-registered config "
            f"({expected[:12]}...): a changed design is a new study and a new trial"
        )
    raw: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    return GapFadeConfig.model_validate(raw), actual


# --- one stock-day ----------------------------------------------------------------


@dataclass(frozen=True)
class GapDay:
    """A stock-day that opened above the previous close and could be shorted."""

    symbol: str
    day: date
    gap: float
    bucket: int  # index into the liquidity buckets
    bin: int  # index into the gap bins
    open: float
    close: float

    @property
    def short_gross(self) -> float:
        """Sell the open, buy back the close."""
        return 1 - self.close / self.open


def band_fraction(band: str | None) -> float | None:
    """'20' -> 0.20; 'No Band' (or anything unreadable) -> None."""
    try:
        return float(band) / 100 if band is not None else None
    except ValueError:
        return None


def classify(
    row: Any,
    history: list[float],
    cfg: GapFadeConfig,
    *,
    ex_date: bool,
    band: float | None,
) -> tuple[str, GapDay | None]:
    """Why a stock-day is not a tradeable gap-up, or ("kept", the gap day).
    ``history``: the turnover (lakhs) of the sessions before it, oldest first."""
    f = cfg.filters
    if not (row.open and row.close and row.prev_close and row.high and row.low):
        return "no_price", None
    if ex_date and cfg.exclude_ex_dates:
        return "ex_date", None
    if row.prev_close < f.min_prev_close_rs:
        return "low_price", None
    recent = history[-f.turnover_lookback_sessions :]
    if len(recent) < f.turnover_lookback_sessions:
        return "illiquid", None
    median_rs = statistics.median(recent) * 1e5
    if median_rs < f.min_median_turnover_rs:
        return "illiquid", None
    if f.skip_locked_day and row.high == row.low:
        return "locked", None
    gap = row.open / row.prev_close - 1
    if gap < cfg.min_gap:
        return "no_gap_up", None
    if gap >= cfg.gap_bins[-1]:
        return "circuit_gap", None
    if band is not None and gap >= band - cfg.band_margin:
        return "near_band", None
    return "kept", GapDay(
        row.symbol,
        row.trade_date,
        gap,
        bisect_right(cfg.liquidity_edges_rs, median_rs) - 1,
        bisect_right(cfg.gap_bins, gap) - 1,
        row.open,
        row.close,
    )


def short_cost(costs: CostModel, gap_day: GapDay, cfg: GapFadeConfig) -> float:
    """Charges and slippage of the short round trip, as a fraction of the
    amount sold."""
    quantity = max(1, int(cfg.costs.notional_rs // gap_day.open))
    charges = (
        costs.charges(Fill("sell", "intraday", gap_day.open, quantity, gap_day.day)).total
        + costs.charges(Fill("buy", "intraday", gap_day.close, quantity, gap_day.day)).total
    )
    return charges / (quantity * gap_day.open) + 2 * cfg.costs.slippage_bps_per_side / 1e4


def session_day(cal: TradingCalendar, moment: datetime) -> tuple[date, bool] | None:
    """(the regular session a filing belongs to, was it made inside one):
    the session it appeared in, or the next one to open."""
    try:
        inside = cal.session_of(moment)
        if inside is not None:
            return inside, True
        return to_ist(cal.next_session_open(moment)).date(), False
    except CalendarError:
        return None


# --- the study --------------------------------------------------------------------


Cell = tuple[date, int, int]  # (date, liquidity bucket, gap bin)


def run_gap_fade(
    clock: AsOf, cfg: GapFadeConfig, costs: CostModel
) -> tuple[list[dict[str, Any]], dict[Cell, list[GapDay]]]:
    """(one row per filing, the matched control stock-days by cell)."""
    cal, resolver, adjuster = clock.calendar(), clock.resolver(), clock.return_adjuster()
    bands = {symbol: band_fraction(band) for symbol, band in clock.latest_bands(cfg.filters.series)}
    start = ist_datetime(cfg.data.start, time())
    end = ist_datetime(cfg.data.end + timedelta(days=1), time())

    # The events: in-scope filings made outside market hours, each with the
    # stock-day it would trade.
    events: list[dict[str, Any]] = []
    for row in clock.typed_filings(start, end, cfg.taxonomy_version, cfg.data.source):
        if row.event_type not in cfg.event_types or row.security_id is None:
            continue
        available = effective_availability(row, cfg.minute_precision_delay_s)
        belongs = session_day(cal, available)
        event: dict[str, Any] = {
            "announcement_id": row.id,
            "security_id": row.security_id,
            "event_type": row.event_type,
            "available_at": available,
            "entry_date": None,
            "period": None,
            "filter_reason": None,
        }
        events.append(event)
        if belongs is None or not (cfg.data.start <= belongs[0] <= cfg.data.end):
            event["filter_reason"] = "outside_window"
            continue
        day, inside = belongs
        event |= {"entry_date": day, "period": _period(cfg, day)}
        event["same_morning"] = to_ist(available).date() == day
        if inside:
            event["filter_reason"] = "in_session"  # arm A's population, not this one's
        elif available > ist_datetime(day, cfg.latest_filing_ist):
            event["filter_reason"] = "too_late"  # the opening auction had taken its orders
        else:
            event["symbol"] = resolver.identifier(row.security_id, "nse_symbol", day)
            if event["symbol"] is None:
                event["filter_reason"] = "no_symbol"
    asked = {(e["symbol"], e["entry_date"]) for e in events if e["filter_reason"] is None}

    # Every stock-day in the window: the tradeable gap-ups by date, and the
    # verdict on each stock-day an event asks about.
    verdicts: dict[tuple[str, date], tuple[str, GapDay | None]] = {}
    gap_ups: dict[date, list[GapDay]] = defaultdict(list)
    history: list[float] = []
    current = None
    for row in clock.eod_range(cfg.data.start, cfg.data.end, cfg.filters.series, lead_days=60):
        if row.symbol != current:
            current, history = row.symbol, []
        if row.trade_date >= cfg.data.start:
            verdict = classify(
                row,
                history,
                cfg,
                ex_date=adjuster.on(row.symbol, row.trade_date) is not None,
                band=bands.get(row.symbol),
            )
            if verdict[1] is not None:
                gap_ups[row.trade_date].append(verdict[1])
            if (row.symbol, row.trade_date) in asked:
                verdicts[(row.symbol, row.trade_date)] = verdict
        history.append(row.turnover_lacs or 0.0)

    # Which company filed anything around which session (any type, any source).
    filed: set[tuple[int, date]] = set()
    for moment in clock.filing_moments(start, end):
        belongs = session_day(cal, effective_availability(moment, cfg.minute_precision_delay_s))
        if belongs is not None:
            filed.add((moment.security_id, belongs[0]))

    # Price the events, in the order they were filed: one per company and day.
    taken: set[tuple[int, date]] = set()
    for event in events:
        if event["filter_reason"] is not None:
            continue
        key = (event["security_id"], event["entry_date"])
        if key in taken:
            event["filter_reason"] = "duplicate"
            continue
        reason, gap_day = verdicts.get((event["symbol"], event["entry_date"]), ("no_price", None))
        if gap_day is None:
            event["filter_reason"] = reason
            continue
        taken.add(key)
        gross = gap_day.short_gross
        event |= {
            "gap": gap_day.gap,
            "bucket": gap_day.bucket,
            "bin": gap_day.bin,
            "short_gross": gross,
            "short_net": gross - short_cost(costs, gap_day, cfg),
        }

    # The controls: gap-ups with no filing, in the cells the events fall in.
    wanted = {
        (e["entry_date"], e["bucket"], e["bin"]) for e in events if e["filter_reason"] is None
    }
    controls: dict[Cell, list[GapDay]] = defaultdict(list)
    for day in sorted({cell[0] for cell in wanted}):
        for gap_day in gap_ups[day]:
            cell = (day, gap_day.bucket, gap_day.bin)
            if cell not in wanted:
                continue
            security = resolver.resolve("nse_symbol", gap_day.symbol, day)
            if security is None or (security, day) in filed:
                continue  # unknown company, or it filed something: not a control
            controls[cell].append(gap_day)
    for event in events:
        if event["filter_reason"] is None:
            matched = controls.get((event["entry_date"], event["bucket"], event["bin"]), [])
            event["controls"] = len(matched)
            if matched:
                event["control_gross"] = sum(g.short_gross for g in matched) / len(matched)
                event["effect"] = event["short_gross"] - event["control_gross"]
    return events, dict(controls)


def control_records(controls: dict[Cell, list[GapDay]]) -> list[dict[str, Any]]:
    """The control stock-days as rows, so a run can be checked by hand."""
    return [
        {
            "entry_date": day,
            "bucket": bucket,
            "bin": gap_bin,
            "symbol": g.symbol,
            "gap": g.gap,
            "open": g.open,
            "close": g.close,
            "short_gross": g.short_gross,
        }
        for (day, bucket, gap_bin), members in sorted(controls.items())
        for g in members
    ]


def _period(cfg: GapFadeConfig, day: date) -> str | None:
    if day <= cfg.data.train_end:
        return "train"
    return "test" if day >= cfg.data.test_start else None


# --- the report -------------------------------------------------------------------


@dataclass(frozen=True)
class Estimate:
    n: int
    mean: float
    lower: float  # one-sided bootstrap lower bound
    t: float

    @property
    def above_zero(self) -> bool:
        return self.n > 0 and self.mean > 0 and self.lower > 0


def estimate(values: list[float], days: list[date], cfg: GapFadeConfig) -> Estimate:
    """Mean with a date-clustered t and a one-sided bootstrap lower bound."""
    if not values:
        return Estimate(0, float("nan"), float("nan"), float("nan"))
    stats = cfg.statistics
    means = cluster_bootstrap_means(values, days, stats.bootstrap_resamples, stats.bootstrap_seed)
    lower = float(np.quantile(means, 1 - stats.confidence))
    summary = clustered_mean(values, days)
    return Estimate(summary.n, summary.mean, lower, summary.t)


def _pct(x: float) -> str:
    return "n/a" if x != x else f"{x * 100:+.2f}%"


def _line(label: str, e: Estimate) -> str:
    return f"| {label} | {e.n} | {_pct(e.mean)} | {_pct(e.lower)} | {e.t:.2f} |"


def supported(events: list[dict[str, Any]], cfg: GapFadeConfig) -> tuple[bool, list[str]]:
    """Is the idea worth a forward test? Test period only: enough matched
    events, the short earns after costs, and the filing adds to the fade
    beyond its matched controls. Both with a lower bound above zero."""
    test = [e for e in events if e["filter_reason"] is None and e.get("period") == "test"]
    matched = [e for e in test if "effect" in e]
    days = [e["entry_date"] for e in matched]
    net = estimate([e["short_net"] for e in matched], days, cfg)
    effect = estimate([e["effect"] for e in matched], days, cfg)
    missing = []
    if len(matched) < cfg.statistics.min_events:
        missing.append(f"only {len(matched)} matched events (needs {cfg.statistics.min_events})")
    if not net.above_zero:
        missing.append("the short does not earn after costs (lower bound not above zero)")
    if not effect.above_zero:
        missing.append("the filing adds nothing beyond its matched controls")
    return not missing, missing


def build_gap_fade_report(
    events: list[dict[str, Any]],
    cfg: GapFadeConfig,
    *,
    digest: str,
    run_id: str,
    experiment_id: int,
) -> str:
    ok, missing = supported(events, cfg)
    verdict = (
        "supported on history: worth a forward test (paper or forward data only)"
        if ok
        else "not supported: " + "; ".join(missing)
    )
    confidence = cfg.statistics.confidence
    lines = [
        "# M5 arm B (exploratory): the gap fade",
        "",
        f"Study `{cfg.study}`, config `{digest[:12]}` (registered in "
        f"`docs/research/M5_prereg.md`, Amendment 1), run {run_id}, experiment #{experiment_id}.",
        "",
        f"**Exploratory result: {verdict}.**",
        "",
        "Suggested by M3's own data, so this history cannot confirm it whatever it shows. "
        "The trade: short at the open, cover at the close, for "
        f"{', '.join(cfg.event_types)} filings made outside market hours that opened at least "
        f"{cfg.min_gap:.0%} up. A control is a gap-up of the same size bin, on the same date, "
        "in the same liquidity bucket, of a company that filed nothing.",
        "",
        f"Lower bounds are one-sided, {confidence:.0%}, from a bootstrap over dates.",
    ]
    for period in ("test", "train"):
        kept = [e for e in events if e["filter_reason"] is None and e.get("period") == period]
        matched = [e for e in kept if "effect" in e]
        days = [e["entry_date"] for e in matched]
        all_days = [e["entry_date"] for e in kept]
        lines += [
            "",
            f"## {period.capitalize()} period",
            "",
            "| Measure | N | Mean | Lower bound | t |",
            "|---|---|---|---|---|",
            _line(
                "Short, before costs (all events)",
                estimate([e["short_gross"] for e in kept], all_days, cfg),
            ),
            _line(
                "Short, after costs (all events)",
                estimate([e["short_net"] for e in kept], all_days, cfg),
            ),
            _line(
                "Short, after costs (matched events)",
                estimate([e["short_net"] for e in matched], days, cfg),
            ),
            _line(
                "Matched controls, before costs",
                estimate([e["control_gross"] for e in matched], days, cfg),
            ),
            _line(
                "Filing effect (event minus its controls)",
                estimate([e["effect"] for e in matched], days, cfg),
            ),
        ]
        morning = [e for e in matched if e.get("same_morning")]
        lines.append(
            _line(
                "Filing effect, filed the same morning only",
                estimate([e["effect"] for e in morning], [e["entry_date"] for e in morning], cfg),
            )
        )
        controls = sum(e["controls"] for e in matched)
        lines.append("")
        lines.append(
            f"{len(kept)} events, {len(matched)} with at least one control "
            f"({controls / max(len(matched), 1):.1f} controls each on average)."
        )
    reasons = Counter(e["filter_reason"] or "kept" for e in events)
    lines += ["", "## Every filing accounted for", "", "| Outcome | Filings |", "|---|---|"]
    lines += [f"| {reason} | {reasons[reason]} |" for reason in REASONS if reasons[reason]]
    lines.append("")
    return "\n".join(lines)
