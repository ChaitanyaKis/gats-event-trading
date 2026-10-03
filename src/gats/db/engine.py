"""Engine creation and schema initialisation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event, inspect, select, text, update

from gats.db.schema import SCHEMA_VERSION, backfill_days, metadata, schema_meta


class SchemaVersionError(RuntimeError):
    pass


def _add_column_if_missing(conn: Connection, table: str, column: str, ddl_type: str) -> None:
    """Additive column change. create_all() never alters existing tables, and a
    fresh database already has the column, so the check makes it idempotent."""
    if column not in {c["name"] for c in inspect(conn).get_columns(table)}:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _v1_to_v2(conn: Connection) -> None:
    """v2 only added `backfill_days`, which create_all() has already made."""


def _v2_to_v3(conn: Connection) -> None:
    _add_column_if_missing(conn, "backfill_days", "expected_records", "INTEGER")
    # 0.1.2 read the page count from TotalPageCnt, which BSE omits for past
    # days, so such days were marked complete after one full page (50 rows).
    # Reopen them; the reconcile job and backfills refetch them in full.
    conn.execute(
        update(backfill_days)
        .where(
            backfill_days.c.source == "BSE",
            backfill_days.c.status == "complete",
            backfill_days.c.n_records == 50,
        )
        .values(status="incomplete", attempts=0, last_error="reopened by v3 upgrade")
    )


def _v3_to_v4(conn: Connection) -> None:
    """v4 only added reference-data tables, which create_all() has made."""


def _v4_to_v5(conn: Connection) -> None:
    # Filled by `gats reparse bse_ann` / `nse_ann` (parsers bse-ann-v2, nse-ann-v3).
    _add_column_if_missing(conn, "announcements", "attachment_size", "BIGINT")


def _v5_to_v6(conn: Connection) -> None:
    """v6 only added tables (event types, attachment texts, LLM calls,
    extracted facts), which create_all() has made."""


def _v6_to_v7(conn: Connection) -> None:
    """v7 only added `bar_months`, which create_all() has made."""


# Upgrade steps keyed by the version they start from. Each must be additive
# and safe to run on a database that create_all() has just touched.
_UPGRADES: dict[int, Callable[[Connection], None]] = {
    1: _v1_to_v2,
    2: _v2_to_v3,
    3: _v3_to_v4,
    4: _v4_to_v5,
    5: _v5_to_v6,
    6: _v6_to_v7,
}


def make_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            # WAL lets `gats status` read while the recorder writes.
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=10000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create tables if missing and check the schema version."""
    metadata.create_all(engine)
    with engine.begin() as conn:
        current: str | None = conn.execute(
            select(schema_meta.c.value).where(schema_meta.c.key == "schema_version")
        ).scalar_one_or_none()
        if current is None:
            conn.execute(
                schema_meta.insert().values(key="schema_version", value=str(SCHEMA_VERSION))
            )
            return
        version = int(current)
        if version > SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema v{version} is newer than this code (v{SCHEMA_VERSION}); "
                "upgrade GATS before starting"
            )
        if version == SCHEMA_VERSION:
            return
        while version < SCHEMA_VERSION:
            step = _UPGRADES.get(version)
            if step is None:
                raise SchemaVersionError(f"no upgrade path from schema v{version}")
            step(conn)
            version += 1
        conn.execute(
            schema_meta.update()
            .where(schema_meta.c.key == "schema_version")
            .values(value=str(SCHEMA_VERSION))
        )
