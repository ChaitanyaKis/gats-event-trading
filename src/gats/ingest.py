"""Fetch → store raw → parse → write. Shared by the recorder, backfill,
probe and reparse commands so every path writes data the same way."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Engine

from gats.config import Settings
from gats.db import repo
from gats.db.repo import IngestMode
from gats.db.schema import eod_prices, instrument_snapshots, price_bands
from gats.logging_setup import kv
from gats.net import Fetched, FetchError, PoliteClient
from gats.rawstore import RawStore
from gats.sources import bse, nse, nse_archives
from gats.sources.models import AnnouncementRecord, ParseResult, PayloadError
from gats.timeutil import ist_datetime, ist_today, utcnow

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    engine: Engine
    store: RawStore
    client: PoliteClient
    clock: Callable[[], datetime] = utcnow


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


async def _safe_get(svc: Services, url: str, **kwargs: Any) -> Fetched | Outcome:
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
    """Page through BSE announcements for ``[start, end]``.

    Live mode stops at the first page with nothing new. If the API turns out
    to sort oldest-first, new rows sit on the last pages, so paging reverses
    from the end.
    """
    settings = svc.settings
    src = _bse_source(settings)
    total = Outcome(ok=True)

    async def fetch_page(page: int) -> Outcome:
        params = bse.request_params(start, end, page)
        got = await _safe_get(
            svc,
            settings.bse_announcements_url,
            params=params,
            headers=bse.request_headers(settings.bse_referer),
        )
        if isinstance(got, Outcome):
            _log_transport_failure(svc, job, settings.bse_announcements_url, got)
            return got
        return _ingest_announcement_payload(
            svc, src, got, job=job, mode=mode, request_meta={"params": params}
        )

    first = await fetch_page(1)
    total.merge(first)
    if not first.ok or first.n_records == 0:
        return total
    total_pages = min(int(first.meta.get("total_pages") or 1), max_pages)
    descending = bool(first.meta.get("descending", True))
    total.meta["descending"] = descending
    if total_pages <= 1:
        return total
    if stop_when_no_new and descending and first.n_new == 0:
        # Newest-first and nothing new on page 1 means nothing new anywhere.
        return total

    if stop_when_no_new and not descending:
        pages = range(total_pages, 1, -1)
    else:
        pages = range(2, total_pages + 1)
    for page in pages:
        outcome = await fetch_page(page)
        total.merge(outcome)
        if not outcome.ok or outcome.n_records == 0:
            break
        if stop_when_no_new and outcome.n_new == 0:
            break
    return total


async def collect_nse(
    svc: Services, start: date, end: date, *, job: str, mode: IngestMode
) -> Outcome:
    settings = svc.settings
    params = nse.request_params(start, end)
    got = await _safe_get(
        svc,
        settings.nse_announcements_url,
        params=params,
        headers=nse.request_headers(settings.nse_announcements_referer),
        warmup_url=settings.nse_home_url,
    )
    if isinstance(got, Outcome):
        _log_transport_failure(svc, job, settings.nse_announcements_url, got)
        return got
    return _ingest_announcement_payload(
        svc, _nse_source(), got, job=job, mode=mode, request_meta={"params": params}
    )


def _log_transport_failure(svc: Services, job: str, url: str, outcome: Outcome) -> None:
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


async def ingest_eod_day(svc: Services, day: date, *, job: str, mode: IngestMode) -> Outcome:
    settings = svc.settings
    url = nse_archives.eod_url(settings.nse_eod_url_template, day)
    got = await _safe_get(svc, url, warmup_url=settings.nse_home_url)
    if isinstance(got, Outcome):
        _log_transport_failure(svc, job, url, got)
        return got
    if got.status == 404:
        # Normal on holidays and before publication; logged so retries are bounded.
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
    try:
        parsed = nse_archives.parse_eod(got.content, trade_date=day)
    except PayloadError as exc:
        return _record_bad_daily(
            svc,
            got,
            job=job,
            kind=nse_archives.EOD_KIND,
            error=exc,
            meta={"trade_date": day.isoformat()},
        )
    with svc.engine.begin() as conn:
        doc_id = repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=nse_archives.EOD_KIND,
            source=nse_archives.SOURCE,
            url=url,
            content_type=got.content_type,
            fetched_at=got.fetched_at,
            meta={
                "trade_date": day.isoformat(),
                "mode": mode,
                "available_at": available_at.isoformat(),
            },
        )
        repo.upsert_eod(
            conn,
            parsed.records,
            raw_doc_id=doc_id,
            parser_version=nse_archives.EOD_PARSER_VERSION,
            available_at=available_at,
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


def _record_bad_daily(
    svc: Services,
    got: Fetched,
    *,
    job: str,
    kind: str,
    error: PayloadError,
    meta: dict[str, Any],
) -> Outcome:
    with svc.engine.begin() as conn:
        doc_id = repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=f"bad_{kind}",
            source=nse_archives.SOURCE,
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

    got = await _safe_get(svc, url, warmup_url=settings.nse_home_url)
    if isinstance(got, Outcome):
        _log_transport_failure(svc, job, url, got)
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
        return _record_bad_daily(svc, got, job=job, kind=kind, error=exc, meta=meta)

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
    return {"bands": price_bands, "instruments": instrument_snapshots, "eod": eod_prices}[what]


# --- attachments -----------------------------------------------------------------


async def fetch_pending_attachments(svc: Services, *, job: str) -> Outcome:
    settings = svc.settings
    with svc.engine.begin() as conn:
        rows = repo.pending_attachments(
            conn, settings.attachments_batch, settings.attachments_max_attempts
        )
    total = Outcome(ok=True)
    for row in rows:
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
            got = await _safe_get(svc, url, warmup_url=warmup)
            if isinstance(got, Outcome):
                status, error = "failed", got.error
                continue
            if got.status == 404:
                continue
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
        total.n_records += 1
        total.n_new += 1 if status == "done" else 0
        if status == "failed":
            total.warnings.append(f"attachment {row.id}: {error}")
    return total


# --- reparse ---------------------------------------------------------------------


def reparse_kind(svc: Services, kind: str) -> dict[str, int]:
    """Re-run the current parser over every stored payload of ``kind``."""
    stats = {"documents": 0, "updated": 0, "inserted": 0, "errors": 0}
    with svc.engine.begin() as conn:
        docs = repo.raw_documents_of_kind(conn, kind)
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
                else:
                    raise ValueError(f"reparse not supported for kind {kind!r}")
        except PayloadError as exc:
            stats["errors"] += 1
            log.error("reparse failed %s", kv(doc_id=doc.doc_id, error=str(exc)))
    return stats


def _meta_available_at(meta: dict[str, Any], fallback: datetime) -> datetime:
    raw = meta.get("available_at")
    return datetime.fromisoformat(raw) if raw else fallback
