"""Quarterly results: which filings exist, and the revenue each states (T4.7).

Two steps, each resumable:

1. :func:`ingest_results_index` asks NSE's two results indexes about one
   symbol and stores every filing with its dissemination time.
2. :func:`fetch_pending_xbrl` downloads the filings' XBRL files and reads
   the quarter's revenue, checking the file against the index row (same
   quarter end, same standalone/consolidated nature) before trusting it.

Raw first as everywhere: index pages and XBRL files go to the raw store
before they are parsed, so a parser fix can be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import Connection, func, select

from gats.db import repo
from gats.db.schema import financial_results
from gats.ingest import Outcome, Services, log_transport_failure, record_bad_payload, safe_get
from gats.sources import nse, nse_results
from gats.sources.models import PayloadError


async def ingest_results_index(svc: Services, symbol: str, *, job: str) -> Outcome:
    """Store one symbol's results filings from both indexes (two requests)."""
    s = svc.settings
    headers = nse.request_headers(s.nse_results_referer)
    total = Outcome(ok=True)
    for kind, url, params, parse in (
        (
            nse_results.LEGACY_KIND,
            s.nse_results_url,
            nse_results.legacy_params(symbol),
            nse_results.parse_legacy_index,
        ),
        (
            nse_results.INTEGRATED_KIND,
            s.nse_integrated_results_url,
            nse_results.integrated_params(symbol),
            nse_results.parse_integrated_index,
        ),
    ):
        got = await safe_get(svc, url, params=params, headers=headers, warmup_url=s.nse_home_url)
        if isinstance(got, Outcome):
            log_transport_failure(svc, job, url, got)
            total.merge(got)
            continue
        if not got.ok:
            with svc.engine.begin() as conn:
                repo.log_fetch(
                    conn, job=job, url=got.url, started_at=got.fetched_at, ok=False,
                    elapsed_ms=got.elapsed_ms, http_status=got.status, error=f"HTTP {got.status}",
                )  # fmt: skip
            total.merge(Outcome(ok=False, http_status=got.status, error=f"HTTP {got.status}"))
            continue
        meta = {"symbol": symbol}
        try:
            parsed = parse(got.content)
        except PayloadError as exc:
            total.merge(
                record_bad_payload(
                    svc, got, job=job, kind=kind, error=exc, meta=meta, source=nse_results.SOURCE
                )
            )
            continue
        with svc.engine.begin() as conn:
            doc_id = repo.save_raw(
                conn, svc.store, got.content, kind=kind, source=nse_results.SOURCE, url=got.url,
                content_type=got.content_type, fetched_at=got.fetched_at, meta=meta,
            )  # fmt: skip
            rows = [_row(f, doc_id) for f in parsed.records if f.disseminated_ts is not None]
            before = _count(conn, symbol)
            repo.insert_ignore(
                conn, financial_results, rows, ["symbol", "period_end", "consolidated", "seq"]
            )
            new = _count(conn, symbol) - before
            repo.log_fetch(
                conn, job=job, url=got.url, started_at=got.fetched_at, ok=True,
                elapsed_ms=got.elapsed_ms, http_status=got.status, doc_id=doc_id,
                n_records=len(rows), n_new=new,
            )  # fmt: skip
        total.merge(Outcome(ok=True, n_records=len(rows), n_new=new, warnings=parsed.warnings))
    return total


def _count(conn: Connection, symbol: str) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(financial_results)
            .where(financial_results.c.symbol == symbol)
        ).scalar_one()
    )


def _row(filing: nse_results.ResultFiling, doc_id: str) -> dict[str, Any]:
    return {
        "symbol": filing.symbol,
        "period_end": filing.period_end,
        "consolidated": filing.consolidated,
        "seq": filing.seq or "",
        "regime": filing.regime,
        "period_start": filing.period_start,
        "audited": filing.audited,
        "revised": filing.revised,
        "isin": filing.isin,
        "xbrl_url": filing.xbrl_url,
        "xbrl_status": "pending" if filing.xbrl_url else "none",
        "xbrl_attempts": 0,
        "event_ts": filing.disseminated_ts,
        "available_at": filing.disseminated_ts,
        "raw_doc_id": doc_id,
        "parser_version": nse_results.PARSER_VERSION,
    }


