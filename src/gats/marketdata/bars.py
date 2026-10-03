"""One-minute bars on disk: Parquet per instrument and month, read with DuckDB.

Why files and not the database: event windows for a few thousand filings
are tens of millions of rows. Parquet stores them compactly by column and
DuckDB scans them quickly, while SQLite stays small and fast for records.

Layout: ``<bars_dir>/1m/<segment>/<id>/<YYYY-MM>.parquet``, where ``id`` is
the ISIN (or the index name) from the instrument key. A month is one file
because the broker returns at most a month per request. Writing a month
merges with the bars already there (a re-fetch never loses data) and
replaces the file atomically, so a crash leaves the old file or the new
one, never half of one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from gats.sources.upstox import Bar
from gats.timeutil import ensure_aware

INTERVAL = "1m"
_UNSAFE = re.compile(r"[^A-Za-z0-9.-]+")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MICROSECOND = timedelta(microseconds=1)
_COLUMNS = "instrument_key, epoch_us(ts), open, high, low, close, volume, open_interest"


# Times cross into and out of Parquet/DuckDB as integer microseconds since
# the epoch (UTC). Letting pyarrow or DuckDB build timezone-aware Python
# datetimes needs a timezone database (tzdata, pytz) that Windows lacks.
def _to_us(ts: datetime) -> int:
    return (ensure_aware(ts) - _EPOCH) // _MICROSECOND


def _from_us(us: int) -> datetime:
    return _EPOCH + timedelta(microseconds=us)


def month_start(day: date) -> date:
    return day.replace(day=1)


def months(start: date, end: date) -> list[date]:
    """First days of every month from ``start``'s to ``end``'s, inclusive."""
    out = []
    current = month_start(start)
    while current <= end:
        out.append(current)
        current = date(current.year + current.month // 12, current.month % 12 + 1, 1)
    return out


def instrument_dir(root: Path, instrument_key: str) -> Path:
    segment, _, ident = instrument_key.partition("|")
    if not segment or not ident:
        raise ValueError(f"not an instrument key: {instrument_key!r}")
    return root / INTERVAL / _UNSAFE.sub("_", segment) / _UNSAFE.sub("_", ident)


def month_path(root: Path, instrument_key: str, month: date) -> Path:
    return instrument_dir(root, instrument_key) / f"{month:%Y-%m}.parquet"


def _table(bars: Sequence[Bar]) -> Any:
    import pyarrow as pa

    return pa.table(
        {
            "instrument_key": pa.array([b.instrument_key for b in bars], pa.string()),
            "ts": pa.array([_to_us(b.ts) for b in bars], pa.int64()).cast(
                pa.timestamp("us", tz="UTC")
            ),
            "open": pa.array([b.open for b in bars], pa.float64()),
            "high": pa.array([b.high for b in bars], pa.float64()),
            "low": pa.array([b.low for b in bars], pa.float64()),
            "close": pa.array([b.close for b in bars], pa.float64()),
            "volume": pa.array([b.volume for b in bars], pa.int64()),
            "open_interest": pa.array([b.open_interest for b in bars], pa.int64()),
        }
    )


def _from_rows(rows: Iterable[tuple[Any, ...]]) -> list[Bar]:
    """Rows with the time as epoch microseconds."""
    return [
        Bar(key, _from_us(int(us)), o, h, low, c, int(v), int(oi))
        for key, us, o, h, low, c, v, oi in rows
    ]


def read_month(root: Path, instrument_key: str, month: date) -> list[Bar]:
    import pyarrow as pa
    import pyarrow.parquet as pq

    path = month_path(root, instrument_key, month)
    if not path.exists():
        return []
    table = pq.read_table(path)
    columns = [
        table.column(name).cast(pa.int64()) if name == "ts" else table.column(name)
        for name in (
            "instrument_key",
            "ts",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "open_interest",
        )
    ]
    return _from_rows(zip(*(column.to_pylist() for column in columns), strict=True))


def write_month(root: Path, instrument_key: str, month: date, bars: Sequence[Bar]) -> int:
    """Merge ``bars`` into the month's file (new bars win on equal times);
    returns the bars in the file."""
    import pyarrow.parquet as pq

    if any(b.instrument_key != instrument_key or month_start(b.ts.date()) != month for b in bars):
        # A bar's UTC date can differ from its IST date only outside market
        # hours, which never happens for 09:15-15:30 IST sessions.
        raise ValueError(f"bars outside {instrument_key} {month:%Y-%m}")
    merged = {b.ts: b for b in read_month(root, instrument_key, month)}
    merged.update({b.ts: b for b in bars})
    ordered = [merged[ts] for ts in sorted(merged)]
    path = month_path(root, instrument_key, month)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".parquet.tmp")
    pq.write_table(_table(ordered), tmp, compression="zstd")
    os.replace(tmp, path)
    return len(ordered)


@dataclass(frozen=True)
class DaySummary:
    bars: int
    first: datetime
    last: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def read_bars(root: Path, instrument_key: str, start: datetime, end: datetime) -> list[Bar]:
    """Bars with ``start <= ts < end`` (UTC-aware bounds), via DuckDB."""
    import duckdb

    start, end = ensure_aware(start), ensure_aware(end)
    files = [
        month_path(root, instrument_key, m).as_posix()
        for m in months(start.date(), end.date())
        if month_path(root, instrument_key, m).exists()
    ]
    if not files:
        return []
    with duckdb.connect() as con:
        rows = con.execute(
            f"SELECT {_COLUMNS} FROM read_parquet(?) "
            "WHERE epoch_us(ts) >= ? AND epoch_us(ts) < ? ORDER BY ts",
            [files, _to_us(start), _to_us(end)],
        ).fetchall()
    return _from_rows(rows)


def summarize(bars: Sequence[Bar]) -> DaySummary | None:
    if not bars:
        return None
    return DaySummary(
        bars=len(bars),
        first=bars[0].ts,
        last=bars[-1].ts,
        open=bars[0].open,
        high=max(b.high for b in bars),
        low=min(b.low for b in bars),
        close=bars[-1].close,
        volume=sum(b.volume for b in bars),
    )


def eod_check(
    summary: DaySummary,
    *,
    open: float | None,
    high: float | None,
    low: float | None,
    volume: int | None,
) -> list[tuple[str, float | None, float | None, bool]]:
    """(field, from bars, from the exchange's EOD file, agrees) for a day.

    Complete bars should reproduce the day's open, high and low exactly.
    Volume agrees within 2%, because whether the broker's bars include every
    session (pre-open, closing) is not documented. The close is not
    compared: the exchange's official close need not be the last trade.
    """

    def same(a: float | None, b: float | None) -> bool:
        return a is not None and b is not None and abs(a - b) <= 0.005 + 1e-9 * abs(b)

    checks: list[tuple[str, float | None, float | None, bool]] = [
        ("open", summary.open, open, same(summary.open, open)),
        ("high", summary.high, high, same(summary.high, high)),
        ("low", summary.low, low, same(summary.low, low)),
    ]
    near = volume is not None and volume > 0 and abs(summary.volume - volume) <= 0.02 * volume
    checks.append(
        ("volume", float(summary.volume), None if volume is None else float(volume), near)
    )
    return checks
