"""Intraday reaction to disclosures (M5, T5.3): what is left after we can act.

For each event the engine finds the first one-minute bar a trader with our
measured latency could have bought in, and the abnormal return from there
to fixed exits, net of verified costs. The design is fixed in
docs/research/M5_prereg.md; this module only carries it out.

Look-ahead guards, tested in tests/test_reaction.py:

- the entry bar starts at or after availability + delay, so a later
  availability or a longer delay can never give an earlier entry;
- nothing before the entry bar enters a return;
- filters use only end-of-day data from before the entry date.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from statistics import median, quantiles
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator
from sqlalchemy import Connection, select

from gats.backtest.costs import CostModel, Fill
from gats.db.schema import announcements
from gats.marketdata.bars import read_bars
from gats.marketdata.windows import EventWindow, event_windows
from gats.pit import AsOf
from gats.refdata.calendar import CalendarError, TradingCalendar
from gats.research.study import (
    BenchmarkSpec,
    DataSpec,
    EventSpec,
    RegistrationError,
    StatSpec,
    _Strict,
    config_hash,
    registered_hash_for,
)
from gats.sources.upstox import Bar
from gats.timeutil import ist_datetime, to_ist

SLIPPAGE_SENSITIVITY_BPS = (0.0, 10.0)


class EntryRule(_Strict):
    latency_percentile: int = Field(ge=50, le=99)
    latency_lookback_days: int = Field(ge=1)
    latency_min_filings: int = Field(ge=1)
    processing_allowance_s: float = Field(ge=0)
    order_allowance_s: float = Field(ge=0)
    cutoff_ist: time
    max_wait_minutes: int = Field(ge=1)
    # Which filings can pass the gate: "session" (decided inside a regular
    # session before the cutoff) and/or "overnight" (entered at the next open).
    confirmatory_strata: list[Literal["session", "overnight"]] = Field(min_length=1)


class Horizon(_Strict):
    name: str
    minutes: int | None = Field(default=None, ge=1)
    at: Literal["square_off", "next_close"] | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Horizon:
        if (self.minutes is None) == (self.at is None):
            raise ValueError(f"exit {self.name}: give either minutes or at")
        return self


class ReactionFilters(_Strict):
    series: list[str]
    min_median_turnover_rs: float
    turnover_lookback_sessions: int
    min_prev_close_rs: float
    skip_locked_entry_bar: bool


class ReactionCosts(_Strict):
    cost_file: Path
    notional_rs: float = Field(gt=0)
    slippage_bps_per_side: float = Field(ge=0)


class ReactionConfig(_Strict):
    study: str
    taxonomy_version: str
    data: DataSpec
    events: EventSpec
    entry: EntryRule
    exits: list[Horizon]
    extra_exits: list[Horizon]
    square_off_ist: time
    benchmark: BenchmarkSpec
    filters: ReactionFilters
    costs: ReactionCosts
    statistics: StatSpec

    @property
    def event_types(self) -> list[str]:
        return [*self.events.confirmatory, *self.events.exploratory]

    @property
    def all_exits(self) -> list[Horizon]:
        return [*self.exits, *self.extra_exits]


def verify_reaction(config: Path, prereg: Path) -> tuple[ReactionConfig, str]:
    """Load the study only if the config is exactly the registered one."""
    actual, expected = config_hash(config), registered_hash_for(prereg, config)
    if actual != expected:
        raise RegistrationError(
            f"{config} (sha256 {actual[:12]}...) is not the pre-registered config "
            f"({expected[:12]}...): a changed design is a new study and a new trial"
        )
    raw: Any = yaml.safe_load(config.read_text(encoding="utf-8"))
    return ReactionConfig.model_validate(raw), actual


# --- latency --------------------------------------------------------------------------


class NotReady(RuntimeError):
    """The data a pre-registered run needs is not there yet."""


@dataclass(frozen=True)
class Delay:
    feed_s: float  # the measured percentile of live feed latency
    filings: int
    total_s: float  # feed + processing + order allowances


def measured_delay(
    conn: Connection, cfg: ReactionConfig, now: datetime, *, percentile: int | None = None
) -> Delay:
    """Decision delay from our own live recording (never assumed)."""
    rule = cfg.entry
    since = now - timedelta(days=rule.latency_lookback_days)
    a = announcements
    lags = sorted(
        (row.available_at - row.exch_disseminated_ts).total_seconds()
        for row in conn.execute(
            select(a.c.available_at, a.c.exch_disseminated_ts).where(
                a.c.ingest_mode == "live",
                a.c.source == cfg.data.source,
                a.c.available_at >= since,
                a.c.available_at <= now,
                a.c.exch_disseminated_ts.is_not(None),
            )
        )
    )
    if len(lags) < rule.latency_min_filings:
        raise NotReady(
            f"only {len(lags)} live {cfg.data.source} filings in the last "
            f"{rule.latency_lookback_days} days (need {rule.latency_min_filings}): the recorder "
            "must run before latency can be measured"
        )
    cut = quantiles(lags, n=100, method="inclusive")[(percentile or rule.latency_percentile) - 1]
    feed = max(cut, 0.0)
    return Delay(feed, len(lags), feed + rule.processing_allowance_s + rule.order_allowance_s)


# --- one event ------------------------------------------------------------------------


def _at_or_after(bars: Sequence[Bar], moment: datetime) -> int | None:
    index = bisect_left([b.ts for b in bars], moment)
    return index if index < len(bars) else None


def _same_day(bars: Sequence[Bar], day: date) -> list[Bar]:
    return [b for b in bars if to_ist(b.ts).date() == day]


def earliest_tradable(
    cal: TradingCalendar, decided: datetime, cutoff: time
) -> tuple[datetime, str] | None:
    """(first minute an order could trade, stratum), or None off the calendar."""
    try:
        in_session = cal.session_of(decided) is not None and to_ist(decided).time() < cutoff
        if in_session:
            return decided, "session"
        return cal.next_session_open(decided), "overnight"
    except CalendarError:
        return None


def cost_fraction(
    costs: CostModel,
    entry: float,
    exit_price: float,
    notional: float,
    slippage_bps: float,
    day: date,
    *,
    delivery: bool = False,
) -> float:
    """Round-trip charges and slippage as a fraction of the amount bought."""
    quantity = max(1, int(notional // entry))
    product: Literal["delivery", "intraday"] = "delivery" if delivery else "intraday"
    charges = (
        costs.charges(Fill("buy", product, entry, quantity, day)).total
        + costs.charges(Fill("sell", product, exit_price, quantity, day)).total
    )
    return charges / (quantity * entry) + 2 * slippage_bps / 1e4


def react(
    window: EventWindow,
    stock: Sequence[Bar],
    index: Sequence[Bar],
    cfg: ReactionConfig,
    cal: TradingCalendar,
    delay_s: float,
    costs: CostModel,
) -> dict[str, Any]:
    """One event's entry and returns; ``filter_reason`` says why there are none."""
    decided = window.available_at + timedelta(seconds=delay_s)
    row: dict[str, Any] = {
        "announcement_id": window.announcement_id,
        "event_type": window.event_type,
        "security_id": window.security_id,
        "available_at": window.available_at,
        "decided_at": decided,
        "filter_reason": None,
        "entry_date": None,
    }
    start = earliest_tradable(cal, decided, cfg.entry.cutoff_ist)
    if start is None:
        return row | {"filter_reason": "outside_calendar"}
    earliest, row["stratum"] = start
    day = to_ist(earliest).date()
    today = _same_day(stock, day)
    at = _at_or_after(today, earliest)
    late = earliest + timedelta(minutes=cfg.entry.max_wait_minutes)
    if at is None or today[at].ts >= late or to_ist(today[at].ts).time() >= cfg.entry.cutoff_ist:
        return row | {"filter_reason": "no_bar", "entry_date": day}
    entry_bar = today[at]
    row |= {"entry_date": day, "entry_at": entry_bar.ts, "entry_price": entry_bar.open}
    # Flat at the day's high so far (which the first bar of a day always is):
    # the pre-registered sign of a stock locked at its upper circuit.
    flat = entry_bar.high == entry_bar.low
    at_high = entry_bar.high >= max((b.high for b in today[:at]), default=entry_bar.high)
    if cfg.filters.skip_locked_entry_bar and flat and at_high:
        return row | {"filter_reason": "locked_entry"}
    index_today = _same_day(index, day)
    index_at = _at_or_after(index_today, entry_bar.ts)
    if index_at is None:
        return row | {"filter_reason": "no_benchmark"}
    index_entry = index_today[index_at].open
    square_off = ist_datetime(day, cfg.square_off_ist)
    for horizon in cfg.all_exits:
        prices = _exit_prices(horizon, entry_bar.ts, day, square_off, stock, index, cal)
        if prices is None:
            continue
        stock_exit, index_exit = prices
        gross = stock_exit / entry_bar.open - 1
        abnormal = gross - (index_exit / index_entry - 1)
        delivery = horizon.at == "next_close"
        row[f"gross_{horizon.name}"] = gross
        row[f"abnormal_{horizon.name}"] = abnormal
        for label, bps in (
            ("net", cfg.costs.slippage_bps_per_side),
            *((f"net{int(b)}", b) for b in SLIPPAGE_SENSITIVITY_BPS),
        ):
            charge = cost_fraction(
                costs,
                entry_bar.open,
                stock_exit,
                cfg.costs.notional_rs,
                bps,
                day,
                delivery=delivery,
            )
            row[f"{label}_{horizon.name}"] = abnormal - charge
    if row["stratum"] == "session":
        row |= _placebo(window, stock, index, cfg, entry_bar.ts, cal)
    return row


