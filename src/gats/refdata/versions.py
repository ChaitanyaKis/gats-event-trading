"""Type-2 versioning for current-only reference files.

Many reference sources (BSE's scrip list, surveillance lists) publish only
"today". Applying each day's snapshot here keeps one row per *version* of an
entity: ``[valid_from, valid_to)`` in snapshot dates, ``valid_to`` NULL for
the current version. A daily full copy would be simpler but grows by the
whole list every day.

Application is idempotent and replayable in date order (``gats reparse``):
re-applying a snapshot that produced a version updates that version in
place instead of creating a duplicate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy import Connection, Table, and_, bindparam, or_, select, update

from gats.db import repo
from gats.db.schema import refdata_snapshots

# A snapshot smaller than this share of the open versions is treated as
# truncated: its missing entities are not closed (they may still exist).
MIN_SHARE_FOR_REMOVALS = 0.95


@dataclass
class ApplyStats:
    inserted: int = 0  # entity seen for the first time
    changed: int = 0  # attributes changed: old version closed, new one opened
    rewritten: int = 0  # same-day version updated in place (reparse / parser fix)
    closed: int = 0  # entity no longer listed
    unchanged: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def n_changes(self) -> int:
        return self.inserted + self.changed + self.rewritten + self.closed

    @property
    def n_records(self) -> int:
        """Entities in the snapshot (closed ones are, by definition, absent)."""
        return self.inserted + self.changed + self.rewritten + self.unchanged

    def summary(self) -> dict[str, int]:
        return {
            "inserted": self.inserted,
            "changed": self.changed,
            "rewritten": self.rewritten,
            "closed": self.closed,
        }


def log_snapshot(
    conn: Connection,
    *,
    kind: str,
    as_of: date,
    n_records: int,
    n_changes: int,
    available_at: datetime,
    raw_doc_id: str,
    parser_version: str,
) -> None:
    """Record that ``kind`` was applied for ``as_of`` (one fetch per day)."""
    repo.upsert(
        conn,
        refdata_snapshots,
        [
            {
                "kind": kind,
                "as_of_date": as_of,
                "n_records": n_records,
                "n_changes": n_changes,
                "available_at": available_at,
                "raw_doc_id": raw_doc_id,
                "parser_version": parser_version,
            }
        ],
        ["kind", "as_of_date"],
        ["n_records", "n_changes", "raw_doc_id", "parser_version"],
    )


def apply_snapshot(
    conn: Connection,
    table: Table,
    *,
    kind: str,
    key: str,
    attrs: Sequence[str],
    as_of: date,
    records: Sequence[Mapping[str, Any]],
    available_at: datetime,
    raw_doc_id: str,
    parser_version: str,
) -> ApplyStats:
    """Apply one snapshot taken on ``as_of`` and log it in ``refdata_snapshots``."""
    stats = ApplyStats()
    key_col = table.c[key]
    rows = conn.execute(
        select(table).where(or_(table.c.valid_to.is_(None), table.c.valid_to > as_of))
    ).all()
    covering: dict[str, Any] = {}
    next_from: dict[str, date] = {}
    for row in rows:
        k = row._mapping[key]
        if row.valid_from <= as_of:
            covering[k] = row
        else:
            next_from[k] = min(row.valid_from, next_from.get(k, row.valid_from))

    to_insert: list[dict[str, Any]] = []
    to_close: list[dict[str, Any]] = []
    meta = {
        "available_at": available_at,
        "raw_doc_id": raw_doc_id,
        "parser_version": parser_version,
    }
    seen: set[str] = set()
    for record in records:
        k = record[key]
        seen.add(k)
        values = {a: record.get(a) for a in attrs}
        current = covering.get(k)
        if current is None:
            stats.inserted += 1
            to_insert.append(
                {key: k, "valid_from": as_of, "valid_to": next_from.get(k), **values, **meta}
            )
        elif all(current._mapping[a] == values[a] for a in attrs):
            stats.unchanged += 1
        elif current.valid_from == as_of:
            stats.rewritten += 1
            conn.execute(
                update(table)
                .where(key_col == k, table.c.valid_from == as_of)
                .values(**values, raw_doc_id=raw_doc_id, parser_version=parser_version)
            )
        else:
            stats.changed += 1
            to_close.append({"k": k, "vf": current.valid_from})
            to_insert.append(
                {key: k, "valid_from": as_of, "valid_to": current.valid_to, **values, **meta}
            )

    missing = [k for k in covering if k not in seen]
    if missing:
        if len(seen) >= MIN_SHARE_FOR_REMOVALS * len(covering):
            stats.closed = len(missing)
            to_close.extend({"k": k, "vf": covering[k].valid_from} for k in missing)
        else:
            stats.warnings.append(
                f"{kind} {as_of}: snapshot has {len(seen)} entities vs {len(covering)} "
                "open versions; looks truncated, so none were closed"
            )

    if to_close:
        conn.execute(
            update(table)
            .where(and_(key_col == bindparam("k"), table.c.valid_from == bindparam("vf")))
            .values(valid_to=as_of),
            to_close,
        )
    repo.insert_ignore(conn, table, to_insert, [key, "valid_from"])
    log_snapshot(
        conn,
        kind=kind,
        as_of=as_of,
        n_records=len(records),
        n_changes=stats.n_changes,
        available_at=available_at,
        raw_doc_id=raw_doc_id,
        parser_version=parser_version,
    )
    return stats


def has_snapshot(conn: Connection, kind: str, as_of: date) -> bool:
    return (
        conn.execute(
            select(refdata_snapshots.c.kind).where(
                refdata_snapshots.c.kind == kind, refdata_snapshots.c.as_of_date == as_of
            )
        ).first()
        is not None
    )
