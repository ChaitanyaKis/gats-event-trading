"""Fetching one-minute bars from Upstox, a month at a time (M5).

Raw first, like every source: each reply goes to the raw store before it is
parsed, so a parser fix can be replayed. ``bar_months`` records what is
done, so an interrupted fetch resumes where it stopped and a finished month
is never fetched again; the current month stays ``partial`` until it ends.

The Analytics Token travels only in the ``Authorization`` header: never in
a URL, a log line or a stored error message.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

from sqlalchemy import Connection, select

from gats.db import repo
from gats.db.schema import bar_months, instrument_snapshots
from gats.ingest import Services
from gats.marketdata.bars import INTERVAL, write_month
from gats.net import FetchError
from gats.sources import upstox
from gats.sources.models import PayloadError
from gats.timeutil import ist_today

FIRST_MINUTE_DATA = date(2022, 1, 1)  # Upstox docs, checked 2026-10-03


class TokenMissing(RuntimeError):
    """Candles need the Analytics Token (a HUMAN step)."""


def auth_headers(svc: Services) -> dict[str, str]:
    token = svc.settings.upstox_analytics_token
    if token is None or not token.get_secret_value().strip():
        raise TokenMissing(
            "GATS_UPSTOX_ANALYTICS_TOKEN is not set. Generate the read-only Analytics Token "
            "at https://account.upstox.com/developer/apps#analytics and add it to .env."
        )
    return {
        "Accept": "application/json",
        "Authorization": f"Bearer {token.get_secret_value().strip()}",
    }


@dataclass(frozen=True)
class MonthResult:
    instrument_key: str
    month: date
    status: str  # complete | partial | failed | skipped
    bars: int = 0
    error: str | None = None


def month_status(conn: Connection, instrument_key: str, month: date) -> str | None:
    row = conn.execute(
        select(bar_months.c.status).where(
            bar_months.c.instrument_key == instrument_key,
            bar_months.c.month == month,
            bar_months.c.interval == INTERVAL,
        )
    ).first()
    return None if row is None else str(row.status)


def _record(
    svc: Services,
    month_row: MonthResult,
    raw_doc_id: str | None,
) -> None:
    with svc.engine.begin() as conn:
        attempts = conn.execute(
            select(bar_months.c.attempts).where(
                bar_months.c.instrument_key == month_row.instrument_key,
                bar_months.c.month == month_row.month,
                bar_months.c.interval == INTERVAL,
            )
        ).scalar()
        repo.upsert(
            conn,
            bar_months,
            [
                {
                    "instrument_key": month_row.instrument_key,
                    "month": month_row.month,
                    "interval": INTERVAL,
                    "status": month_row.status,
                    "n_bars": month_row.bars,
                    "attempts": (attempts or 0) + 1,
                    "raw_doc_id": raw_doc_id,
                    "last_error": month_row.error,
                    "updated_at": svc.clock(),
                }
            ],
            ["instrument_key", "month", "interval"],
            ["status", "n_bars", "attempts", "raw_doc_id", "last_error", "updated_at"],
        )


async def fetch_month(
    svc: Services, instrument_key: str, month: date, *, force: bool = False
) -> MonthResult:
    """Fetch, store and record one instrument-month of one-minute bars."""
    if month < FIRST_MINUTE_DATA:
        return MonthResult(instrument_key, month, "skipped", error="before January 2022")
    today = ist_today(svc.clock())
    if month > today:
        return MonthResult(instrument_key, month, "skipped", error="in the future")
    if not force:
        with svc.engine.begin() as conn:
            if month_status(conn, instrument_key, month) == "complete":
                return MonthResult(instrument_key, month, "skipped", error="already complete")
    headers = auth_headers(svc)
    last_day = date(month.year, month.month, calendar.monthrange(month.year, month.month)[1])
    to_date = min(last_day, today)
    url = upstox.candles_url(
        svc.settings.upstox_api_base, instrument_key, "minutes", 1, to_date, month
    )
    try:
        got = await svc.client.get(url, headers=headers)
    except FetchError as exc:
        failed = MonthResult(instrument_key, month, "failed", error=str(exc)[:300])
        _record(svc, failed, None)
        return failed
    with svc.engine.begin() as conn:
        doc_id = repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=upstox.CANDLES_KIND,
            source=upstox.SOURCE,
            url=got.url,
            content_type=got.content_type,
            fetched_at=got.fetched_at,
            meta={
                "instrument_key": instrument_key,
                "from": month.isoformat(),
                "to": to_date.isoformat(),
                "http_status": got.status,
            },
        )
    if not got.ok:
        failed = MonthResult(
            instrument_key, month, "failed", error=f"HTTP {got.status}: {got.content[:200]!r}"
        )
        _record(svc, failed, doc_id)
        return failed
    try:
        parsed = upstox.parse_candles(got.content, instrument_key=instrument_key)
    except PayloadError as exc:
        failed = MonthResult(instrument_key, month, "failed", error=str(exc)[:300])
        _record(svc, failed, doc_id)
        return failed
    stored = write_month(svc.settings.bars_dir, instrument_key, month, parsed.records)
    status = "complete" if to_date == last_day and to_date < today else "partial"
    done = MonthResult(instrument_key, month, status, bars=stored)
    _record(svc, done, doc_id)
    return done


def key_for_symbol(conn: Connection, symbol: str) -> str | None:
    """The instrument key for an NSE symbol as of the latest instrument
    snapshot (a current symbol; research code goes through the master)."""
    row = conn.execute(
        select(instrument_snapshots.c.isin)
        .where(
            instrument_snapshots.c.symbol == symbol.upper(),
            instrument_snapshots.c.isin.is_not(None),
        )
        .order_by(instrument_snapshots.c.as_of_date.desc(), instrument_snapshots.c.series)
        .limit(1)
    ).first()
    return None if row is None else upstox.equity_key(str(row.isin))
