"""Fetch → raw store → parse → versioned tables, for reference files."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime

from sqlalchemy import Connection

from gats.db import repo
from gats.db.schema import bse_scrips
from gats.ingest import Outcome, Services, log_transport_failure, record_bad_payload, safe_get
from gats.refdata.versions import ApplyStats, apply_snapshot
from gats.sources import bse_scrips as bse_scrips_src
from gats.sources.models import ParseResult, PayloadError
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


def _scrip_rows(records: list[bse_scrips_src.BseScripRecord]) -> list[dict[str, object]]:
    rows = []
    for record in records:
        row = asdict(record)
        row["scrip_group"] = row.pop("group")  # GROUP is an SQL keyword
        rows.append(row)
    return rows


def apply_bse_scrips(
    conn: Connection,
    parsed: ParseResult[bse_scrips_src.BseScripRecord],
    *,
    as_of: date,
    doc_id: str,
    available_at: datetime,
) -> ApplyStats:
    """Apply a parsed scrip list as the ``as_of`` snapshot (live and reparse)."""
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


async def ingest_bse_scrips(svc: Services, *, job: str) -> Outcome:
    """Fetch BSE's full scrip list (all statuses) as today's snapshot."""
    s = svc.settings
    params = bse_scrips_src.request_params()
    got = await safe_get(
        svc,
        s.bse_scrips_url,
        params=params,
        headers=bse_scrips_src.request_headers(s.bse_referer),
        warmup_url=s.bse_referer if s.bse_warmup else None,
    )
    if isinstance(got, Outcome):
        log_transport_failure(svc, job, s.bse_scrips_url, got)
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
    meta = {
        "as_of_date": as_of.isoformat(),
        "mode": "live",
        "available_at": got.fetched_at.isoformat(),
        "params": params,
    }
    try:
        parsed = bse_scrips_src.parse_scrips(got.content)
    except PayloadError as exc:
        return record_bad_payload(
            svc, got, job=job, kind=bse_scrips_src.KIND, error=exc, meta=meta, source="BSE"
        )
    with svc.engine.begin() as conn:
        doc_id = repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=bse_scrips_src.KIND,
            source=bse_scrips_src.SOURCE,
            url=got.url,
            content_type=got.content_type,
            fetched_at=got.fetched_at,
            meta=meta,
        )
        stats = apply_bse_scrips(
            conn, parsed, as_of=as_of, doc_id=doc_id, available_at=got.fetched_at
        )
        n_records = len(parsed.records)
        repo.log_fetch(
            conn,
            job=job,
            url=got.url,
            started_at=got.fetched_at,
            elapsed_ms=got.elapsed_ms,
            http_status=got.status,
            ok=True,
            doc_id=doc_id,
            n_records=n_records,
            n_new=stats.n_changes,
        )
    return Outcome(
        ok=True,
        n_records=n_records,
        n_new=stats.n_changes,
        http_status=got.status,
        warnings=stats.warnings,
        meta={
            "as_of_date": as_of.isoformat(),
            "inserted": stats.inserted,
            "changed": stats.changed,
            "closed": stats.closed,
        },
    )
