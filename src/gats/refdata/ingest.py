"""Fetch → raw store → parse → apply, for reference files.

Every reference file follows the same path: one GET, the raw payload kept,
a pure parser, and an ``apply`` step that writes derived rows and logs the
day in ``refdata_snapshots`` (so the recorder fetches it once a day). The
same ``apply`` runs again under ``gats reparse``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection

from gats.db import repo
from gats.db.schema import bse_scrips
from gats.ingest import Outcome, Services, log_transport_failure, record_bad_payload, safe_get
from gats.refdata import symbols
from gats.refdata.versions import ApplyStats, apply_snapshot, log_snapshot
from gats.sources import bse_scrips as bse_scrips_src
from gats.sources import nse_symbols
from gats.sources.models import PayloadError
from gats.timeutil import ist_today

BSE_SCRIP_ATTRS = (
    "symbol",
    "name",
    "issuer_name",
    "isin",
    "status",
    "scrip_group",
    "face_value",
    "segment",
    "industry",
)

# (conn, payload, as_of, doc_id, available_at) -> stats
Applier = Callable[[Connection, bytes, date, str, datetime], ApplyStats]


@dataclass(frozen=True)
class ReferenceFile:
    kind: str
    source: str
    apply: Applier


# --- appliers: parse + write, shared by the recorder and reparse ----------------------


def _scrip_rows(records: list[bse_scrips_src.BseScripRecord]) -> list[dict[str, Any]]:
    rows = []
    for record in records:
        row = asdict(record)
        row["scrip_group"] = row.pop("group")  # GROUP is an SQL keyword
        rows.append(row)
    return rows


def apply_bse_scrips(
    conn: Connection, payload: bytes, as_of: date, doc_id: str, available_at: datetime
) -> ApplyStats:
    parsed = bse_scrips_src.parse_scrips(payload)
    stats = apply_snapshot(
        conn,
        bse_scrips,
        kind=bse_scrips_src.KIND,
        key="scrip_code",
        attrs=BSE_SCRIP_ATTRS,
        as_of=as_of,
        records=_scrip_rows(parsed.records),
        available_at=available_at,
        raw_doc_id=doc_id,
        parser_version=bse_scrips_src.PARSER_VERSION,
    )
    stats.warnings = parsed.warnings[:20] + stats.warnings
    return stats


def apply_nse_symbol_changes(
    conn: Connection, payload: bytes, as_of: date, doc_id: str, available_at: datetime
) -> ApplyStats:
    parsed = nse_symbols.parse_symbol_changes(payload)
    inserted = symbols.store_changes(
        conn,
        parsed.records,
        fetched_at=available_at,
        raw_doc_id=doc_id,
        parser_version=nse_symbols.PARSER_VERSION,
    )
    log_snapshot(
        conn,
        kind=nse_symbols.KIND,
        as_of=as_of,
        n_records=len(parsed.records),
        n_changes=inserted,
        available_at=available_at,
        raw_doc_id=doc_id,
        parser_version=nse_symbols.PARSER_VERSION,
    )
    return ApplyStats(
        inserted=inserted,
        unchanged=len(parsed.records) - inserted,
        warnings=parsed.warnings[:20],
    )


BSE_SCRIPS = ReferenceFile(bse_scrips_src.KIND, bse_scrips_src.SOURCE, apply_bse_scrips)
NSE_SYMBOL_CHANGES = ReferenceFile(nse_symbols.KIND, nse_symbols.SOURCE, apply_nse_symbol_changes)

FILES: Mapping[str, ReferenceFile] = {f.kind: f for f in (BSE_SCRIPS, NSE_SYMBOL_CHANGES)}


# --- fetch path -------------------------------------------------------------------------


async def ingest_reference_file(
    svc: Services,
    ref: ReferenceFile,
    url: str,
    *,
    job: str,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    warmup_url: str | None = None,
) -> Outcome:
    """Fetch one reference file as today's (IST) snapshot and apply it."""
    got = await safe_get(svc, url, params=params, headers=headers, warmup_url=warmup_url)
    if isinstance(got, Outcome):
        log_transport_failure(svc, job, url, got)
        return got
    if not got.ok:
        with svc.engine.begin() as conn:
            repo.log_fetch(
                conn,
                job=job,
                url=got.url,
                started_at=got.fetched_at,
                elapsed_ms=got.elapsed_ms,
                http_status=got.status,
                ok=False,
                error=f"HTTP {got.status}",
            )
        return Outcome(ok=False, http_status=got.status, error=f"HTTP {got.status}")

    as_of = ist_today(svc.clock())
    meta: dict[str, Any] = {
        "as_of_date": as_of.isoformat(),
        "mode": "live",
        "available_at": got.fetched_at.isoformat(),
    }
    if params:
        meta["params"] = dict(params)
    try:
        with svc.engine.begin() as conn:
            doc_id = repo.save_raw(
                conn,
                svc.store,
                got.content,
                kind=ref.kind,
                source=ref.source,
                url=got.url,
                content_type=got.content_type,
                fetched_at=got.fetched_at,
                meta=meta,
            )
            stats = ref.apply(conn, got.content, as_of, doc_id, got.fetched_at)
            repo.log_fetch(
                conn,
                job=job,
                url=got.url,
                started_at=got.fetched_at,
                elapsed_ms=got.elapsed_ms,
                http_status=got.status,
                ok=True,
                doc_id=doc_id,
                n_records=stats.n_records,
                n_new=stats.n_changes,
            )
    except PayloadError as exc:
        # The transaction rolled back, so nothing was stored as good; keep the
        # payload under bad_<kind> as the evidence for a parser fix.
        return record_bad_payload(
            svc, got, job=job, kind=ref.kind, error=exc, meta=meta, source=ref.source
        )
    return Outcome(
        ok=True,
        n_records=stats.n_records,
        n_new=stats.n_changes,
        http_status=got.status,
        warnings=stats.warnings,
        meta={"as_of_date": as_of.isoformat(), **stats.summary()},
    )


async def ingest_bse_scrips(svc: Services, *, job: str) -> Outcome:
    """BSE's full scrip list (all statuses) as today's snapshot."""
    s = svc.settings
    return await ingest_reference_file(
        svc,
        BSE_SCRIPS,
        s.bse_scrips_url,
        job=job,
        params=bse_scrips_src.request_params(),
        headers=bse_scrips_src.request_headers(s.bse_referer),
        warmup_url=s.bse_referer if s.bse_warmup else None,
    )


async def ingest_nse_symbol_changes(svc: Services, *, job: str) -> Outcome:
    s = svc.settings
    return await ingest_reference_file(
        svc, NSE_SYMBOL_CHANGES, s.nse_symbol_changes_url, job=job, warmup_url=s.nse_home_url
    )


def reapply(
    conn: Connection,
    kind: str,
    payload: bytes,
    meta: Mapping[str, Any],
    doc_id: str,
    fallback_available_at: datetime,
) -> ApplyStats:
    """Re-apply a stored reference payload (``gats reparse``)."""
    raw = meta.get("available_at")
    available_at = datetime.fromisoformat(raw) if raw else fallback_available_at
    return FILES[kind].apply(
        conn, payload, date.fromisoformat(meta["as_of_date"]), doc_id, available_at
    )
