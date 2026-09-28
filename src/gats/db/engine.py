"""Engine creation and schema initialisation."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event, select

from gats.db.schema import SCHEMA_VERSION, metadata, schema_meta


class SchemaVersionError(RuntimeError):
    pass


# Older versions whose upgrade only adds tables. create_all() has already
# added them, so upgrading is just recording the new version.
_ADDITIVE_UPGRADES = frozenset({1})


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
        elif int(current) in _ADDITIVE_UPGRADES and int(current) < SCHEMA_VERSION:
            conn.execute(
                schema_meta.update()
                .where(schema_meta.c.key == "schema_version")
                .values(value=str(SCHEMA_VERSION))
            )
        elif int(current) != SCHEMA_VERSION:
            raise SchemaVersionError(
                f"database schema v{current} does not match code v{SCHEMA_VERSION}; "
                "run the migration for this release before starting"
            )