def _placebo(
    window: EventWindow,
    stock: Sequence[Bar],
    index: Sequence[Bar],
    cfg: ReactionConfig,
    entered: datetime,
    cal: TradingCalendar,
) -> dict[str, float]:
    """The same windows one session earlier: same stock, same clock time, no
    filing. Abnormal returns before costs (``placebo_<exit>``): the baseline
    an event's returns are read against, because an ordinary stock is not
    flat against the index within a day."""
    before = window.sessions[0]
    moment = ist_datetime(before, to_ist(entered).time())
    ours, theirs = _same_day(stock, before), _same_day(index, before)
    at, index_at = _at_or_after(ours, moment), _at_or_after(theirs, moment)
    if at is None or index_at is None:
        return {}
    start = ours[at]
    if start.ts >= moment + timedelta(minutes=cfg.entry.max_wait_minutes):
        return {}
    square_off = ist_datetime(before, cfg.square_off_ist)
    found: dict[str, float] = {}
    for horizon in cfg.exits:
        prices = _exit_prices(horizon, start.ts, before, square_off, stock, index, cal)
        if prices is not None:
            stock_exit, index_exit = prices
            found[f"placebo_{horizon.name}"] = (
                stock_exit / start.open - 1 - (index_exit / theirs[index_at].open - 1)
            )
    return found


