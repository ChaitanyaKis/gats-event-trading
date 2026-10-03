"""Daily event study engine (M3, T3.3).

Turns typed filings into trades exactly as the pre-registration describes
(docs/research/M3_prereg.md): enter at the open of the first regular session
strictly after the filing became available, exit at fixed closes, measure the
return net of a flat cost against the Nifty 500 over the same window.

Everything is read through :class:`gats.pit.AsOf`, and the only input that
decides *when* a trade happens is ``available_at``; the leak tests in
tests/test_event_study.py check that a later availability can never produce
an earlier entry, and that prices after an exit cannot change its result.

Every event keeps the reason it was filtered (or None), so the report can
account for every filing.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from gats.pit import AsOf
from gats.refdata.calendar import CalendarError, TradingCalendar
from gats.research.study import StudyConfig
from gats.timeutil import IST, ist_datetime, to_ist


@dataclass
class EventResult:
    announcement_id: int
    security_id: int | None
    event_type: str
    confirmatory: bool
    available_at: datetime
    entry_date: date | None = None
    symbol: str | None = None
    period: str | None = None  # train | test | None (outside both)
    filter_reason: str | None = None
    entry_open: float | None = None
    median_turnover_rs: float | None = None
    # per exit name -> value (None when the exit price is missing)
    ret: dict[str, float | None] = field(default_factory=dict)
    bench: dict[str, float | None] = field(default_factory=dict)
    abnormal: dict[str, float | None] = field(default_factory=dict)
    net: dict[str, float | None] = field(default_factory=dict)

    @property
    def kept(self) -> bool:
        return self.filter_reason is None


@dataclass
class _Bar:
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    prev_close: float | None
    turnover_rs: float


def effective_availability(row: Any, delay_s: int) -> datetime:
    """When the filing could have been acted on.

    Before NSE published seconds (no dissemination time; the receipt time at
    ``:00``), the filing may have appeared any time in that minute, so it
    counts as available at the minute's end. Never earlier than recorded.
    """
    available: datetime = row.available_at
    if row.exch_disseminated_ts is None and to_ist(row.event_ts).second == 0:
        available = max(available, row.event_ts + timedelta(seconds=delay_s))
    return available


def entry_session(cal: TradingCalendar, available_at: datetime) -> date | None:
    """The pre-registered entry rule: first regular session open strictly
    after availability (as an IST date), or None if the calendar ends."""
    try:
        return to_ist(cal.next_session_open(available_at)).date()
    except CalendarError:
        return None


def _bars(
    clock: AsOf, symbol_windows: Sequence[tuple[str, date, date | None]], series: str, since: date
) -> dict[date, _Bar]:
    """Daily bars of one security across its NSE symbols (each used only
    inside its own validity window)."""
    bars: dict[date, _Bar] = {}
    for symbol, valid_from, valid_to in symbol_windows:
        for row in clock.eod_history(symbol, series=series, start=max(since, valid_from)):
            if valid_to is not None and row.trade_date >= valid_to:
                continue
            bars[row.trade_date] = _Bar(
                row.open,
                row.high,
                row.low,
                row.close,
                row.prev_close,
                (row.turnover_lacs or 0.0) * 1e5,
            )
    return bars


def run_event_study(
    clock: AsOf,
    cfg: StudyConfig,
    keep: Callable[[Any, datetime], str | None] | None = None,
) -> list[EventResult]:
    """Build every event with its filter reason and returns. ``keep`` is an
    extra pre-registered filter: given a filing and its availability, it
    returns None to keep it or the reason it is dropped (before pricing)."""
    cal = clock.calendar()
    adjuster = clock.return_adjuster()
    resolver = clock.resolver()
    bench = {
        row.trade_date: (row.open, row.close) for row in clock.index_history(cfg.benchmark.index)
    }
    wanted = set(cfg.event_types)
    confirmatory = set(cfg.events.confirmatory)
    excluded = {c.lower() for c in cfg.events.exclude_categories}
    start = ist_datetime(cfg.data.start, datetime.min.time()) - timedelta(days=7)
    end = ist_datetime(cfg.data.end + timedelta(days=1), datetime.min.time())

    by_security: dict[int | None, list[EventResult]] = defaultdict(list)
    results: list[EventResult] = []
    for row in clock.typed_filings(start, end, cfg.taxonomy_version, cfg.data.source):
        if row.event_type not in wanted:
            continue
        event = EventResult(
            announcement_id=row.id,
            security_id=row.security_id,
            event_type=row.event_type,
            confirmatory=row.event_type in confirmatory,
            available_at=effective_availability(row, cfg.events.minute_precision_delay_s),
        )
        results.append(event)
        if (row.category or "").lower() in excluded or (row.subject or "").lower() in excluded:
            event.filter_reason = "excluded_category"
            continue
        event.entry_date = entry_session(cal, event.available_at)
        if event.entry_date is None or not (cfg.data.start <= event.entry_date <= cfg.data.end):
            event.filter_reason = "outside_window"
            continue
        if row.security_id is None:
            event.filter_reason = "unlinked"
            continue
        if keep is not None and (reason := keep(row, event.available_at)) is not None:
            event.filter_reason = reason
            continue
        by_security[row.security_id].append(event)

    lookback = cfg.filters.turnover_lookback_sessions
    since = cfg.data.start - timedelta(days=60)
    for security_id, events in by_security.items():
        assert security_id is not None
        windows = resolver.windows_of(security_id, "nse_symbol")
        if not windows:
            for event in events:
                event.filter_reason = "no_nse_listing"
            continue
        bars = {series: _bars(clock, windows, series, since) for series in cfg.filters.series}
        last_kept: dict[str, date] = {}
        for event in sorted(events, key=lambda e: (e.available_at, e.announcement_id)):
            _price_event(event, cfg, cal, adjuster, resolver, bars, bench, lookback, last_kept)
    return results


def _price_event(
    event: EventResult,
    cfg: StudyConfig,
    cal: TradingCalendar,
    adjuster: Any,
    resolver: Any,
    bars_by_series: dict[str, dict[date, _Bar]],
    bench: dict[date, tuple[float | None, float | None]],
    lookback: int,
    last_kept: dict[str, date],
) -> None:
    entry = event.entry_date
    assert entry is not None and event.security_id is not None
    event.symbol = resolver.identifier(event.security_id, "nse_symbol", entry)
    bars = next(
        (b for series in cfg.filters.series if entry in (b := bars_by_series[series])), None
    )
    if bars is None or event.symbol is None:
        event.filter_reason = "no_entry_price"
        return
    bar = bars[entry]
    if bar.open is None or bar.prev_close is None or bar.open <= 0:
        event.filter_reason = "no_entry_price"
        return
    event.entry_open = bar.open
    event.period = (
        "train" if entry <= cfg.data.train_end else "test" if entry >= cfg.data.test_start else None
    )

    # Filters, in a fixed order so each event gets one reason.
    if bar.prev_close < cfg.filters.min_prev_close_rs:
        event.filter_reason = "low_price"
        return
    history = [cal.shift(entry, -k) for k in range(1, lookback + 1)]
    turnover = [bars[d].turnover_rs if d in bars else 0.0 for d in history]
    event.median_turnover_rs = statistics.median(turnover)
    if event.median_turnover_rs < cfg.filters.min_median_turnover_rs:
        event.filter_reason = "illiquid"
        return
    if cfg.filters.skip_locked_entry_day and bar.high is not None and bar.high == bar.low:
        event.filter_reason = "locked_entry"
        return
    # The gap is measured against the split-adjusted previous close.
    gap = bar.open * adjuster.multiplier(event.symbol, entry) / bar.prev_close - 1
    if abs(gap) >= cfg.filters.max_entry_gap:
        event.filter_reason = "circuit_gap"
        return
    exits = {x.name: cal.shift(entry, x.sessions_after_entry) for x in cfg.exits}
    last_exit = max(exits.values())
    if cfg.filters.exclude_review_actions and any(
        adjuster.needs_review(event.symbol, d) for d in cal.trading_days(entry, last_exit)
    ):
        event.filter_reason = "review_action"
        return
    previous = last_kept.get(event.event_type)
    cooldown = cfg.events.same_type_cooldown_sessions
    if previous is not None and cal.shift(previous, cooldown) >= entry:
        event.filter_reason = "duplicate"
        return
    last_kept[event.event_type] = entry

    bench_open = bench.get(entry, (None, None))[0]
    for name, exit_day in exits.items():
        exit_bar = bars.get(exit_day)
        stock: float | None = None
        if exit_bar is not None and exit_bar.close is not None:
            multiplier = 1.0
            for d in cal.trading_days(entry + timedelta(days=1), exit_day):
                multiplier *= adjuster.multiplier(event.symbol, d)
            stock = exit_bar.close * multiplier / bar.open - 1
        bench_close = bench.get(exit_day, (None, None))[1]
        index: float | None = bench_close / bench_open - 1 if bench_open and bench_close else None
        abnormal = stock - index if stock is not None and index is not None else None
        event.ret[name] = stock
        event.bench[name] = index
        event.abnormal[name] = abnormal
        event.net[name] = abnormal - cfg.costs.round_trip if abnormal is not None else None


def filter_counts(results: Iterable[EventResult]) -> dict[str, dict[str, int]]:
    """Events per type by filter reason ("kept" for events that survived)."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for event in results:
        counts[event.event_type][event.filter_reason or "kept"] += 1
    return {k: dict(v) for k, v in counts.items()}


def to_records(results: Iterable[EventResult], exit_names: Sequence[str]) -> list[dict[str, Any]]:
    """Flat rows (one column per measure and exit) for Parquet and reports."""
    rows = []
    for event in results:
        row = {
            k: v for k, v in asdict(event).items() if k not in ("ret", "bench", "abnormal", "net")
        }
        for name in exit_names:
            for measure in ("ret", "bench", "abnormal", "net"):
                row[f"{measure}_{name}"] = getattr(event, measure).get(name)
        rows.append(row)
    return rows


def write_parquet(rows: Sequence[dict[str, Any]], path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path.parent.mkdir(parents=True, exist_ok=True)
    columns: dict[str, list[Any]] = {
        key: [row.get(key) for row in rows] for key in (rows[0] if rows else {})
    }
    for key, values in columns.items():
        if any(isinstance(v, datetime) for v in values):
            columns[key] = [v.astimezone(IST) if isinstance(v, datetime) else v for v in values]
    pq.write_table(pa.table(columns), path)
