"""The 24/7 recorder: a set of independent polling jobs.

Each job runs in its own asyncio task. A failing job backs off exponentially
and never stops the others. Progress is written to a heartbeat file that
``gats status`` reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import and_, func, select

from gats import ingest
from gats.db import repo
from gats.db.repo import IngestMode
from gats.db.schema import announcement_event_types, announcements, financial_results
from gats.extract.texts import ExtractStats, extract_all, extract_pending
from gats.ingest import Outcome, Services
from gats.logging_setup import kv
from gats.refdata import dedupe, master, versions
from gats.refdata.ingest import (
    ingest_bse_scrips,
    ingest_nse_corp_actions,
    ingest_nse_holidays,
    ingest_nse_surveillance,
    ingest_nse_symbol_changes,
)
from gats.refdata.link import link_all, link_pending
from gats.refdata.results import fetch_pending_xbrl, ingest_results_index
from gats.research.taxonomy import Taxonomy, classify_all, classify_pending
from gats.sources import (
    bse_scrips,
    nse_corp_actions,
    nse_holidays,
    nse_surveillance,
    nse_symbols,
)
from gats.timeutil import ist_time_of_day, ist_today

log = logging.getLogger(__name__)


class Job(Protocol):
    """A job may also carry ``wake``, an :class:`asyncio.Event` that ends
    its sleep early (see :class:`HandOffJob`)."""

    name: str

    def interval_s(self, now: datetime) -> float: ...

    async def run_once(self, svc: Services) -> Outcome: ...


def _in_window(now_t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= now_t < end
    return now_t >= start or now_t < end  # window wraps midnight


@dataclass
class _PollingAnnouncementsJob:
    """Shared behaviour: first successful run is a 'catchup' (it sees filings
    published while the recorder was down, so their latency is meaningless);
    later runs are 'live'."""

    name: str
    base_interval_s: float
    night_multiplier: float
    night_start: time
    night_end: time
    notify: asyncio.Event | None = field(default=None, kw_only=True)  # set on new filings
    _caught_up: bool = field(default=False, init=False)

    def interval_s(self, now: datetime) -> float:
        if _in_window(ist_time_of_day(now), self.night_start, self.night_end):
            return self.base_interval_s * self.night_multiplier
        return self.base_interval_s

    def _mode(self) -> IngestMode:
        return "live" if self._caught_up else "catchup"

    def _mark(self, outcome: Outcome) -> Outcome:
        if outcome.ok:
            self._caught_up = True
        if outcome.n_new and self.notify is not None:
            self.notify.set()
        return outcome

    @staticmethod
    def _days(now: datetime) -> list[date]:
        """Today, plus yesterday during the first hour after IST midnight so
        filings published just before midnight are not missed."""
        today = ist_today(now)
        if ist_time_of_day(now) < time(1, 0):
            return [today - timedelta(days=1), today]
        return [today]


@dataclass
class BseAnnouncementsJob(_PollingAnnouncementsJob):
    max_pages: int = 10
    catchup_max_pages: int = 100

    async def run_once(self, svc: Services) -> Outcome:
        # After a restart, today may already have 20+ pages of filings.
        pages = self.max_pages if self._caught_up else self.catchup_max_pages
        total = Outcome(ok=True)
        for day in self._days(svc.clock()):  # BSE serves one day per query
            total.merge(
                await ingest.collect_bse(
                    svc,
                    day,
                    day,
                    job=self.name,
                    mode=self._mode(),
                    max_pages=pages,
                    stop_when_no_new=True,
                )
            )
        return self._mark(total)


@dataclass
class NseAnnouncementsJob(_PollingAnnouncementsJob):
    async def run_once(self, svc: Services) -> Outcome:
        # NSE returns a whole day per response, so poll only today (plus
        # yesterday in the first hour after midnight) to keep payloads small.
        # One day per request, like BSE: only single-day queries are verified.
        total = Outcome(ok=True)
        for day in self._days(svc.clock()):
            total.merge(await ingest.collect_nse(svc, day, day, job=self.name, mode=self._mode()))
        return self._mark(total)


@dataclass
class EodJob:
    """A once-per-session NSE file (the EOD bhavcopy by default; pass
    ``spec=ingest.INDEX_FILE`` for index closes)."""

    name: str
    check_s: float
    catchup_days: int
    publish_after: time
    max_missing_attempts: int
    spec: ingest.DailyFile = ingest.EOD_FILE

    def interval_s(self, now: datetime) -> float:
        return self.check_s

    def due_days(self, svc: Services) -> list[date]:
        """Days in the catch-up window still worth asking about. Weekends are
        included: special sessions (e.g. the Sunday budget-day session of
        2026-02-01) publish a file too."""
        now = svc.clock()
        today = ist_today(now)
        days = []
        with svc.engine.begin() as conn:
            for back in range(self.catchup_days - 1, -1, -1):
                day = today - timedelta(days=back)
                if day == today and ist_time_of_day(now) < self.publish_after:
                    continue
                if repo.has_rows_for_date(conn, self.spec.rows_table, "trade_date", day):
                    continue
                if repo.eod_day_settled(
                    repo.eod_day(conn, day, self.spec.days_table),
                    today=today,
                    max_attempts=self.max_missing_attempts,
                ):
                    continue  # holiday or weekend: stop asking
                days.append(day)
        return days

    async def run_once(self, svc: Services) -> Outcome:
        total = Outcome(ok=True)
        for day in self.due_days(svc):
            total.merge(
                await ingest.ingest_daily_file(svc, self.spec, day, job=self.name, mode="live")
            )
        return total


@dataclass
class SnapshotJob:
    name: str
    what: str  # bands | instruments
    check_s: float
    after: time

    def interval_s(self, now: datetime) -> float:
        return self.check_s

    async def run_once(self, svc: Services) -> Outcome:
        now = svc.clock()
        if ist_time_of_day(now) < self.after:
            return Outcome(ok=True, meta={"skipped": "before daily window"})
        with svc.engine.begin() as conn:
            if repo.has_rows_for_date(
                conn, ingest.snapshot_table_for(self.what), "as_of_date", ist_today(now)
            ):
                return Outcome(ok=True, meta={"skipped": "already have today"})
        return await ingest.ingest_snapshot(svc, self.what, job=self.name)


@dataclass
class RefdataJob:
    """A current-only reference file, applied once a day as a versioned
    snapshot (``gats.refdata.versions``)."""

    name: str
    kind: str
    check_s: float
    after: time
    fetch: Callable[[Services], Awaitable[Outcome]]

    def interval_s(self, now: datetime) -> float:
        return self.check_s

    async def run_once(self, svc: Services) -> Outcome:
        now = svc.clock()
        if ist_time_of_day(now) < self.after:
            return Outcome(ok=True, meta={"skipped": "before daily window"})
        with svc.engine.begin() as conn:
            if versions.has_snapshot(conn, self.kind, ist_today(now)):
                return Outcome(ok=True, meta={"skipped": "already have today"})
        return await self.fetch(svc)


@dataclass
class MasterBuildJob:
    """Rebuild the security master once a day, after the reference files."""

    name: str
    check_s: float
    after: time

    def interval_s(self, now: datetime) -> float:
        return self.check_s

    async def run_once(self, svc: Services) -> Outcome:
        now = svc.clock()
        if ist_time_of_day(now) < self.after:
            return Outcome(ok=True, meta={"skipped": "before daily window"})
        with svc.engine.begin() as conn:
            built_at = master.latest_build_time(conn)
            if built_at is not None and ist_today(built_at) == ist_today(now):
                return Outcome(ok=True, meta={"skipped": "already built today"})
            stats = master.build(conn, now)
        return Outcome(
            ok=True, n_records=stats.securities, n_new=stats.new_securities, meta=stats.as_dict()
        )


@dataclass
class LinkJob:
    """Link new filings to securities; retry unresolved ones after each build."""

    name: str
    poll_s: float
    _resolver: master.Resolver | None = field(default=None, init=False)
    _build_id: int | None = field(default=None, init=False)

    def interval_s(self, now: datetime) -> float:
        return self.poll_s

    async def run_once(self, svc: Services) -> Outcome:
        with svc.engine.begin() as conn:
            build_id = master.latest_build_id(conn)
            if build_id is None:
                return Outcome(ok=True, meta={"skipped": "no security master yet"})
            if build_id != self._build_id or self._resolver is None:
                self._resolver = master.Resolver.load(conn, build_id)
                self._build_id = build_id
        stats = link_all(svc.engine, svc.clock(), self._resolver)
        return Outcome(
            ok=True,
            n_records=stats.considered,
            n_new=stats.linked,
            meta={"unresolved": stats.unresolved},
        )


@dataclass
class DedupeJob:
    """Pair recent BSE/NSE filings of the same disclosure into one event."""

    name: str
    poll_s: float
    lookback_days: int

    def interval_s(self, now: datetime) -> float:
        return self.poll_s

    async def run_once(self, svc: Services) -> Outcome:
        now = svc.clock()
        with svc.engine.begin() as conn:
            stats = dedupe.group_range(
                conn, now - timedelta(days=self.lookback_days), now + timedelta(minutes=1), now
            )
        return Outcome(ok=True, n_records=stats.filings, meta={"pairs": stats.pairs})


@dataclass
class ClassifyJob:
    """Type new filings with the event taxonomy (reloaded when the file changes)."""

    name: str
    poll_s: float
    path: Path
    _taxonomy: Taxonomy | None = field(default=None, init=False)
    _mtime: float | None = field(default=None, init=False)

    def interval_s(self, now: datetime) -> float:
        return self.poll_s

    async def run_once(self, svc: Services) -> Outcome:
        if not self.path.exists():
            return Outcome(ok=False, error=f"taxonomy file not found: {self.path.resolve()}")
        mtime = self.path.stat().st_mtime
        if self._taxonomy is None or mtime != self._mtime:
            self._taxonomy, self._mtime = Taxonomy.load(self.path), mtime
        stats = classify_all(svc.engine, self._taxonomy, svc.clock())
        return Outcome(ok=True, n_records=stats.classified, n_new=stats.classified)


@dataclass
class ExtractJob:
    """Extract text from newly downloaded attachments."""

    name: str
    poll_s: float

    def interval_s(self, now: datetime) -> float:
        return self.poll_s

    async def run_once(self, svc: Services) -> Outcome:
        stats = extract_all(svc.engine, svc.store, svc.clock(), limit=500)
        return Outcome(ok=True, n_records=stats.documents, n_new=stats.with_text)


@dataclass
class AttachmentsJob:
    name: str
    poll_s: float

    def interval_s(self, now: datetime) -> float:
        return self.poll_s

    async def run_once(self, svc: Services) -> Outcome:
        return await ingest.fetch_pending_attachments(svc, job=self.name)


@dataclass
class HandOffJob:
    """The fast lane from a new filing to something a strategy can act on.

    The batch jobs (classify, link, attachments, extract) each wake on
    their own timer. That suits research and costs minutes, which an
    intraday strategy does not have. This job is woken the moment an
    announcements job stores new filings, and takes just those filings
    through the same steps at once: type them, link them, and for the event
    types a strategy trades, fetch the attachment ahead of the queue and
    read its text.

    It calls only what the batch jobs call, on rows they would reach
    anyway, so whichever gets to a filing first the result is the same. The
    recorder stays the only process that talks to the exchanges; a runtime
    reads the outcome from the database.
    """

    name: str
    fallback_s: float
    path: Path
    event_types: frozenset[str]
    sources: frozenset[str]
    max_age_s: float
    max_downloads: int
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    _taxonomy: Taxonomy | None = field(default=None, init=False)
    _mtime: float | None = field(default=None, init=False)
    _resolver: master.Resolver | None = field(default=None, init=False)
    _build_id: int | None = field(default=None, init=False)
    _cursor: int = field(default=-1, init=False)  # highest filing id taken so far
    _retry: set[int] = field(default_factory=set, init=False)  # attachments still to get

    def interval_s(self, now: datetime) -> float:
        return self.fallback_s

    async def run_once(self, svc: Services) -> Outcome:
        self.wake.clear()  # filings stored from here on wake the job again
        if not self.path.exists():
            return Outcome(ok=False, error=f"taxonomy file not found: {self.path.resolve()}")
        mtime = self.path.stat().st_mtime
        if self._taxonomy is None or mtime != self._mtime:
            self._taxonomy, self._mtime = Taxonomy.load(self.path), mtime
        taxonomy = self._taxonomy
        now = svc.clock()
        cutoff = now - timedelta(seconds=self.max_age_s)
        a, et = announcements, announcement_event_types
        with svc.engine.begin() as conn:
            fresh = [
                int(i)
                for i in conn.execute(
                    select(a.c.id)
                    .where(
                        a.c.id > self._cursor,
                        a.c.available_at >= cutoff,
                        a.c.ingest_mode != "backfill",
                    )
                    .order_by(a.c.id)
                ).scalars()
            ]
            for chunk in _chunked(fresh):
                classify_pending(conn, taxonomy, now, ids=chunk)
            build_id = master.latest_build_id(conn)
            if build_id is not None:
                if build_id != self._build_id or self._resolver is None:
                    self._resolver = master.Resolver.load(conn, build_id)
                    self._build_id = build_id
                for chunk in _chunked(fresh):
                    link_pending(conn, now, self._resolver, ids=chunk)
            wanted = [
                int(i)
                for chunk in _chunked(sorted(self._retry | set(fresh)))
                for i in conn.execute(
                    select(a.c.id)
                    .select_from(
                        a.join(
                            et,
                            and_(
                                et.c.announcement_id == a.c.id,
                                et.c.taxonomy_version == taxonomy.version,
                            ),
                        )
                    )
                    .where(
                        a.c.id.in_(chunk),
                        et.c.event_type.in_(self.event_types),
                        a.c.source.in_(self.sources),
                        a.c.available_at >= cutoff,
                    )
                ).scalars()
            ]
        if fresh:
            self._cursor = fresh[-1]
        fetched = await ingest.fetch_attachments_for(
            svc, wanted, job=self.name, limit=self.max_downloads
        )
        texts = ExtractStats()
        with svc.engine.begin() as conn:
            rows = conn.execute(
                select(
                    a.c.id, a.c.attachment_status, a.c.attachment_attempts, a.c.attachment_doc_id
                ).where(a.c.id.in_(wanted))
            ).all()
            docs = [str(row.attachment_doc_id) for row in rows if row.attachment_doc_id]
            if docs:
                texts = extract_pending(conn, svc.store, svc.clock(), doc_ids=docs)
        self._retry = {
            int(row.id)
            for row in rows
            if row.attachment_status in ("pending", "failed", "skipped")
            and row.attachment_attempts < svc.settings.attachments_max_attempts
        }
        return Outcome(
            ok=True,
            n_records=len(fresh),
            n_new=fetched.n_new,
            meta={
                "in_scope": len(wanted),
                "attachments": fetched.n_new,
                "texts": texts.with_text,
                "retrying": len(self._retry),
            },
        )


def _chunked(ids: Sequence[int], size: int = 500) -> list[Sequence[int]]:
    return [ids[i : i + size] for i in range(0, len(ids), size)]


@dataclass
class ResultsJob:
    """Keep quarterly results current for the companies a strategy may trade.

    ``gats backfill results`` loads history once. Without this job the
    figures then age: an order win in December would be sized against
    revenue that stops in June, and a later backtest, run on a complete
    database, would see a different ratio than paper trading did.

    Two triggers, both read from filings the recorder already has:

    - a company in the universe (it has had an in-scope filing before) files
      its results: ask NSE's results index about it, then read the XBRL;
    - a company has a fresh in-scope filing and no results stored at all:
      ask too, so that its next filing can be sized.

    A company is asked at most once per ``retry_s``: the results index can
    lag the announcement by hours.
    """

    name: str
    check_s: float
    path: Path
    results_type: str
    in_scope: frozenset[str]
    lookback_days: int
    max_symbols: int
    max_xbrl: int
    retry_s: float
    xbrl_since_days: int
    _taxonomy: Taxonomy | None = field(default=None, init=False)
    _mtime: float | None = field(default=None, init=False)
    _asked: dict[str, datetime] = field(default_factory=dict, init=False)

    def interval_s(self, now: datetime) -> float:
        return self.check_s

    def due(self, svc: Services, version: str) -> list[str]:
        """Symbols to ask about now, most recently filed first."""
        now = svc.clock()
        since = now - timedelta(days=self.lookback_days)
        a, et, f = announcements, announcement_event_types, financial_results
        typed = a.join(et, and_(et.c.announcement_id == a.c.id, et.c.taxonomy_version == version))
        nse = and_(a.c.source == "NSE", a.c.symbol.is_not(None))
        with svc.engine.begin() as conn:
            universe = set(
                conn.execute(
                    select(a.c.symbol)
                    .select_from(typed)
                    .where(nse, et.c.event_type.in_(self.in_scope))
                    .distinct()
                ).scalars()
            )
            stored = {
                str(symbol): latest
                for symbol, latest in conn.execute(
                    select(f.c.symbol, func.max(f.c.available_at)).group_by(f.c.symbol)
                )
            }
            recent = conn.execute(
                select(a.c.symbol, et.c.event_type, func.max(a.c.available_at))
                .select_from(typed)
                .where(
                    nse,
                    a.c.available_at >= since,
                    et.c.event_type.in_([self.results_type, *self.in_scope]),
                )
                .group_by(a.c.symbol, et.c.event_type)
            ).all()
        wanted: dict[str, datetime] = {}
        for symbol, event_type, filed_at in recent:
            have = stored.get(symbol)
            reported = event_type == self.results_type and symbol in universe
            behind = reported and (have is None or have < filed_at)  # newer than what is stored
            unknown = event_type in self.in_scope and have is None
            if behind or unknown:
                wanted[symbol] = max(filed_at, wanted.get(symbol, filed_at))
        retry = timedelta(seconds=self.retry_s)
        waiting = [s for s in wanted if s not in self._asked or now - self._asked[s] >= retry]
        return sorted(waiting, key=lambda s: wanted[s], reverse=True)[: self.max_symbols]

    async def run_once(self, svc: Services) -> Outcome:
        if not self.path.exists():
            return Outcome(ok=False, error=f"taxonomy file not found: {self.path.resolve()}")
        mtime = self.path.stat().st_mtime
        if self._taxonomy is None or mtime != self._mtime:
            self._taxonomy, self._mtime = Taxonomy.load(self.path), mtime
        symbols = self.due(svc, self._taxonomy.version)
        total = Outcome(ok=True, meta={"asked": symbols})
        for symbol in symbols:
            self._asked[symbol] = svc.clock()
            outcome = await ingest_results_index(svc, symbol, job=self.name)
            total.n_records += outcome.n_records
            total.n_new += outcome.n_new
            if not outcome.ok:
                total.warnings.append(f"{symbol}: {outcome.error}")
        if symbols:
            since = ist_today(svc.clock()) - timedelta(days=self.xbrl_since_days)
            stats = await fetch_pending_xbrl(svc, since=since, limit=self.max_xbrl, job=self.name)
            total.meta["xbrl"] = {"read": stats.attempted, "done": stats.done}
        return total


@dataclass
class ReconcileJob:
    """Re-collect each of the last ``days`` days in full until it is complete.

    Heals every kind of gap without manual work: the PC being off, BSE
    throttling mid-day, or more new filings between two polls than one page
    holds. Rows it adds are labelled ``backfill`` (they were not seen live).
    Its outcome is always ok: per-day failures live in ``backfill_days`` and
    must not shorten the hourly interval through the error backoff.
    """

    name: str
    check_s: float
    days: int
    sources: tuple[str, ...]
    max_pages: int

    def interval_s(self, now: datetime) -> float:
        return self.check_s

    def due(self, svc: Services) -> list[tuple[str, date]]:
        today = ist_today(svc.clock())
        due: list[tuple[str, date]] = []
        with svc.engine.begin() as conn:
            for back in range(self.days, 0, -1):
                day = today - timedelta(days=back)
                for source in self.sources:
                    if repo.backfill_day_status(conn, source, day) not in ("complete", "gave_up"):
                        due.append((source, day))
        return due

    async def run_once(self, svc: Services) -> Outcome:
        total = Outcome(ok=True)
        for source, day in self.due(svc):
            outcome = await ingest.backfill_day(
                svc, source, day, job=self.name, max_pages=self.max_pages
            )
            total.n_records += outcome.n_records
            total.n_new += outcome.n_new
            if outcome.meta.get("backfill_status") != "complete":
                total.warnings.append(f"{source} {day}: {outcome.error or 'incomplete'}")
                log.warning(
                    "reconcile incomplete %s",
                    kv(source=source, day=str(day), error=outcome.error or "incomplete"),
                )
        return total


def build_jobs(svc: Services) -> list[Job]:
    s = svc.settings
    jobs: list[Job] = []
    # The hand-off needs typed, linked filings and their attachments.
    hand_off = (
        HandOffJob(
            "hand_off",
            s.handoff_fallback_s,
            s.taxonomy_path,
            frozenset(s.handoff_event_types),
            frozenset(s.handoff_sources),
            s.handoff_max_age_s,
            s.handoff_max_downloads,
        )
        if s.handoff_enabled and s.refdata_enabled and s.attachments_enabled
        else None
    )
    new_filings = hand_off.wake if hand_off else None
    if s.bse_enabled:
        jobs.append(
            BseAnnouncementsJob(
                "bse_announcements",
                s.bse_poll_s,
                s.night_poll_multiplier,
                s.night_start_ist,
                s.night_end_ist,
                max_pages=s.bse_max_pages,
                catchup_max_pages=s.bse_catchup_max_pages,
                notify=new_filings,
            )
        )
    if s.nse_enabled:
        jobs.append(
            NseAnnouncementsJob(
                "nse_announcements",
                s.nse_poll_s,
                s.night_poll_multiplier,
                s.night_start_ist,
                s.night_end_ist,
                notify=new_filings,
            )
        )
    if hand_off is not None:
        jobs.append(hand_off)
    if s.eod_enabled:
        jobs.append(
            EodJob(
                "nse_eod",
                s.eod_check_s,
                s.eod_catchup_days,
                s.eod_publish_after_ist,
                s.eod_max_missing_attempts,
            )
        )
    if s.indices_enabled:
        jobs.append(
            EodJob(
                "nse_indices",
                s.eod_check_s,
                s.eod_catchup_days,
                s.eod_publish_after_ist,
                s.eod_max_missing_attempts,
                spec=ingest.INDEX_FILE,
            )
        )
    if s.snapshots_enabled:
        jobs.append(
            SnapshotJob("nse_bands", "bands", s.snapshot_check_s, s.daily_snapshot_after_ist)
        )
        jobs.append(
            SnapshotJob(
                "nse_instruments", "instruments", s.snapshot_check_s, s.daily_snapshot_after_ist
            )
        )
    if s.refdata_enabled:

        async def fetch_bse_scrips(svc: Services) -> Outcome:
            return await ingest_bse_scrips(svc, job="bse_scrips")

        async def fetch_symbol_changes(svc: Services) -> Outcome:
            return await ingest_nse_symbol_changes(svc, job="nse_symbol_changes")

        async def fetch_holidays(svc: Services) -> Outcome:
            return await ingest_nse_holidays(svc, job="nse_holidays")

        async def fetch_surveillance(svc: Services) -> Outcome:
            total = Outcome(ok=True)
            for outcome in await ingest_nse_surveillance(svc, job="nse_surveillance"):
                total.merge(outcome)
            return total

        async def fetch_corp_actions(svc: Services) -> Outcome:
            today = ist_today(svc.clock())
            return await ingest_nse_corp_actions(
                svc,
                today - timedelta(days=s.corp_actions_lookback_days),
                today + timedelta(days=s.corp_actions_lookahead_days),
                job="nse_corp_actions",
            )

        for name, kind, fetch in (
            ("bse_scrips", bse_scrips.KIND, fetch_bse_scrips),
            ("nse_symbol_changes", nse_symbols.KIND, fetch_symbol_changes),
            ("nse_holidays", nse_holidays.KIND, fetch_holidays),
            # Done for the day once the GSM list (fetched second) is in.
            ("nse_surveillance", nse_surveillance.GSM_KIND, fetch_surveillance),
            *(
                [("nse_corp_actions", nse_corp_actions.KIND, fetch_corp_actions)]
                if s.corp_actions_enabled
                else []
            ),
        ):
            jobs.append(
                RefdataJob(name, kind, s.snapshot_check_s, s.daily_snapshot_after_ist, fetch)
            )
        jobs.append(MasterBuildJob("master_build", s.snapshot_check_s, s.master_build_after_ist))
        jobs.append(LinkJob("link", s.link_poll_s))
        jobs.append(DedupeJob("dedupe", s.dedupe_poll_s, s.dedupe_lookback_days))
        jobs.append(ClassifyJob("classify", s.classify_poll_s, s.taxonomy_path))
    if s.attachments_enabled:
        jobs.append(AttachmentsJob("attachments", s.attachments_poll_s))
        jobs.append(ExtractJob("extract", s.extract_poll_s))
    if s.results_enabled and s.refdata_enabled and s.nse_enabled:
        jobs.append(
            ResultsJob(
                "results",
                s.results_check_s,
                s.taxonomy_path,
                s.results_event_type,
                frozenset(s.handoff_event_types),
                s.results_lookback_days,
                s.results_max_symbols,
                s.results_max_xbrl,
                s.results_retry_s,
                s.results_xbrl_since_days,
            )
        )
    sources = tuple(
        src for src, enabled in (("BSE", s.bse_enabled), ("NSE", s.nse_enabled)) if enabled
    )
    if s.reconcile_enabled and sources:
        jobs.append(
            ReconcileJob(
                "reconcile", s.reconcile_check_s, s.reconcile_days, sources, s.backfill_max_pages
            )
        )
    return jobs


class Heartbeat:
    """Per-job status written atomically to a JSON file."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._state: dict[str, dict[str, Any]] = {}

    def update(self, job: str, **fields: Any) -> None:
        self._state.setdefault(job, {}).update(fields)
        try:
            self._write()
        except OSError as exc:
            # On Windows the replace fails if a reader holds the file open.
            # A missed heartbeat must never stop a job; the next update retries.
            log.warning("heartbeat write failed %s", kv(error=str(exc)))

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"pid": os.getpid(), "jobs": self._state}, handle, indent=2, default=str)
            os.replace(tmp, self._path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


async def sleep_or_stop(
    stop: asyncio.Event, seconds: float, wake: asyncio.Event | None = None
) -> None:
    """Sleep ``seconds``; less if the recorder stops or the job is woken."""
    if wake is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        return
    waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(wake.wait())]
    try:
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiting in waits:
            waiting.cancel()