@dataclass
class XbrlStats:
    attempted: int = 0
    done: int = 0
    failed: int = 0


async def fetch_pending_xbrl(
    svc: Services, *, since: date, limit: int, job: str, max_attempts: int = 3
) -> XbrlStats:
    """Read revenue from up to ``limit`` filings' XBRL (quarters ending on or
    after ``since``), newest first."""
    f = financial_results
    with svc.engine.begin() as conn:
        # Trailing revenue prefers consolidated figures, so a standalone file
        # is read only for a quarter that has no consolidated filing.
        other = financial_results.alias("other")
        has_consolidated = (
            select(other.c.seq)
            .where(
                other.c.symbol == f.c.symbol,
                other.c.period_end == f.c.period_end,
                other.c.consolidated.is_(True),
            )
            .exists()
        )
        pending = conn.execute(
            select(f)
            .where(
                f.c.xbrl_status.in_(["pending", "failed"]),
                f.c.xbrl_attempts < max_attempts,
                f.c.period_end >= since,
                f.c.consolidated.is_(True) | ~has_consolidated,
            )
            .order_by(f.c.period_end.desc(), f.c.symbol)
            .limit(limit)
        ).all()
    stats = XbrlStats()
    for row in pending:
        stats.attempted += 1
        status, note, revenue, doc_id = await _read_xbrl(svc, row, job)
        stats.done += status == "done"
        stats.failed += status == "failed"
        with svc.engine.begin() as conn:
            conn.execute(
                f.update()
                .where(
                    f.c.symbol == row.symbol,
                    f.c.period_end == row.period_end,
                    f.c.consolidated == row.consolidated,
                    f.c.seq == row.seq,
                )
                .values(
                    xbrl_status=status,
                    xbrl_note=note,
                    revenue=revenue,
                    xbrl_doc_id=doc_id,
                    xbrl_attempts=row.xbrl_attempts + 1,
                )
            )
    return stats


async def _read_xbrl(
    svc: Services, row: Any, job: str
) -> tuple[str, str | None, float | None, str | None]:
    """(status, note, revenue, raw doc) for one filing's XBRL."""
    got = await safe_get(svc, row.xbrl_url, warmup_url=svc.settings.nse_home_url)
    if isinstance(got, Outcome):
        log_transport_failure(svc, job, row.xbrl_url, got)
        return "failed", got.error, None, None
    with svc.engine.begin() as conn:
        doc_id = None
        if got.ok:
            doc_id = repo.save_raw(
                conn, svc.store, got.content, kind=nse_results.XBRL_KIND,
                source=nse_results.SOURCE, url=got.url, content_type=got.content_type,
                fetched_at=got.fetched_at, meta={"symbol": row.symbol, "seq": row.seq},
            )  # fmt: skip
        repo.log_fetch(
            conn, job=job, url=got.url, started_at=got.fetched_at, ok=got.ok,
            elapsed_ms=got.elapsed_ms, http_status=got.status, doc_id=doc_id,
            error=None if got.ok else f"HTTP {got.status}",
        )  # fmt: skip
    if not got.ok:
        return "failed", f"HTTP {got.status}", None, None
    try:
        facts = nse_results.parse_results_xbrl(got.content)
    except PayloadError as exc:
        return "failed", str(exc)[:300], None, doc_id
    # The file must be the filing the index row describes.
    if facts.period_end != row.period_end:
        return (
            "failed",
            f"XBRL quarter ends {facts.period_end}, index says {row.period_end}",
            None,
            doc_id,
        )
    if facts.consolidated is not None and facts.consolidated != row.consolidated:
        return "failed", "XBRL and index disagree on standalone/consolidated", None, doc_id
    if facts.revenue is None:
        return "done", "no RevenueFromOperations (a bank or NBFC format)", None, doc_id
    return "done", None, facts.revenue, doc_id