def _exit_prices(
    horizon: Horizon,
    entered: datetime,
    day: date,
    square_off: datetime,
    stock: Sequence[Bar],
    index: Sequence[Bar],
    cal: TradingCalendar,
) -> tuple[float, float] | None:
    """(stock price, index price) at the exit, or None when it does not exist."""
    if horizon.at == "next_close":
        try:
            following = cal.shift(day, 1)
        except CalendarError:
            return None
        ours, theirs = _same_day(stock, following), _same_day(index, following)
        return (ours[-1].close, theirs[-1].close) if ours and theirs else None
    target = (
        square_off
        if horizon.at == "square_off"
        else entered + timedelta(minutes=horizon.minutes or 0)
    )
    if target > square_off:
        return None  # the position would have been squared off first
    ours, theirs = _same_day(stock, day), _same_day(index, day)
    stock_at, index_at = _at_or_after(ours, target), _at_or_after(theirs, target)
    if stock_at is None or index_at is None:
        return None
    return ours[stock_at].open, theirs[index_at].open


# --- the study ------------------------------------------------------------------------


def run_reaction_study(
    clock: AsOf,
    cfg: ReactionConfig,
    bars_dir: Path,
    *,
    delay_s: float,
    scope: Collection[str],
    costs: CostModel,
    index_key: str,
) -> list[dict[str, Any]]:
    """Every in-scope event with its filter reason or its returns."""
    cal = clock.calendar()
    resolver = clock.resolver()
    windows, _ = event_windows(
        clock,
        event_types=set(scope),
        taxonomy_version=cfg.taxonomy_version,
        start=cfg.data.start,
        end=cfg.data.end,
        source=cfg.data.source,
        minute_precision_delay_s=cfg.events.minute_precision_delay_s,
        exclude_categories=cfg.events.exclude_categories,
    )
    index_cache: dict[date, list[Bar]] = {}

    def bars_for(key: str, days: Sequence[date]) -> list[Bar]:
        first = ist_datetime(min(days), time())
        return read_bars(bars_dir, key, first, ist_datetime(max(days) + timedelta(days=1), time()))

    def index_for(days: Sequence[date]) -> list[Bar]:
        out: list[Bar] = []
        for day in days:
            if day not in index_cache:
                index_cache[day] = bars_for(index_key, [day])
            out += index_cache[day]
        return out

    eod: dict[str, list[Any]] = {}
    last_kept: dict[tuple[int, str], date] = {}
    rows = []
    for window in sorted(windows, key=lambda w: (w.available_at, w.announcement_id)):
        row = react(
            window,
            bars_for(window.instrument_key, window.sessions),
            index_for(window.sessions),
            cfg,
            cal,
            delay_s,
            costs,
        )
        entry_date: date | None = row["entry_date"]
        row["period"] = _period(cfg, entry_date)
        if row["filter_reason"] is None and entry_date is not None:
            symbol = resolver.identifier(window.security_id, "nse_symbol", entry_date)
            if symbol is not None and symbol not in eod:
                eod[symbol] = clock.eod_history(symbol, series=cfg.filters.series[0])
            history = [r for r in eod.get(symbol or "", []) if r.trade_date < entry_date]
            recent = history[-cfg.filters.turnover_lookback_sessions :]
            turnover = median((r.turnover_lacs or 0.0) * 1e5 for r in recent) if recent else None
            row["median_turnover_rs"] = turnover
            key = (window.security_id, window.event_type)
            previous = last_kept.get(key)
            cooldown = cfg.events.same_type_cooldown_sessions
            if len(recent) < cfg.filters.turnover_lookback_sessions:
                row["filter_reason"] = "no_price_history"
            elif turnover is None or turnover < cfg.filters.min_median_turnover_rs:
                row["filter_reason"] = "illiquid"
            elif (history[-1].close or 0.0) < cfg.filters.min_prev_close_rs:
                row["filter_reason"] = "low_price"
            elif previous is not None and cal.shift(previous, cooldown) >= entry_date:
                row["filter_reason"] = "duplicate"  # the same rule and name as M3
            else:
                last_kept[key] = entry_date
        rows.append(row)
    return rows


def _period(cfg: ReactionConfig, entry_date: date | None) -> str | None:
    if entry_date is None:
        return None
    if cfg.data.start <= entry_date <= cfg.data.train_end:
        return "train"
    if cfg.data.test_start <= entry_date <= cfg.data.end:
        return "test"
    return None
