"""Repository: all SQL writes and the reads the recorder needs.

Writes are idempotent (insert-or-ignore / upsert on natural keys), so any job
can be re-run safely after a crash.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Literal

from sqlalchemy import Connection, Row, Table, func, select, update
from sqlalchemy.dialects import postgresql, sqlite

from gats.db.schema import (
    announcements,
    eod_prices,
    fetch_log,
    instrument_snapshots,
    price_bands,
    raw_documents,
)
from gats.rawstore import RawStore
from gats.sources.models import AnnouncementRecord, BandRecord, EodRecord, InstrumentRecord

IngestMode = Literal["live", "catchup", "backfill"]

_ANN_PARSED_FIELDS = (
    "symbol",
    "scrip_code",
    "isin",
    "company_name",
    "category",
    "subcategory",
    "subject",
    "details",
    "attachment_url",
    "exch_submitted_ts",
    "exch_disseminated_ts",
    "event_ts",
)

_CHUNK = 500


def _dialect_insert(conn: Connection, table: Table) -> sqlite.Insert | postgresql.Insert:
    name = conn.dialect.name
    if name == "sqlite":
        return sqlite.insert(table)
    if name == "postgresql":
        return postgresql.insert(table)
    raise NotImplementedError(f"unsupported database dialect: {name}")


def insert_ignore(
    conn: Connection, table: Table, rows: Sequence[Mapping[str, Any]], conflict_cols: Sequence[str]
) -> None:
    if not rows:
        return
    stmt = _dialect_insert(conn, table)
    conn.execute(stmt.on_conflict_do_nothing(index_elements=list(conflict_cols)), list(rows))


def upsert(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    conflict_cols: Sequence[str],
    update_cols: Sequence[str],
) -> None:
    if not rows:
        return
    stmt = _dialect_insert(conn, table)
    upsert_stmt = stmt.on_conflict_do_update(
        index_elements=list(conflict_cols),
        set_={col: stmt.excluded[col] for col in update_cols},
    )
    conn.execute(upsert_stmt, list(rows))


def _chunks(items: Sequence[str], size: int = _CHUNK) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- raw documents & fetch log ------------------------------------------------


def save_raw(
    conn: Connection,
    store: RawStore,
    data: bytes,
    *,
    kind: str,
    source: str,
    url: str,
    content_type: str | None,
    fetched_at: datetime,
    meta: Mapping[str, Any] | None = None,
) -> str:
    doc_id, rel = store.put(data)
    insert_ignore(
        conn,
        raw_documents,
        [
            {
                "doc_id": doc_id,
                "kind": kind,
                "source": source,
                "url": url,
                "content_type": content_type,
                "size_bytes": len(data),
                "blob_path": rel.as_posix(),
                "first_fetched_at": fetched_at,
                "meta": dict(meta or {}),
            }
        ],
        ["doc_id"],
    )
    return doc_id


def log_fetch(
    conn: Connection,
    *,
    job: str,
    url: str,
    started_at: datetime,
    ok: bool,
    elapsed_ms: int | None = None,
    http_status: int | None = None,
    error: str | None = None,
    doc_id: str | None = None,
    n_records: int | None = None,
    n_new: int | None = None,
) -> None:
    conn.execute(
        fetch_log.insert().values(
            job=job,
            url=url,
            started_at=started_at,
            elapsed_ms=elapsed_ms,
            http_status=http_status,
            ok=ok,
            error=error[:2000] if error else None,
            doc_id=doc_id,
            n_records=n_records,
            n_new=n_new,
        )
    )


def count_not_found(conn: Connection, url: str) -> int:
    return int(
        conn.execute(
            select(func.count())
            .select_from(fetch_log)
            .where(fetch_log.c.url == url, fetch_log.c.http_status == 404)
        ).scalar_one()
    )


def raw_documents_of_kind(conn: Connection, kind: str) -> list[Row[Any]]:
    return list(
        conn.execute(
            select(raw_documents)
            .where(raw_documents.c.kind == kind)
            .order_by(raw_documents.c.first_fetched_at)
        )
    )


# --- announcements --------------------------------------------------------------


def known_announcement_ids(conn: Connection, source: str, ids: Sequence[str]) -> set[str]:
    known: set[str] = set()
    for chunk in _chunks(list(ids)):
        known.update(
            conn.execute(
                select(announcements.c.source_ann_id).where(
                    announcements.c.source == source,
                    announcements.c.source_ann_id.in_(chunk),
                )
            ).scalars()
        )
    return known


def _available_at(record: AnnouncementRecord, mode: IngestMode, fetched_at: datetime) -> datetime:
    event_ts = record.event_ts or fetched_at
    if mode == "backfill":
        # We did not observe it live; the best defensible estimate is the event time.
        return event_ts
    # Live: we knew it when we fetched it. max() guards against local clock skew.
    return max(fetched_at, event_ts)


def _dedupe(records: Iterable[AnnouncementRecord]) -> list[AnnouncementRecord]:
    seen: dict[tuple[str, str], AnnouncementRecord] = {}
    for record in records:
        seen.setdefault((record.source, record.source_ann_id), record)
    return list(seen.values())


def new_announcements(
    conn: Connection, records: Sequence[AnnouncementRecord]
) -> list[AnnouncementRecord]:
    """Records whose (source, source_ann_id) is not yet stored."""
    unique = _dedupe(records)
    by_source: dict[str, list[str]] = {}
    for record in unique:
        by_source.setdefault(record.source, []).append(record.source_ann_id)
    known = {
        (source, ann_id)
        for source, ids in by_source.items()
        for ann_id in known_announcement_ids(conn, source, ids)
    }
    return [r for r in unique if (r.source, r.source_ann_id) not in known]


def insert_announcements(
    conn: Connection,
    records: Sequence[AnnouncementRecord],
    *,
    raw_doc_id: str,
    parser_version: str,
    mode: IngestMode,
    fetched_at: datetime,
    now: datetime,
) -> list[AnnouncementRecord]:
    """Insert unseen announcements and return the ones actually inserted."""
    fresh = new_announcements(conn, records)
    rows = []
    for record in fresh:
        row = asdict(record)
        row["event_ts"] = record.event_ts or fetched_at
        row.update(
            available_at=_available_at(record, mode, fetched_at),
            ingest_mode=mode,
            first_seen_at=now,
            raw_doc_id=raw_doc_id,
            parser_version=parser_version,
            attachment_status="pending" if record.attachment_url else "none",
            attachment_attempts=0,
        )
        rows.append(row)
    insert_ignore(conn, announcements, rows, ["source", "source_ann_id"])
    return fresh


def reparse_announcements(
    conn: Connection,
    records: Sequence[AnnouncementRecord],
    *,
    raw_doc_id: str,
    parser_version: str,
    mode: IngestMode,
    fetched_at: datetime,
    now: datetime,
) -> tuple[int, int]:
    """Re-apply parsed fields from a stored payload.

    Rows that originated from this payload get their parsed fields refreshed;
    ingest timestamps (``available_at``, ``first_seen_at``, ``ingest_mode``)
    are never changed. Records a buggy parser previously dropped are inserted
    with ``available_at`` derived from when the payload was fetched.
    Returns ``(updated, inserted)``.
    """
    unique = _dedupe(records)
    updated = 0
    for record in unique:
        values: dict[str, Any] = {f: getattr(record, f) for f in _ANN_PARSED_FIELDS}
        values["event_ts"] = record.event_ts or fetched_at
        values["parser_version"] = parser_version
        result = conn.execute(
            update(announcements)
            .where(
                announcements.c.source == record.source,
                announcements.c.source_ann_id == record.source_ann_id,
                announcements.c.raw_doc_id == raw_doc_id,
            )
            .values(**values)
        )
        updated += result.rowcount or 0
    inserted = insert_announcements(
        conn,
        unique,
        raw_doc_id=raw_doc_id,
        parser_version=parser_version,
        mode=mode,
        fetched_at=fetched_at,
        now=now,
    )
    # Rows whose attachment URL appeared only after the fix need fetching.
    conn.execute(
        update(announcements)
        .where(
            announcements.c.raw_doc_id == raw_doc_id,
            announcements.c.attachment_status == "none",
            announcements.c.attachment_url.is_not(None),
        )
        .values(attachment_status="pending")
    )
    return updated, len(inserted)


def pending_attachments(conn: Connection, limit: int, max_attempts: int) -> list[Row[Any]]:
    return list(
        conn.execute(
            select(
                announcements.c.id,
                announcements.c.source,
                announcements.c.attachment_url,
                announcements.c.attachment_attempts,
                announcements.c.category,
                announcements.c.subcategory,
                announcements.c.subject,
            )
            .where(
                announcements.c.attachment_status.in_(("pending", "failed")),
                announcements.c.attachment_attempts < max_attempts,
            )
            .order_by(announcements.c.event_ts.desc())
            .limit(limit)
        )
    )


def mark_attachment(
    conn: Connection,
    ann_id: int,
    *,
    status: str,
    doc_id: str | None = None,
    attempted: bool = True,
) -> None:
    """Set the attachment status. ``attempted=False`` for policy decisions
    (skipped) that made no download attempt."""
    values: dict[str, Any] = {"attachment_status": status, "attachment_doc_id": doc_id}
    if attempted:
        values["attachment_attempts"] = announcements.c.attachment_attempts + 1
    conn.execute(update(announcements).where(announcements.c.id == ann_id).values(**values))


# --- daily files ----------------------------------------------------------------

_EOD_VALUE_COLS = (
    "prev_close",
    "open",
    "high",
    "low",
    "last",
    "close",
    "avg_price",
    "volume",
    "turnover_lacs",
    "num_trades",
    "deliv_qty",
    "deliv_pct",
)


def upsert_eod(
    conn: Connection,
    records: Sequence[EodRecord],
    *,
    raw_doc_id: str,
    parser_version: str,
    available_at: datetime,
) -> None:
    rows = [
        {
            **asdict(r),
            "available_at": available_at,
            "raw_doc_id": raw_doc_id,
            "parser_version": parser_version,
        }
        for r in records
    ]
    upsert(
        conn,
        eod_prices,
        rows,
        ["trade_date", "symbol", "series"],
        [*_EOD_VALUE_COLS, "raw_doc_id", "parser_version"],
    )


def upsert_bands(
    conn: Connection,
    as_of_date: date,
    records: Sequence[BandRecord],
    *,
    raw_doc_id: str,
    parser_version: str,
    available_at: datetime,
) -> None:
    rows = [
        {
            **asdict(r),
            "as_of_date": as_of_date,
            "available_at": available_at,
            "raw_doc_id": raw_doc_id,
            "parser_version": parser_version,
        }
        for r in records
    ]
    upsert(
        conn,
        price_bands,
        rows,
        ["as_of_date", "symbol", "series"],
        ["band", "remarks", "raw_doc_id", "parser_version"],
    )


def upsert_instruments(
    conn: Connection,
    as_of_date: date,
    records: Sequence[InstrumentRecord],
    *,
    raw_doc_id: str,
    parser_version: str,
    available_at: datetime,
) -> None:
    rows = [
        {
            **asdict(r),
            "as_of_date": as_of_date,
            "available_at": available_at,
            "raw_doc_id": raw_doc_id,
            "parser_version": parser_version,
        }
        for r in records
    ]
    upsert(
        conn,
        instrument_snapshots,
        rows,
        ["as_of_date", "symbol", "series"],
        [
            "isin",
            "name",
            "listing_date",
            "face_value",
            "market_lot",
            "raw_doc_id",
            "parser_version",
        ],
    )


def has_rows_for_date(conn: Connection, table: Table, date_col: str, day: date) -> bool:
    column = table.c[date_col]
    return conn.execute(select(column).where(column == day).limit(1)).first() is not None
