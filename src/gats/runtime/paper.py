"""The paper runtime (T7.1): the backtest engine, fed live.

One loop, a tick every few seconds:

1. **Filings.** New in-scope filings are read from the database, where the
   recorder's hand-off has typed, linked and read them, and turned into
   events by the same code the backtest uses, as of now. Facts are extracted
   from the text here: rules at once, the local LLM in the background when
   the rules are unsure, so a slow model never delays an exit.
2. **Bars.** A stock is asked for once a minute while it has an open
   position, a working order or a filing being handed over.
3. **Step.** Bars, then filings, go to the engine with the time they were
   really handed over. The step and what it caused are journaled together.
4. **Close.** After the session the day is closed.

Nothing here talks to an exchange or can send an order to a broker. Fills
are the backtest's pessimistic model applied to live one-minute bars, which
makes paper results comparable with the backtest (same code, same rules)
and says nothing about real slippage: only real orders measure that.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, and_, select

from gats.alerts import Alerter, NoAlerts, daily_summary, tick_messages
from gats.backtest.costs import CostModel
from gats.backtest.engine import Engine, EngineConfig, Execution, Listener, Order
from gats.backtest.feed import Lookups, market_events, with_revenue_ratio
from gats.backtest.runner import design
from gats.config import Settings
from gats.db.schema import (
    announcement_event_types,
    announcement_security,
    announcements,
    document_texts,
    extractions,
    paper_runs,
)
from gats.extract import cascade
from gats.extract.llm import prompt_hash
from gats.extract.pdf_text import EXTRACTOR, EXTRACTOR_VERSION
from gats.ingest import Services
from gats.logging_setup import kv
from gats.marketdata.upstox import TokenMissing, current_isins
from gats.marketdata.windows import event_windows
from gats.pit import AsOf
from gats.recorder import Heartbeat, sleep_or_stop
from gats.refdata.calendar import TradingCalendar
from gats.research.taxonomy import Taxonomy
from gats.risk.engine import RiskEngine, RiskLimits
from gats.runtime.journal import (
    DayClose,
    DesignChanged,
    Diverged,
    Item,
    PaperRun,
    Reference,
    Tape,
)
from gats.runtime.quotes import MINUTE, fetch_intraday, fetch_session
from gats.sources.upstox import Bar
from gats.strategy.base import BarEvent, MarketEvent, Strategy
from gats.strategy.catalog import load_strategy
from gats.timeutil import ist_datetime, to_ist

log = logging.getLogger(__name__)
_OPEN, _CLOSE = time(9, 15), time(15, 30)
_NO_FILE = frozenset({"none", "missing", "too_large"})  # attachment states with nothing to read
_RETRY_S = 10.0  # before asking the broker again after a failed request
_HEARTBEAT_S = 15.0  # a quiet run still says it is alive this often


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EngineSpec(_Strict):
    initial_cash: float = Field(gt=0)
    notional_per_trade: float = Field(gt=0)
    latency_s: float = Field(gt=0)


class PaperSpec(_Strict):
    strategy: Path
    costs: Path
    risk: Path
    source: str = "NSE"
    event_types: list[str] = Field(min_length=1)
    engine: EngineSpec


@dataclass(frozen=True)
class System:
    """The trading system of a paper run, and the hash that identifies it."""

    spec: PaperSpec
    strategy: Strategy[Any]
    costs: CostModel
    limits: RiskLimits
    risk_version: str
    config: EngineConfig
    taxonomy_version: str
    extractor: str
    design_hash: str
    design: dict[str, Any]
    root: Path

    def build(self, tape: Tape, listener: Listener) -> Engine:
        """An engine whose risk rules ask the tape about the outside world."""
        risk = RiskEngine(
            self.limits,
            self.risk_version,
            liquidity=tape.liquidity,
            flags=tape.flags,
            halted=tape.halted,
        )
        return Engine(
            self.strategy,
            self.costs,
            self.config,
            risk=risk,
            listener=listener,
            is_session=tape.session,
        )

    @property
    def kill_switch(self) -> Path | None:
        switch = self.limits.kill_switch_file
        return None if switch is None else self.root / switch


def load_system(path: Path, settings: Settings, root: Path = Path()) -> System:
    """Load the paper config and everything it names. The design covers what
    decides a result: strategy, costs, risk limits, engine settings, and the
    feed (which filings, typed by which taxonomy, read by which extractor)."""
    spec = PaperSpec.model_validate(yaml.safe_load((root / path).read_text(encoding="utf-8")))
    strategy = load_strategy(root / spec.strategy)
    costs = CostModel.load(root / spec.costs)
    risk = RiskEngine.load(root / spec.risk)
    config = EngineConfig(**spec.engine.model_dump())
    taxonomy = Taxonomy.load(settings.taxonomy_path)
    extractor = cascade.extractor_version(
        "cascade", prompt_hash(settings.llm_max_chars), settings.llm_model
    )
    _, params = design(strategy, costs, config, risk.version)
    params["feed"] = {
        "source": spec.source,
        "event_types": sorted(spec.event_types),
        "taxonomy": taxonomy.version,
        "extractor": extractor,
    }
    canonical = json.dumps(params, sort_keys=True, default=str)
    return System(
        spec=spec,
        strategy=strategy,
        costs=costs,
        limits=risk.limits,
        risk_version=risk.version,
        config=config,
        taxonomy_version=taxonomy.version,
        extractor=extractor,
        design_hash=hashlib.sha256(canonical.encode()).hexdigest()[:16],
        design=json.loads(canonical),
        root=root,
    )


class LiveWorld:
    """The outside world at one moment: liquidity and surveillance through
    the point-in-time reader, the kill switch from the file system."""

    def __init__(
        self,
        clock: AsOf,
        securities: Mapping[str, int],
        kill_switch: Path | None,
        calendar: TradingCalendar,
    ) -> None:
        self._lookups = Lookups(clock, securities)
        self._kill_switch = kill_switch
        self._calendar = calendar

    def liquidity(self, instrument_key: str, day: date) -> float | None:
        return self._lookups.liquidity(instrument_key, day)

    def flags(self, instrument_key: str, day: date) -> frozenset[str] | None:
        return self._lookups.flags(instrument_key, day)

    def halted(self) -> bool:
        return self._kill_switch is not None and self._kill_switch.exists()

    def session(self, day: date) -> bool:
        return self._calendar.is_regular_session(day)


@dataclass
class TickReport:
    """What one tick did (for the log, the heartbeat and alerts)."""

    at: datetime
    bars: int = 0
    events: list[MarketEvent] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    fills: list[Execution] = field(default_factory=list)
    waiting: int = 0  # filings seen but not ready to hand over
    closed: date | None = None
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class _Filing:
    """A candidate filing and how far the pipeline has taken it."""

    id: int
    available_at: datetime
    linked: bool
    has_file: bool  # an attachment exists or may still arrive
    text: bool  # its text has been read
    text_error: bool
    extracted: bool


class PaperRuntime:
    def __init__(self, svc: Services, system: System, name: str) -> None:
        self.svc = svc
        self.system = system
        self.name = name
        self.run, self.created_at = self._open()
        self._due: dict[str, datetime] = {}  # watched stock -> when to ask for it next
        self._dropped: set[int] = set()  # filings this run will not take
        self._facts: asyncio.Task[cascade.CascadeStats] | None = None
        self._facts_after: datetime | None = None  # no new extraction before this
        self._calendar: tuple[date, TradingCalendar] | None = None
        self._watch(self.svc.clock())

    # --- the run ------------------------------------------------------------------

    def _open(self) -> tuple[PaperRun, datetime]:
        system = self.system
        with self.svc.engine.begin() as conn:
            run = PaperRun.open(
                conn,
                name=self.name,
                strategy=system.strategy.version,
                design_hash=system.design_hash,
                design=system.design,
                build=system.build,
                now=self.svc.clock(),
                root=system.root,
            )
            created: datetime = conn.execute(
                select(paper_runs.c.created_at).where(paper_runs.c.id == run.id)
            ).scalar_one()
        return run, created

    def reopen(self) -> None:
        """Rebuild the engine from the journal (after a tick failed part-way)."""
        self.run, self.created_at = self._open()
        self._watch(self.svc.clock())

    def _now(self) -> datetime:
        """The clock, never behind the last step (a wall clock can jump)."""
        now = self.svc.clock()
        last = self.run.last_at
        return now if last is None or now >= last else last

    def _watch(self, now: datetime, waiting: Collection[str] = ()) -> None:
        """Watch exactly the stocks with a position or a working order, and
        remember when to ask again for those a waiting filing needs."""
        engine = self.run.engine
        wanted = set(engine.state.positions) | {
            o.signal.instrument_key for o in engine.orders if o.status == "working"
        }
        retry = {key: self._due[key] for key in waiting if key in self._due}
        self._due = retry | {key: self._due.get(key, now) for key in wanted}

    def _calendar_for(self, conn: Connection, today: date) -> TradingCalendar:
        if self._calendar is None or self._calendar[0] != today:
            self._calendar = (today, AsOf(conn, self.svc.clock()).calendar())
        return self._calendar[1]

    # --- one tick -----------------------------------------------------------------

    async def tick(self) -> TickReport:
        svc, run, settings = self.svc, self.run, self.svc.settings
        now = self._now()
        today = to_ist(now).date()
        report = TickReport(now)
        with svc.engine.begin() as conn:
            calendar = self._calendar_for(conn, today)
        ready, report.waiting = await self._ready(now, calendar)
        in_session = calendar.is_regular_session(today)
        grace = timedelta(seconds=settings.paper_close_grace_s)
        polling = in_session and (
            ist_datetime(today, _OPEN) <= now <= ist_datetime(today, _CLOSE) + grace
        )
        # A filing needs a price for its stock: today's bars if the stock is
        # already watched, else asked for now (and again when due, not every
        # tick, if the broker does not answer).
        wanted = {e.instrument_key for e in ready}
        priced = {
            key
            for key in wanted
            if (seen := run.last_bar.get(key)) is not None and to_ist(seen).date() == today
        }
        asked = {key for key in wanted - priced if self._due.get(key, now) <= now}
        if polling:
            asked |= {key for key, due in self._due.items() if due <= now}

        bars: list[BarEvent] = []
        references: list[Reference] = []
        for key in sorted(asked):
            candles = await fetch_intraday(svc, key)
            if candles.error:
                report.errors.append(f"{key}: {candles.error}")
                self._due[key] = candles.asked_at + timedelta(seconds=_RETRY_S)
                continue
            self._due[key] = _next_minute(candles.asked_at, settings.paper_bar_margin_s)
            finished = candles.finished(settings.paper_bar_margin_s)
            last = run.last_bar.get(key)
            fresh = [
                b
                for b in finished
                if to_ist(b.ts).date() == today and (last is None or b.ts > last)
            ]
            bars += [BarEvent(key, b.ts, b.open, b.high, b.low, b.close, b.volume) for b in fresh]
            if fresh:
                priced.add(key)
            elif key in wanted and key not in priced:
                reference = await self._reference(key, finished, calendar, now, report)
                if reference is not None:
                    priced.add(key)
                    if not run.seen(reference):
                        references.append(reference)

        waited = timedelta(seconds=settings.paper_event_wait_s)
        events = [e for e in ready if e.instrument_key in priced or now - e.available_at > waited]
        report.waiting += len(ready) - len(events)
        closing = self._closing(now, today, in_session, bars)
        if not (bars or references or events or closing):
            return report

        at = self._now()
        securities = {**run.securities, **{e.instrument_key: e.security_id for e in events}}
        items: list[Item] = [
            *sorted(bars, key=lambda b: (b.start, b.instrument_key)),
            *references,
            *events,
            *([DayClose(today)] if closing else []),
        ]
        try:
            with svc.engine.begin() as conn:
                world = LiveWorld(AsOf(conn, at), securities, self.system.kill_switch, calendar)
                for item in items:
                    applied = run.apply(conn, item, at, world)
                    report.orders += applied.orders
                    report.fills += applied.executions
        except BaseException:
            self.reopen()  # the engine is ahead of the database: rebuild it
            raise
        report.bars, report.events = len(bars), events
        report.closed = today if closing else None
        self._watch(now, wanted - {e.instrument_key for e in events})
        return report

    def _closing(self, now: datetime, today: date, in_session: bool, bars: list[BarEvent]) -> bool:
        """Close today once the session is over and its last bars are in."""
        grace = timedelta(seconds=self.svc.settings.paper_close_grace_s)
        if not in_session or now < ist_datetime(today, _CLOSE) + grace:
            return False
        engine_day = self.run.engine.state.day
        touched = engine_day == today or any(to_ist(b.start).date() == today for b in bars)
        return touched and not self.run.seen(DayClose(today))

    async def _reference(
        self,
        key: str,
        finished: list[Bar],
        calendar: TradingCalendar,
        now: datetime,
        report: TickReport,
    ) -> Reference | None:
        """The close of the last finished session, for a stock with no bar
        today: from the reply in hand if it reaches back, else asked for."""
        today = to_ist(now).date()
        older = [b for b in finished if to_ist(b.ts).date() < today]
        if not older:
            day = (
                today
                if calendar.is_regular_session(today) and now >= ist_datetime(today, _CLOSE)
                else _previous_session(calendar, today)
            )
            if day is None:
                report.errors.append(f"{key}: no earlier session in the calendar")
                return None
            candles = await fetch_session(self.svc, key, day)
            if candles.error:
                report.errors.append(f"{key}: {candles.error}")
                return None
            older = candles.finished(self.svc.settings.paper_bar_margin_s)
        if not older:
            report.errors.append(f"{key}: no candles to take a reference price from")
            return None
        return Reference(key, older[-1].close, older[-1].ts + MINUTE)

    # --- filings ------------------------------------------------------------------

    async def _ready(
        self, now: datetime, calendar: TradingCalendar
    ) -> tuple[list[MarketEvent], int]:
        """(events ready to hand over, filings still waiting). The loop is
        given a turn first, so a finished extraction lands; and one more if
        an extraction starts here, because the rules answer within it and
        only the LLM takes longer."""
        await asyncio.sleep(0)
        before = self._facts
        with self.svc.engine.begin() as conn:
            ready, waiting = self._filings(conn, now, calendar)
        if self._facts is not None and self._facts is not before:
            await asyncio.sleep(0)
            with self.svc.engine.begin() as conn:
                ready, waiting = self._filings(conn, now, calendar)
        return ready, waiting

    def _filings(
        self, conn: Connection, now: datetime, calendar: TradingCalendar
    ) -> tuple[list[MarketEvent], int]:
        """(events ready to hand over, filings still waiting)."""
        settings, system = self.svc.settings, self.system
        since = max(self.created_at, now - timedelta(days=settings.paper_event_lookback_days))
        candidates = [
            f
            for f in self._candidates(conn, since, now)
            if f.id not in self.run.events and f.id not in self._dropped
        ]
        if not candidates:
            self._extract([], now)  # still collect a finished extraction
            return [], 0
        waited = timedelta(seconds=settings.paper_event_wait_s)
        ready: list[_Filing] = []
        to_extract: list[int] = []
        waiting = 0
        for f in candidates:
            late = now - f.available_at > waited
            if not f.linked:
                if late:
                    self._drop(f.id, "not linked to a security")
                else:
                    waiting += 1
            elif f.extracted or late or not f.has_file or f.text_error:
                ready.append(f)
            else:
                waiting += 1
                if f.text:
                    to_extract.append(f.id)
        self._extract(to_extract, now)
        if not ready:
            return [], waiting

        clock = AsOf(conn, now, calendar=calendar)
        windows, _ = event_windows(
            clock,
            event_types=set(system.spec.event_types),
            taxonomy_version=system.taxonomy_version,
            start=to_ist(since).date(),
            end=to_ist(now).date(),
            source=system.spec.source,
            current_isins=current_isins(conn),
        )
        wanted = {f.id for f in ready}
        mine = [w for w in windows if w.announcement_id in wanted]
        for missing in sorted(wanted - {w.announcement_id for w in mine}):
            self._drop(missing, "no tradable instrument (ISIN, calendar or category)")
        due = [w for w in mine if w.available_at <= now]  # minute-precision rows wait a minute
        waiting += len(mine) - len(due)
        extracted = clock.extracted_facts([w.announcement_id for w in due], system.extractor)
        facts = with_revenue_ratio(clock, due, extracted)
        return market_events(due, facts), waiting

    def _candidates(self, conn: Connection, since: datetime, now: datetime) -> list[_Filing]:
        a, et, link = announcements, announcement_event_types, announcement_security
        t, x = document_texts, extractions
        system = self.system
        rows = conn.execute(
            select(
                a.c.id,
                a.c.available_at,
                a.c.attachment_url,
                a.c.attachment_status,
                a.c.attachment_attempts,
                link.c.security_id,
                t.c.doc_id.label("text_doc"),
                t.c.error.label("text_error"),
                x.c.announcement_id.label("extracted"),
                et.c.event_type,
            )
            .select_from(
                a.join(
                    et,
                    and_(
                        et.c.announcement_id == a.c.id,
                        et.c.taxonomy_version == system.taxonomy_version,
                    ),
                )
                .outerjoin(link, link.c.announcement_id == a.c.id)
                .outerjoin(
                    t,
                    and_(
                        t.c.doc_id == a.c.attachment_doc_id,
                        t.c.extractor == EXTRACTOR,
                        t.c.extractor_version == EXTRACTOR_VERSION,
                    ),
                )
                .outerjoin(
                    x,
                    and_(
                        x.c.announcement_id == a.c.id,
                        x.c.extractor_version == system.extractor,
                    ),
                )
            )
            .where(
                a.c.source == system.spec.source,
                et.c.event_type.in_(system.spec.event_types),
                a.c.available_at >= since,
                a.c.available_at <= now,
            )
            .order_by(a.c.available_at, a.c.id)
        ).all()
        gave_up = self.svc.settings.attachments_max_attempts
        out = []
        for row in rows:
            readable = row.event_type in cascade.EVENT_TYPES
            no_file = (
                row.attachment_url is None
                or row.attachment_status in _NO_FILE
                or (row.attachment_status == "failed" and row.attachment_attempts >= gave_up)
            )
            out.append(
                _Filing(
                    id=int(row.id),
                    available_at=row.available_at,
                    linked=row.security_id is not None,
                    has_file=readable and not no_file,
                    text=row.text_doc is not None and row.text_error is None,
                    text_error=row.text_error is not None,
                    extracted=row.extracted is not None,
                )
            )
        return out

    def _drop(self, announcement_id: int, why: str) -> None:
        self._dropped.add(announcement_id)
        log.warning("filing not taken %s", kv(run=self.name, id=announcement_id, why=why))

    def _extract(self, ids: list[int], now: datetime) -> None:
        """Extract facts in the background, one batch at a time: the LLM can
        take many seconds, and exits must not wait for it."""
        task = self._facts
        if task is not None:
            if not task.done():
                return
            self._facts = None
            failed = task.exception() if not task.cancelled() else None
            deferred = 0 if failed or task.cancelled() else task.result().deferred
            if failed is not None or deferred:
                retry = timedelta(seconds=self.svc.settings.paper_facts_retry_s)
                self._facts_after = now + retry
                log.warning(
                    "facts not extracted %s",
                    kv(run=self.name, error=str(failed) if failed else f"{deferred} deferred"),
                )
        if not ids or (self._facts_after is not None and now < self._facts_after):
            return
        # ORDER_WIN is the only type with an extractor (cascade.EVENT_TYPES).
        self._facts = asyncio.create_task(
            cascade.run_extractions(
                self.svc,
                event_type="ORDER_WIN",
                taxonomy_version=self.system.taxonomy_version,
                mode="cascade",
                limit=len(ids),
                ids=ids,
            )
        )

    async def aclose(self) -> None:
        """Stop the background extraction, if one is running."""
        if self._facts is not None and not self._facts.done():
            self._facts.cancel()
            await asyncio.gather(self._facts, return_exceptions=True)
        self._facts = None


def _next_minute(asked_at: datetime, margin_s: float) -> datetime:
    """The next moment after ``asked_at`` at which one more candle can be
    trusted: a minute boundary plus the margin."""
    due = asked_at.replace(second=0, microsecond=0) + timedelta(seconds=margin_s)
    return due if due > asked_at else due + MINUTE


def _previous_session(calendar: TradingCalendar, day: date) -> date | None:
    for back in range(1, 31):
        earlier = day - timedelta(days=back)
        if calendar.is_regular_session(earlier):
            return earlier
    return None


FATAL = (TokenMissing, DesignChanged, Diverged)  # nothing a retry can fix


async def run_paper(
    svc: Services, runtime: PaperRuntime, stop: asyncio.Event, alerts: Alerter | None = None
) -> None:
    """Tick until told to stop. A tick that fails does not end the run: the
    engine has been rebuilt from the journal, and the loop backs off. A
    missing token or a run that no longer matches its record does end it."""
    settings = svc.settings
    alerts = alerts or NoAlerts()
    heartbeat = Heartbeat(settings.paper_heartbeat_path)
    failures = 0
    beat_at: datetime | None = None
    log.info("paper run starting %s", kv(run=runtime.name, design=runtime.system.design_hash))
    try:
        while not stop.is_set():
            started = svc.clock()
            try:
                report = await runtime.tick()
            except FATAL:
                raise
            except Exception as exc:
                failures += 1
                log.exception("paper tick failed %s", kv(run=runtime.name, failures=failures))
                heartbeat.update(
                    "paper",
                    run=runtime.name,
                    last_error_at=started.isoformat(),
                    last_error=f"{type(exc).__name__}: {exc}"[:300],
                    consecutive_failures=failures,
                )
                delay = min(
                    settings.job_error_backoff_max_s, settings.paper_poll_s * 2 ** min(failures, 10)
                )
                await alerts.send(
                    f"[{runtime.name}] ERROR: tick failed: {type(exc).__name__}: {exc}"[:500]
                )
            else:
                recovered, failures = failures > 0, 0
                active = bool(
                    report.events or report.orders or report.fills or report.errors or report.closed
                )
                if active:
                    log.info("paper %s", kv(run=runtime.name, **summary(report)))
                for message in tick_messages(
                    runtime.name, report.orders, report.fills, report.errors
                ):
                    await alerts.send(message)
                if report.closed is not None:
                    result = runtime.run.engine.result()
                    await alerts.send(daily_summary(runtime.name, report.closed, result))
                quiet_s = None if beat_at is None else (started - beat_at).total_seconds()
                if active or recovered or quiet_s is None or quiet_s >= _HEARTBEAT_S:
                    engine = runtime.run.engine
                    heartbeat.update(
                        "paper",
                        run=runtime.name,
                        design=runtime.system.design_hash,
                        last_ok_at=started.isoformat(),
                        consecutive_failures=0,
                        positions=len(engine.state.positions),
                        working=sum(o.status == "working" for o in engine.orders),
                        waiting=report.waiting,
                        last_note=report.errors[-1] if report.errors else None,
                    )
                    beat_at = started
                delay = settings.paper_poll_s
            await sleep_or_stop(stop, delay)
    finally:
        await runtime.aclose()
        log.info("paper run stopped %s", kv(run=runtime.name))


def summary(report: TickReport) -> dict[str, Any]:
    """One tick, as a log line's fields."""
    return {
        "bars": report.bars,
        "filings": [e.event_id for e in report.events],
        "orders": [
            f"{o.signal.side} {o.quantity} {o.signal.instrument_key} {o.status}"
            + (f" ({o.note})" if o.note else "")
            for o in report.orders
        ],
        "fills": [
            f"{e.side} {e.quantity} {e.instrument_key} @ {e.price:.2f}" for e in report.fills
        ],
        "waiting": report.waiting,
        "closed": report.closed.isoformat() if report.closed else None,
        "errors": report.errors,
    }
