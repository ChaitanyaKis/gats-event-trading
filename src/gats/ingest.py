"""Fetch → store raw → parse → write. Shared by the recorder, backfill,
probe and reparse commands so every path writes data the same way."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Connection, Engine, Table

from gats.config import Settings
from gats.db import repo
from gats.db.repo import IngestMode
from gats.db.schema import (
    eod_days,
    eod_prices,
    index_days,
    index_eod,
    instrument_snapshots,
    price_bands,
)
from gats.logging_setup import kv
from gats.net import Fetched, FetchError, PoliteClient
from gats.rawstore import RawStore
from gats.sources import (
    bse,
    bse_scrips,
    nse,
    nse_archives,
    nse_corp_actions,
    nse_holidays,
    nse_indices,
    nse_surveillance,
    nse_symbols,
)
from gats.sources.models import AnnouncementRecord, ParseResult, PayloadError
from gats.timeutil import ist_datetime, ist_today, utcnow

log = logging.getLogger(__name__)

# Reference files applied by gats.refdata (kept here so reparse can route them
# without importing gats.refdata at module load: it imports this module).
REFERENCE_KINDS = frozenset(
    {
        bse_scrips.KIND,
        nse_symbols.KIND,
        nse_holidays.KIND,
        nse_corp_actions.KIND,
        nse_surveillance.ASM_KIND,
        nse_surveillance.GSM_KIND,
    }
)


@dataclass
class Services:
    settings: Settings
    engine: Engine
    store: RawStore
    client: PoliteClient
    clock: Callable[[], datetime] = utcnow
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep


@dataclass
class Outcome:
    """Result of one fetch-and-ingest step."""

    ok: bool
    n_records: int = 0
    n_new: int = 0
    http_status: int | None = None
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    new_records: list[AnnouncementRecord] = field(default_factory=list)

    def merge(self, other: Outcome) -> None:
        self.ok = self.ok and other.ok
        self.n_records += other.n_records
        self.n_new += other.n_new
        self.warnings.extend(other.warnings)
        self.new_records.extend(other.new_records)
        if other.error:
            self.error = other.error
            self.http_status = other.http_status


# --- announcements ---------------------------------------------------------------


@dataclass(frozen=True)
class _AnnSource:
    source: str
    kind: str
    parser_version: str
    parse: Callable[[bytes], ParseResult[AnnouncementRecord]]


def _bse_source(settings: Settings) -> _AnnSource:
    def parse(payload: bytes) -> ParseResult[AnnouncementRecord]:
        return bse.parse_announcements(payload, attachment_base=settings.bse_attachment_live_base)

    return _AnnSource(bse.SOURCE, bse.KIND, bse.PARSER_VERSION, parse)


def _nse_source() -> _AnnSource:
    return _AnnSource(nse.SOURCE, nse.KIND, nse.PARSER_VERSION, nse.parse_announcements)


def announcement_source(settings: Settings, kind: str) -> _AnnSource:
    if kind == bse.KIND:
        return _bse_source(settings)
    if kind == nse.KIND:
        return _nse_source()
    raise ValueError(f"not an announcement kind: {kind}")


async def safe_get(svc: Services, url: str, **kwargs: Any) -> Fetched | Outcome:
    try:
        return await svc.client.get(url, **kwargs)
    except FetchError as exc:
        return Outcome(ok=False, http_status=exc.status, error=str(exc))


def _ingest_announcement_payload(
    svc: Services,
    src: _AnnSource,
    fetched: Fetched,
    *,
    job: str,
    mode: IngestMode,
    request_meta: dict[str, Any],
) -> Outcome:
    now = svc.clock()
    if not fetched.ok:
        outcome = Outcome(ok=False, http_status=fetched.status, error=f"HTTP {fetched.status}")
        with svc.engine.begin() as conn:
            repo.log_fetch(
                conn,
                job=job,
                url=fetched.url,
                started_at=fetched.fetched_at,
                elapsed_ms=fetched.elapsed_ms,
                http_status=fetched.status,
                ok=False,
                error=outcome.error,
            )
        return outcome

    try:
        parsed = src.parse(fetched.content)
    except PayloadError as exc:
        with svc.engine.begin() as conn:
            # Keep the bad payload: it is the evidence needed to fix the parser.
            bad_doc_id = repo.save_raw(
                conn,
                svc.store,
                fetched.content,
                kind=f"bad_{src.kind}",
                source=src.source,
                url=fetched.url,
                content_type=fetched.content_type,
                fetched_at=fetched.fetched_at,
                meta={**request_meta, "error": str(exc)},
            )
            repo.log_fetch(
                conn,
                job=job,
                url=fetched.url,
                started_at=fetched.fetched_at,
                elapsed_ms=fetched.elapsed_ms,
                http_status=fetched.status,
                ok=False,
                error=str(exc),
                doc_id=bad_doc_id,
            )
        return Outcome(ok=False, http_status=fetched.status, error=str(exc))

    with svc.engine.begin() as conn:
        fresh = repo.new_announcements(conn, parsed.records)
        doc_id: str | None = None
        inserted: list[AnnouncementRecord] = []
        # Live polls mostly return rows we already have; store raw only when
        # something is new. Backfill pages are always kept as coverage evidence.
        if fresh or mode == "backfill":
            content, raw_meta = fetched.content, {**request_meta, "mode": mode}
            if src.source == nse.SOURCE and mode != "backfill":
                content = nse.subset_payload(content, {r.source_ann_id for r in fresh})
                raw_meta.update(subset=True, full_size_bytes=len(fetched.content))
            doc_id = repo.save_raw(
                conn,
                svc.store,
                content,
                kind=src.kind,
                source=src.source,
                url=fetched.url,
                content_type=fetched.content_type,
                fetched_at=fetched.fetched_at,
                meta=raw_meta,
            )
            inserted = repo.insert_announcements(
                conn,
                parsed.records,
                raw_doc_id=doc_id,
                parser_version=src.parser_version,
                mode=mode,
                fetched_at=fetched.fetched_at,
                now=now,
            )
        repo.log_fetch(
            conn,
            job=job,
            url=fetched.url,
            started_at=fetched.fetched_at,
            elapsed_ms=fetched.elapsed_ms,
            http_status=fetched.status,
            ok=True,
            doc_id=doc_id,
            n_records=len(parsed.records),
            n_new=len(inserted),
        )
    if inserted:  # a re-polled page repeats old warnings; report them once
        for warning in parsed.warnings:
            log.warning("parser %s", kv(job=job, warning=warning))
    meta = {**parsed.meta, "descending": _is_descending(parsed.records)}
    return Outcome(
        ok=True,
        n_records=len(parsed.records),
        n_new=len(inserted),
        http_status=fetched.status,
        warnings=parsed.warnings,
        meta=meta,
        new_records=inserted,
    )


def _is_descending(records: list[AnnouncementRecord]) -> bool:
    stamps = [r.event_ts for r in records if r.event_ts is not None]
    return len(stamps) < 2 or stamps[0] >= stamps[-1]


async def collect_bse(
    svc: Services,
    start: date,
    end: date,
    *,
    job: str,
    mode: IngestMode,
    max_pages: int,
    stop_when_no_new: bool,
) -> Outcome:
    """Page through BSE announcements for ``[start, end]`` (pass one day).

    Live mode stops at the first page with nothing new; if the API sorts
    oldest-first, paging runs from the end instead.

    A page that should hold rows but comes back empty, as ``{}``, or as an
    HTML block page is an anomaly (BSE throttling, seen 2026-09-28). Such pages
    are retried after a pause. ``meta["complete"]`` is True only when every
    page was fetched cleanly; backfill bookkeeping relies on it.
    """
    settings = svc.settings
    src = _bse_source(settings)
    total = Outcome(ok=True, meta={"complete": False})
    warmup = settings.bse_referer if settings.bse_warmup else None

    async def fetch_page_once(page: int) -> Outcome:
        params = bse.request_params(start, end, page)
        got = await safe_get(
            svc,
            settings.bse_announcements_url,
            params=params,
            headers=bse.request_headers(settings.bse_referer),
            warmup_url=warmup,
        )
        if isinstance(got, Outcome):
            log_transport_failure(svc, job, settings.bse_announcements_url, got)
            return got
        return _ingest_announcement_payload(
            svc, src, got, job=job, mode=mode, request_meta={"params": params}
        )

    async def fetch_page(page: int, *, expect_rows: bool) -> Outcome:
        retry = mode == "backfill" or page > 1
        attempts = settings.bse_page_retries + 1 if retry else 1
        outcome = Outcome(ok=False, error="not attempted")
        for attempt in range(attempts):
            outcome = await fetch_page_once(page)
            anomalous = (
                outcome.ok
                and outcome.n_records == 0
                and (expect_rows or outcome.meta.get("empty_object"))
            )
            if outcome.ok and not anomalous:
                return outcome
            if anomalous:
                outcome = Outcome(
                    ok=False,
                    http_status=outcome.http_status,
                    error=f"BSE page {page} returned no rows (throttled?)",
                    meta=outcome.meta,
                )
            if attempt + 1 < attempts:
                delay = settings.bse_page_retry_delay_s * (attempt + 1)
                log.warning(
                    "bse page retry %s", kv(job=job, page=page, error=outcome.error, delay_s=delay)
                )
                await svc.sleep(delay)
        return outcome

    first = await fetch_page(1, expect_rows=False)
    if first.meta.get("empty_object") and mode != "backfill":
        # Live: `{}` for today is treated as "nothing yet", not as a failure,
        # so an idle poll does not trip the job's error backoff.
        first = Outcome(ok=True, meta=first.meta)
    total.merge(first)
    if not first.ok or first.meta.get("empty_object"):
        return total
    if first.n_records == 0:
        total.meta["complete"] = True  # a real, empty Table
        return total

    expected = first.meta.get("row_count")
    pages_meta = first.meta.get("total_pages")
    if pages_meta is None and first.n_records >= bse.PAGE_SIZE:
        # A full first page and no page count: completeness cannot be judged.
        total.meta.update(expected_records=expected)
        total.error = "BSE gave no page count for a full page"
        total.ok = False
        return total
    reported_pages = int(pages_meta or 1)
    total_pages = min(reported_pages, max_pages)
    descending = bool(first.meta.get("descending", True))
    total.meta.update(descending=descending, total_pages=reported_pages, expected_records=expected)
    if stop_when_no_new and descending and first.n_new == 0:
        # Newest-first and nothing new on page 1 means nothing new anywhere.
        return total

    if stop_when_no_new and not descending:
        pages = range(total_pages, 1, -1)
    else:
        pages = range(2, total_pages + 1)
    for page in pages:
        outcome = await fetch_page(page, expect_rows=True)
        total.merge(outcome)
        if not outcome.ok:
            return total
        if stop_when_no_new and outcome.n_new == 0:
            return total
    # Every page fetched is not enough: BSE's ROWCNT must also be met, so a
    # wrong page count can never mark a short day complete again.
    total.meta["complete"] = reported_pages <= max_pages and (
        expected is None or total.n_records >= expected
    )
    if not total.meta["complete"] and reported_pages <= max_pages:
        total.error = f"BSE gave {total.n_records} of {expected} rows"
    return total


async def collect_nse(
    svc: Services, start: date, end: date, *, job: str, mode: IngestMode
) -> Outcome:
    settings = svc.settings
    params = nse.request_params(start, end)
    got = await safe_get(
        svc,
        settings.nse_announcements_url,
        params=params,
        headers=nse.request_headers(settings.nse_announcements_referer),
        warmup_url=settings.nse_home_url,
    )
    if isinstance(got, Outcome):
        log_transport_failure(svc, job, settings.nse_announcements_url, got)
        return got
    outcome = _ingest_announcement_payload(
        svc, _nse_source(), got, job=job, mode=mode, request_meta={"params": params}
    )
    outcome.meta["complete"] = outcome.ok
    return outcome


async def backfill_day(
    svc: Services, source: str, day: date, *, job: str, max_pages: int
) -> Outcome:
    """Load one full day of announcements and record whether it is complete.

    Only a complete day is skipped by later runs; a partial day (throttled
    mid-way, laptop shut down) is retried, and idempotent inserts make the
    retry cheap and safe.
    """
    if source == bse.SOURCE:
        outcome = await collect_bse(
            svc, day, day, job=job, mode="backfill", max_pages=max_pages, stop_when_no_new=False
        )
    elif source == nse.SOURCE:
        outcome = await collect_nse(svc, day, day, job=job, mode="backfill")
        outcome.meta.setdefault("expected_records", outcome.n_records if outcome.ok else None)
    else:
        raise ValueError(f"unknown source: {source}")
    complete = bool(outcome.ok and outcome.meta.get("complete"))
    if not outcome.ok and outcome.http_status is None:
        # No answer from the server (network down, PC offline): not the day's
        # fault, so it does not count towards giving up.
        outcome.meta["backfill_status"] = "incomplete"
        return outcome
    with svc.engine.begin() as conn:
        status = repo.record_backfill_day(
            conn,
            source,
            day,
            complete=complete,
            n_records=outcome.n_records,
            expected_records=outcome.meta.get("expected_records"),
            error=None if complete else (outcome.error or "incomplete"),
            now=svc.clock(),
            max_attempts=svc.settings.reconcile_max_attempts,
        )
    outcome.meta["backfill_status"] = status
    return outcome


def log_transport_failure(svc: Services, job: str, url: str, outcome: Outcome) -> None:
    with svc.engine.begin() as conn:
        repo.log_fetch(
            conn,
            job=job,
            url=url,
            started_at=svc.clock(),
            ok=False,
            http_status=outcome.http_status,
            error=outcome.error,
        )


# --- daily files -----------------------------------------------------------------


@dataclass(frozen=True)
class DailyFile:
    """A file NSE publishes once per session under a date-stamped URL.

    The EOD bhavcopy and the index-close file share one ingest path: fetch,
    keep the raw bytes, parse with a date check, write, and record the day's
    outcome (loaded / not published / another day's copy) so retries stop.
    """

    kind: str
    source: str
    parser_version: str
    url: Callable[[Settings, date], str]
    parse: Callable[[bytes, date], ParseResult[Any]]
    write: Callable[[Connection, Sequence[Any], str, str, datetime], None]
    days_table: Table
    rows_table: Table


def _write_eod(
    conn: Connection, records: Sequence[Any], doc_id: str, version: str, available_at: datetime
) -> None:
    repo.upsert_eod(
        conn, records, raw_doc_id=doc_id, parser_version=version, available_at=available_at
    )


def _write_index(
    conn: Connection, records: Sequence[Any], doc_id: str, version: str, available_at: datetime
) -> None:
    repo.upsert_index_eod(
        conn, records, raw_doc_id=doc_id, parser_version=version, available_at=available_at
    )


EOD_FILE = DailyFile(
    kind=nse_archives.EOD_KIND,
    source=nse_archives.SOURCE,
    parser_version=nse_archives.EOD_PARSER_VERSION,
    url=lambda s, day: nse_archives.eod_url(s.nse_eod_url_template, day),
    parse=lambda payload, day: nse_archives.parse_eod(payload, trade_date=day),
    write=_write_eod,
    days_table=eod_days,
    rows_table=eod_prices,
)

INDEX_FILE = DailyFile(
    kind=nse_indices.KIND,
    source=nse_indices.SOURCE,
    parser_version=nse_indices.PARSER_VERSION,
    url=lambda s, day: nse_indices.url(s.nse_index_close_url_template, day),
    parse=lambda payload, day: nse_indices.parse_index_close(payload, trade_date=day),
    write=_write_index,
    days_table=index_days,
    rows_table=index_eod,
)


async def ingest_daily_file(
    svc: Services, spec: DailyFile, day: date, *, job: str, mode: IngestMode
) -> Outcome:
    settings = svc.settings
    url = spec.url(settings, day)
    got = await safe_get(svc, url, warmup_url=settings.nse_home_url)
    if isinstance(got, Outcome):
        log_transport_failure(svc, job, url, got)
        return got
    if got.status == 404:
        # Normal on weekends and before publication; recorded so retries are bounded.
        with svc.engine.begin() as conn:
            repo.log_fetch(
                conn,
                job=job,
                url=url,
                started_at=got.fetched_at,
                elapsed_ms=got.elapsed_ms,
                http_status=404,
                ok=True,
                n_records=0,
            )
            repo.record_eod_day(
                conn,
                day,
                status="not_published",
                n_records=0,
                now=svc.clock(),
                http_status=404,
                table=spec.days_table,
            )
        return Outcome(ok=True, http_status=404, meta={"not_found": True})
    if not got.ok:
        with svc.engine.begin() as conn:
            repo.log_fetch(
                conn,
                job=job,
                url=url,
                started_at=got.fetched_at,
                elapsed_ms=got.elapsed_ms,
                http_status=got.status,
                ok=False,
                error=f"HTTP {got.status}",
            )
        return Outcome(ok=False, http_status=got.status, error=f"HTTP {got.status}")

    available_at = (
        ist_datetime(day, settings.eod_publish_after_ist) if mode == "backfill" else got.fetched_at
    )
    # Raw first: the payload is stored before the parser sees it, so even a
    # parser bug cannot lose what the exchange sent.
    with svc.engine.begin() as conn:
        doc_id = repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=spec.kind,
            source=spec.source,
            url=url,
            content_type=got.content_type,
            fetched_at=got.fetched_at,
            meta={
                "trade_date": day.isoformat(),
                "mode": mode,
                "available_at": available_at.isoformat(),
            },
        )
    try:
        parsed = spec.parse(got.content, day)
    except PayloadError as exc:
        return record_bad_payload(
            svc,
            got,
            job=job,
            kind=spec.kind,
            error=exc,
            meta={"trade_date": day.isoformat()},
            source=spec.source,
        )
    with svc.engine.begin() as conn:
        spec.write(conn, parsed.records, doc_id, spec.parser_version, available_at)
        dates_seen = [date.fromisoformat(d) for d in parsed.meta.get("dates_seen", [])]
        if parsed.records:
            repo.record_eod_day(
                conn,
                day,
                status="loaded",
                n_records=len(parsed.records),
                now=svc.clock(),
                http_status=got.status,
                table=spec.days_table,
            )
        else:
            # NSE serves another session's file under a holiday's EOD URL.
            repo.record_eod_day(
                conn,
                day,
                status="other_day" if dates_seen else "not_published",
                n_records=0,
                now=svc.clock(),
                file_date=max(dates_seen) if dates_seen else None,
                http_status=got.status,
                table=spec.days_table,
            )
        repo.log_fetch(
            conn,
            job=job,
            url=url,
            started_at=got.fetched_at,
            elapsed_ms=got.elapsed_ms,
            http_status=got.status,
            ok=True,
            doc_id=doc_id,
            n_records=len(parsed.records),
            n_new=len(parsed.records),
        )
    return Outcome(
        ok=True,
        n_records=len(parsed.records),
        n_new=len(parsed.records),
        http_status=got.status,
        warnings=parsed.warnings,
    )


async def ingest_eod_day(svc: Services, day: date, *, job: str, mode: IngestMode) -> Outcome:
    return await ingest_daily_file(svc, EOD_FILE, day, job=job, mode=mode)


async def ingest_index_day(svc: Services, day: date, *, job: str, mode: IngestMode) -> Outcome:
    return await ingest_daily_file(svc, INDEX_FILE, day, job=job, mode=mode)


def record_bad_payload(
    svc: Services,
    got: Fetched,
    *,
    job: str,
    kind: str,
    error: PayloadError,
    meta: dict[str, Any],
    source: str = nse_archives.SOURCE,
) -> Outcome:
    """Keep a payload that failed to parse: it is the evidence for the fix."""
    with svc.engine.begin() as conn:
        doc_id = repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=f"bad_{kind}",
            source=source,
            url=got.url,
            content_type=got.content_type,
            fetched_at=got.fetched_at,
            meta={**meta, "error": str(error)},
        )
        repo.log_fetch(
            conn,
            job=job,
            url=got.url,
            started_at=got.fetched_at,
            elapsed_ms=got.elapsed_ms,
            http_status=got.status,
            ok=False,
            error=str(error),
            doc_id=doc_id,
        )
    return Outcome(ok=False, http_status=got.status, error=str(error))


async def ingest_snapshot(svc: Services, what: str, *, job: str) -> Outcome:
    """Fetch today's price bands or instrument list (NSE publishes only the
    current file, so history exists only from the day recording starts)."""
    settings = svc.settings
    if what == "bands":
        url, kind, version = (
            settings.nse_bands_url,
            nse_archives.BANDS_KIND,
            nse_archives.BANDS_PARSER_VERSION,
        )
    elif what == "instruments":
        url, kind, version = (
            settings.nse_instruments_url,
            nse_archives.INSTRUMENTS_KIND,
            nse_archives.INSTRUMENTS_PARSER_VERSION,
        )
    else:
        raise ValueError(f"unknown snapshot: {what}")

    got = await safe_get(svc, url, warmup_url=settings.nse_home_url)
    if isinstance(got, Outcome):
        log_transport_failure(svc, job, url, got)
        return got
    if not got.ok:
        with svc.engine.begin() as conn:
            repo.log_fetch(
                conn,
                job=job,
                url=url,
                started_at=got.fetched_at,
                elapsed_ms=got.elapsed_ms,
                http_status=got.status,
                ok=False,
                error=f"HTTP {got.status}",
            )
        return Outcome(ok=False, http_status=got.status, error=f"HTTP {got.status}")

    # Same clock the scheduler uses to decide "already have today".
    as_of = ist_today(svc.clock())
    meta = {
        "as_of_date": as_of.isoformat(),
        "mode": "live",
        "available_at": got.fetched_at.isoformat(),
    }
    try:
        if what == "bands":
            bands = nse_archives.parse_bands(got.content)
            n, warnings = len(bands.records), bands.warnings
        else:
            instruments = nse_archives.parse_instruments(got.content)
            n, warnings = len(instruments.records), instruments.warnings
    except PayloadError as exc:
        return record_bad_payload(svc, got, job=job, kind=kind, error=exc, meta=meta)

    with svc.engine.begin() as conn:
        doc_id = repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=kind,
            source=nse_archives.SOURCE,
            url=url,
            content_type=got.content_type,
            fetched_at=got.fetched_at,
            meta=meta,
        )
        if what == "bands":
            repo.upsert_bands(
                conn,
                as_of,
                bands.records,
                raw_doc_id=doc_id,
                parser_version=version,
                available_at=got.fetched_at,
            )
        else:
            repo.upsert_instruments(
                conn,
                as_of,
                instruments.records,
                raw_doc_id=doc_id,
                parser_version=version,
                available_at=got.fetched_at,
            )
        repo.log_fetch(
            conn,
            job=job,
            url=url,
            started_at=got.fetched_at,
            elapsed_ms=got.elapsed_ms,
            http_status=got.status,
            ok=True,
            doc_id=doc_id,
            n_records=n,
            n_new=n,
        )
    return Outcome(ok=True, n_records=n, n_new=n, http_status=got.status, warnings=warnings)


def snapshot_table_for(what: str) -> Any:
    return {
        "bands": price_bands,
        "instruments": instrument_snapshots,
        "eod": eod_prices,
        "indices": index_eod,
    }[what]


# --- attachments -----------------------------------------------------------------


def attachment_wanted(
    texts: Sequence[str | None], include: Sequence[str], exclude: Sequence[str]
) -> bool:
    """Storage policy: exclude wins; an empty include list means everything."""
    haystack = " | ".join(t.lower() for t in texts if t)
    if any(word.lower() in haystack for word in exclude):
        return False
    return not include or any(word.lower() in haystack for word in include)


async def download_attachment(svc: Services, row: Any, *, job: str, max_bytes: int) -> str:
    """Download one announcement's attachment; returns the new status.

    BSE moves files from its live folder to a historical one, so both are
    tried; a 404 on every candidate is ``missing``, transport errors are
    ``failed`` (retried later), oversize files are ``too_large``.
    """
    settings = svc.settings
    if row.source == bse.SOURCE:
        candidates = bse.attachment_candidates(
            row.attachment_url,
            settings.bse_attachment_live_base,
            settings.bse_attachment_hist_base,
        )
        warmup = None
    else:
        candidates, warmup = [row.attachment_url], settings.nse_home_url

    status, doc_id, error = "missing", None, None
    for url in candidates:
        got = await safe_get(svc, url, warmup_url=warmup, max_bytes=max_bytes)
        if isinstance(got, Outcome):
            status, error = "failed", got.error
            continue
        if got.status == 404:
            continue
        if got.too_large:
            status = "too_large"
            break
        if not got.ok:
            status, error = "failed", f"HTTP {got.status}"
            continue
        with svc.engine.begin() as conn:
            doc_id = repo.save_raw(
                conn,
                svc.store,
                got.content,
                kind="attachment",
                source=row.source,
                url=url,
                content_type=got.content_type,
                fetched_at=got.fetched_at,
                meta={"ann_id": row.id},
            )
        status = "done"
        break

    with svc.engine.begin() as conn:
        repo.mark_attachment(conn, row.id, status=status, doc_id=doc_id)
        repo.log_fetch(
            conn,
            job=job,
            url=row.attachment_url,
            started_at=svc.clock(),
            ok=status != "failed",
            error=error,
            doc_id=doc_id,
            n_records=1,
            n_new=1 if status == "done" else 0,
        )
    return status


async def fetch_pending_attachments(svc: Services, *, job: str) -> Outcome:
    """Download queued attachments of recent filings that pass the storage
    policy.

    Policy skips cost no requests, so a wider slice of the queue is scanned
    than the download budget (``attachments_batch``) allows. Filings older
    than ``attachments_max_age_days`` are left alone: after a backfill the
    queue holds years of history, which is fetched only on request
    (:func:`fetch_attachments_for`).
    """
    settings = svc.settings
    max_bytes = int(settings.attachments_max_mb * 1024 * 1024)
    since = svc.clock() - timedelta(days=settings.attachments_max_age_days)
    with svc.engine.begin() as conn:
        rows = repo.pending_attachments(
            conn, settings.attachments_batch * 10, settings.attachments_max_attempts, since
        )
    total = Outcome(ok=True)
    downloads = 0
    for row in rows:
        if not attachment_wanted(
            (row.category, row.subcategory, row.subject),
            settings.attachments_include,
            settings.attachments_exclude,
        ):
            with svc.engine.begin() as conn:
                repo.mark_attachment(conn, row.id, status="skipped", attempted=False)
            total.meta["skipped"] = total.meta.get("skipped", 0) + 1
            continue
        if downloads >= settings.attachments_batch:
            continue
        downloads += 1
        status = await download_attachment(svc, row, job=job, max_bytes=max_bytes)
        total.n_records += 1
        total.n_new += 1 if status == "done" else 0
        if status == "failed":
            total.warnings.append(f"attachment {row.id}: failed")
    return total


async def fetch_attachments_for(
    svc: Services, announcement_ids: Sequence[int], *, job: str, limit: int
) -> Outcome:
    """Download the attachments of chosen filings, regardless of the storage
    policy (they were chosen because a study needs them). The size cap and
    retry limits still apply."""
    settings = svc.settings
    max_bytes = int(settings.attachments_max_mb * 1024 * 1024)
    with svc.engine.begin() as conn:
        rows = repo.attachments_for(conn, announcement_ids, settings.attachments_max_attempts)
    total = Outcome(ok=True)
    for row in rows[:limit]:
        status = await download_attachment(svc, row, job=job, max_bytes=max_bytes)
        total.n_records += 1
        total.n_new += 1 if status == "done" else 0
        total.meta[status] = total.meta.get(status, 0) + 1
    return total


# --- reparse ---------------------------------------------------------------------


def reparse_kind(svc: Services, kind: str) -> dict[str, int]:
    """Re-run the current parser over every stored payload of ``kind``."""
    stats = {"documents": 0, "updated": 0, "inserted": 0, "errors": 0}
    with svc.engine.begin() as conn:
        docs = repo.raw_documents_of_kind(conn, kind)
        if kind in (bse.KIND, nse.KIND):
            seen = {d.doc_id for d in docs}
            source = bse.SOURCE if kind == bse.KIND else nse.SOURCE
            docs += [
                d
                for d in repo.raw_documents_for_announcements(conn, source)
                if d.doc_id not in seen
            ]
    for doc in docs:
        stats["documents"] += 1
        payload = svc.store.get(doc.doc_id)
        meta = dict(doc.meta or {})
        try:
            with svc.engine.begin() as conn:
                if kind in (bse.KIND, nse.KIND):
                    src = announcement_source(svc.settings, kind)
                    parsed = src.parse(payload)
                    updated, inserted = repo.reparse_announcements(
                        conn,
                        parsed.records,
                        raw_doc_id=doc.doc_id,
                        parser_version=src.parser_version,
                        mode=meta.get("mode", "live"),
                        fetched_at=doc.first_fetched_at,
                        now=svc.clock(),
                    )
                    stats["updated"] += updated
                    stats["inserted"] += inserted
                elif kind == nse_archives.EOD_KIND:
                    day = date.fromisoformat(meta["trade_date"])
                    eod = nse_archives.parse_eod(payload, trade_date=day)
                    repo.upsert_eod(
                        conn,
                        eod.records,
                        raw_doc_id=doc.doc_id,
                        parser_version=nse_archives.EOD_PARSER_VERSION,
                        available_at=_meta_available_at(meta, doc.first_fetched_at),
                    )
                    stats["updated"] += len(eod.records)
                elif kind == nse_indices.KIND:
                    day = date.fromisoformat(meta["trade_date"])
                    idx = nse_indices.parse_index_close(payload, trade_date=day)
                    repo.upsert_index_eod(
                        conn,
                        idx.records,
                        raw_doc_id=doc.doc_id,
                        parser_version=nse_indices.PARSER_VERSION,
                        available_at=_meta_available_at(meta, doc.first_fetched_at),
                    )
                    stats["updated"] += len(idx.records)
                elif kind == nse_archives.BANDS_KIND:
                    bands = nse_archives.parse_bands(payload)
                    repo.upsert_bands(
                        conn,
                        date.fromisoformat(meta["as_of_date"]),
                        bands.records,
                        raw_doc_id=doc.doc_id,
                        parser_version=nse_archives.BANDS_PARSER_VERSION,
                        available_at=_meta_available_at(meta, doc.first_fetched_at),
                    )
                    stats["updated"] += len(bands.records)
                elif kind == nse_archives.INSTRUMENTS_KIND:
                    inst = nse_archives.parse_instruments(payload)
                    repo.upsert_instruments(
                        conn,
                        date.fromisoformat(meta["as_of_date"]),
                        inst.records,
                        raw_doc_id=doc.doc_id,
                        parser_version=nse_archives.INSTRUMENTS_PARSER_VERSION,
                        available_at=_meta_available_at(meta, doc.first_fetched_at),
                    )
                    stats["updated"] += len(inst.records)
                elif kind in REFERENCE_KINDS:
                    # Local import: gats.refdata builds on this module.
                    from gats.refdata.ingest import reapply

                    applied = reapply(conn, kind, payload, meta, doc.doc_id, doc.first_fetched_at)
                    stats["updated"] += applied.n_changes
                else:
                    raise ValueError(f"reparse not supported for kind {kind!r}")
        except PayloadError as exc:
            stats["errors"] += 1
            log.error("reparse failed %s", kv(doc_id=doc.doc_id, error=str(exc)))
    return stats


def _meta_available_at(meta: dict[str, Any], fallback: datetime) -> datetime:
    raw = meta.get("available_at")
    return datetime.fromisoformat(raw) if raw else fallback
