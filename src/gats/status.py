"""Health summary for ``gats status``."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import quantiles
from typing import Any

from sqlalchemy import Connection, func, select

from gats.config import Settings
from gats.db import repo
from gats.db.schema import (
    announcements,
    backfill_days,
    eod_prices,
    fetch_log,
    instrument_snapshots,
    price_bands,
    raw_documents,
)
from gats.timeutil import ist_today


@dataclass
class StatusReport:
    announcements_by_source_mode: dict[str, int] = field(default_factory=dict)
    attachments_by_status: dict[str, int] = field(default_factory=dict)
    eod_days: int = 0
    eod_latest: str | None = None
    bands_latest: str | None = None
    instruments_latest: str | None = None
    backfill_days: dict[str, int] = field(default_factory=dict)
    raw_docs: int = 0
    raw_bytes: int = 0
    last_fetch_by_job: dict[str, dict[str, Any]] = field(default_factory=dict)
    live_latency_s: dict[str, dict[str, float]] = field(default_factory=dict)
    heartbeat: dict[str, Any] | None = None
    # Inputs to the health rules (gats.health).
    bse_failures_last_hour: int = 0
    data_bytes: int = 0
    disk_free_bytes: int | None = None
    reconcile_queue: int = 0
    gave_up_days: int = 0
    gave_up: list[str] = field(default_factory=list)


def _latency_summary(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    values = sorted(values)
    if len(values) >= 2:
        cuts = quantiles(values, n=20, method="inclusive")
        p50, p95 = cuts[9], cuts[18]
    else:
        p50 = p95 = values[0]
    return {"n": float(len(values)), "p50": p50, "p95": p95, "max": values[-1]}


def _db_file_bytes(data_dir: Path) -> int:
    """SQLite database plus its WAL/SHM files (0 for Postgres)."""
    return sum(p.stat().st_size for p in data_dir.glob("gats.db*") if p.is_file())


def _reconcile_queue(conn: Connection, now: datetime, settings: Settings) -> int:
    """Source-days the reconcile job would still fetch (mirrors ReconcileJob.due)."""
    sources = [
        src
        for src, enabled in (("BSE", settings.bse_enabled), ("NSE", settings.nse_enabled))
        if enabled
    ]
    today = ist_today(now)
    days = [today - timedelta(days=back) for back in range(1, settings.reconcile_days + 1)]
    done = {
        (row.source, row.day)
        for row in conn.execute(
            select(backfill_days.c.source, backfill_days.c.day).where(
                backfill_days.c.status.in_(("complete", "gave_up")),
                backfill_days.c.day >= min(days),
            )
        )
    }
    return sum(1 for src in sources for day in days if (src, day) not in done)


def build_report(conn: Connection, now: datetime, settings: Settings) -> StatusReport:
    report = StatusReport()
    heartbeat_path = settings.heartbeat_path

    for source, mode, count in conn.execute(
        select(announcements.c.source, announcements.c.ingest_mode, func.count()).group_by(
            announcements.c.source, announcements.c.ingest_mode
        )
    ):
        report.announcements_by_source_mode[f"{source}/{mode}"] = int(count)

    for status, count in conn.execute(
        select(announcements.c.attachment_status, func.count()).group_by(
            announcements.c.attachment_status
        )
    ):
        report.attachments_by_status[str(status)] = int(count)

    days, latest = conn.execute(
        select(
            func.count(func.distinct(eod_prices.c.trade_date)), func.max(eod_prices.c.trade_date)
        )
    ).one()
    report.eod_days, report.eod_latest = int(days or 0), str(latest) if latest else None
    bands_latest = conn.execute(select(func.max(price_bands.c.as_of_date))).scalar()
    report.bands_latest = str(bands_latest) if bands_latest else None
    inst_latest = conn.execute(select(func.max(instrument_snapshots.c.as_of_date))).scalar()
    report.instruments_latest = str(inst_latest) if inst_latest else None

    report.backfill_days = repo.backfill_summary(conn)

    docs, size = conn.execute(
        select(func.count(), func.coalesce(func.sum(raw_documents.c.size_bytes), 0))
    ).one()
    report.raw_docs, report.raw_bytes = int(docs), int(size)

    latest_ids = select(func.max(fetch_log.c.id)).group_by(fetch_log.c.job).scalar_subquery()
    for row in conn.execute(select(fetch_log).where(fetch_log.c.id.in_(latest_ids))):
        report.last_fetch_by_job[row.job] = {
            "at": row.started_at.isoformat(),
            "ok": row.ok,
            "http_status": row.http_status,
            "error": row.error,
        }

    since = now - timedelta(hours=24)
    by_source: dict[str, list[float]] = {}
    for row in conn.execute(
        select(
            announcements.c.source,
            announcements.c.available_at,
            announcements.c.exch_disseminated_ts,
        ).where(
            announcements.c.ingest_mode == "live",
            announcements.c.available_at >= since,
            announcements.c.exch_disseminated_ts.is_not(None),
        )
    ):
        delta = (row.available_at - row.exch_disseminated_ts).total_seconds()
        by_source.setdefault(row.source, []).append(delta)
    report.live_latency_s = {src: _latency_summary(v) for src, v in by_source.items()}

    hour_ago = now - timedelta(hours=1)
    report.bse_failures_last_hour = int(
        conn.execute(
            select(func.count())
            .select_from(fetch_log)
            .where(
                fetch_log.c.ok.is_(False),
                fetch_log.c.started_at >= hour_ago,
                fetch_log.c.url.like("%bseindia.com%"),
            )
        ).scalar_one()
    )
    # Uncompressed raw bytes overstate the gzip store, so this errs high; a
    # directory walk would be exact but slow once the store holds 100k+ blobs.
    report.data_bytes = report.raw_bytes + _db_file_bytes(settings.data_dir)
    if settings.data_dir.exists():
        report.disk_free_bytes = shutil.disk_usage(settings.data_dir).free
    report.reconcile_queue = (
        _reconcile_queue(conn, now, settings) if settings.reconcile_enabled else 0
    )
    report.gave_up = [
        f"{row.source} {row.day}"
        for row in conn.execute(
            select(backfill_days.c.source, backfill_days.c.day)
            .where(backfill_days.c.status == "gave_up")
            .order_by(backfill_days.c.day)
        )
    ]
    report.gave_up_days = len(report.gave_up)

    if heartbeat_path.exists():
        try:
            report.heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report.heartbeat = {"error": "unreadable heartbeat file"}
    return report
