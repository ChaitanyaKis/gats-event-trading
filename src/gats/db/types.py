"""Column types."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator[datetime]):
    """Aware-UTC datetime column.

    Binding a naive datetime raises: silently guessing a timezone is how
    point-in-time bugs get into trading datasets. SQLite has no timezone
    support, so values are stored there as naive UTC and re-tagged on read.
    Postgres stores ``timestamptz`` natively.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"naive datetime bound to UTCDateTime column: {value!r}")
        as_utc = value.astimezone(UTC)
        return as_utc if dialect.name == "postgresql" else as_utc.replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime from database, got {type(value).__name__}")
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