async def run_job_loop(
    job: Job, svc: Services, stop: asyncio.Event, heartbeat: Heartbeat, max_backoff_s: float
) -> None:
    failures = 0
    while not stop.is_set():
        started = svc.clock()
        try:
            outcome = await job.run_once(svc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a job must never take the recorder down
            log.exception("job crashed %s", kv(job=job.name))
            outcome = Outcome(ok=False, error=f"{type(exc).__name__}: {exc}")

        if outcome.ok:
            failures = 0
            heartbeat.update(
                job.name,
                last_ok_at=started.isoformat(),
                consecutive_failures=0,
                last_new=outcome.n_new,
                last_records=outcome.n_records,
            )
            if outcome.n_new:
                log.info(
                    "new data %s", kv(job=job.name, new=outcome.n_new, records=outcome.n_records)
                )
            delay = job.interval_s(svc.clock())
        else:
            failures += 1
            heartbeat.update(
                job.name,
                last_error_at=started.isoformat(),
                last_error=outcome.error,
                consecutive_failures=failures,
            )
            log.warning("job failed %s", kv(job=job.name, failures=failures, error=outcome.error))
            delay = min(max_backoff_s, job.interval_s(svc.clock()) * (2 ** min(failures, 10)))
        await sleep_or_stop(stop, delay, getattr(job, "wake", None))


async def run_recorder(svc: Services, stop: asyncio.Event, jobs: list[Job] | None = None) -> None:
    jobs = jobs if jobs is not None else build_jobs(svc)
    if not jobs:
        raise RuntimeError("no jobs enabled; check GATS_*_ENABLED settings")
    heartbeat = Heartbeat(svc.settings.heartbeat_path)
    log.info("recorder starting %s", kv(jobs=",".join(j.name for j in jobs)))
    tasks = [
        asyncio.create_task(
            run_job_loop(job, svc, stop, heartbeat, svc.settings.job_error_backoff_max_s),
            name=job.name,
        )
        for job in jobs
    ]
    try:
        await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("recorder stopped")
