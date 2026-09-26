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
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from gats import ingest
from gats.db import repo
from gats.db.repo import IngestMode
from gats.ingest import Outcome, Services
from gats.logging_setup import kv
from gats.sources import nse_archives
from gats.timeutil import is_weekday, ist_time_of_day, ist_today

log = logging.getLogger(__name__)


class Job(Protocol):
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
        return outcome

    @staticmethod
    def _window(now: datetime) -> tuple[date, date]:
        # Always include yesterday so nothing is lost across IST midnight.
        today = ist_today(now)
        return today - timedelta(days=1), today


@dataclass
class BseAnnouncementsJob(_PollingAnnouncementsJob):
    max_pages: int = 10

    async def run_once(self, svc: Services) -> Outcome:
        start, end = self._window(svc.clock())
        outcome = await ingest.collect_bse(
            svc,
            start,
            end,
            job=self.name,
            mode=self._mode(),
            max_pages=self.max_pages,
            stop_when_no_new=True,
        )
        return self._mark(outcome)


@dataclass
class NseAnnouncementsJob(_PollingAnnouncementsJob):
    async def run_once(self, svc: Services) -> Outcome:
        today = ist_today(svc.clock())
        # NSE returns the whole range in one response, so poll only today to
        # keep payloads small; the midnight overlap is covered by the first
        # poll after midnight including yesterday.
        start = today - timedelta(days=1) if ist_time_of_day(svc.clock()) < time(1, 0) else today
        outcome = await ingest.collect_nse(svc, start, today, job=self.name, mode=self._mode())
        return self._mark(outcome)


@dataclass
class EodJob:
    name: str
    check_s: float
    catchup_days: int
    publish_after: time
    max_missing_attempts: int

    def interval_s(self, now: datetime) -> float:
        return self.check_s

    def due_days(self, svc: Services) -> list[date]:
        now = svc.clock()
        today = ist_today(now)
        days = []
        with svc.engine.begin() as conn:
            for back in range(self.catchup_days - 1, -1, -1):
                day = today - timedelta(days=back)
                if not is_weekday(day):
                    continue
                if day == today and ist_time_of_day(now) < self.publish_after:
                    continue
                if repo.has_rows_for_date(
                    conn, ingest.snapshot_table_for("eod"), "trade_date", day
                ):
                    continue
                url = nse_archives.eod_url(svc.settings.nse_eod_url_template, day)
                if day < today and repo.count_not_found(conn, url) >= self.max_missing_attempts:
                    continue  # holiday: stop asking
                days.append(day)
        return days

    async def run_once(self, svc: Services) -> Outcome:
        total = Outcome(ok=True)
        for day in self.due_days(svc):
            total.merge(await ingest.ingest_eod_day(svc, day, job=self.name, mode="live"))
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
class AttachmentsJob:
    name: str
    poll_s: float

    def interval_s(self, now: datetime) -> float:
        return self.poll_s

    async def run_once(self, svc: Services) -> Outcome:
        return await ingest.fetch_pending_attachments(svc, job=self.name)


def build_jobs(svc: Services) -> list[Job]:
    s = svc.settings
    jobs: list[Job] = []
    if s.bse_enabled:
        jobs.append(
            BseAnnouncementsJob(
                "bse_announcements",
                s.bse_poll_s,
                s.night_poll_multiplier,
                s.night_start_ist,
                s.night_end_ist,
                max_pages=s.bse_max_pages,
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
            )
        )
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
    if s.snapshots_enabled:
        jobs.append(
            SnapshotJob("nse_bands", "bands", s.snapshot_check_s, s.daily_snapshot_after_ist)
        )
        jobs.append(
            SnapshotJob(
                "nse_instruments", "instruments", s.snapshot_check_s, s.daily_snapshot_after_ist
            )
        )
    if s.attachments_enabled:
        jobs.append(AttachmentsJob("attachments", s.attachments_poll_s))
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


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


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
        await _sleep_or_stop(stop, delay)


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
