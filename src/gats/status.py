"""Health summary for ``gats status``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import quantiles
from typing import Any

from sqlalchemy import Connection, func, select

from gats.db import repo
from gats.db.schema import (
    announcements,
    eod_prices,
    fetch_log,
    instrument_snapshots,
    price_bands,
    raw_documents,
)


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


def build_report(conn: Connection, now: datetime, heartbeat_path: Path) -> StatusReport:
    report = StatusReport()

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

    if heartbeat_path.exists():
        try:
            report.heartbeat = json.loads(heartbeat_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            report.heartbeat = {"error": "unreadable heartbeat file"}
    return report
