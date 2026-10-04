"""Live one-minute bars for the paper runtime (T7.1).

The broker's intraday-candle API returns the current day's candles. The
runtime asks for a stock once a minute while it has a reason to watch it,
and keeps only candles whose minute had fully ended, with a margin, when
the question was *sent*: the docs do not say whether the candle still
forming is in the reply, and a half-built bar must never reach a strategy
as if it were final.

Raw first, as everywhere: each reply is stored before it is parsed. The
token travels only in the ``Authorization`` header.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from gats.db import repo
from gats.ingest import Services
from gats.marketdata.upstox import auth_headers
from gats.net import FetchError
from gats.sources import upstox
from gats.sources.models import PayloadError
from gats.sources.upstox import Bar

MINUTE = timedelta(minutes=1)


@dataclass(frozen=True)
class Candles:
    """One reply: its bars oldest first, and when the question was sent."""

    instrument_key: str
    asked_at: datetime
    bars: list[Bar] = field(default_factory=list)
    error: str | None = None

    def finished(self, margin_s: float) -> list[Bar]:
        """Bars whose minute had ended ``margin_s`` before the question was
        sent: only those are certain to be complete."""
        latest = self.asked_at - timedelta(seconds=margin_s)
        return [bar for bar in self.bars if bar.ts + MINUTE <= latest]


async def _candles(svc: Services, instrument_key: str, url: str, kind: str) -> Candles:
    headers = auth_headers(svc)
    asked_at = svc.clock()
    try:
        got = await svc.client.get(url, headers=headers)
    except FetchError as exc:
        return Candles(instrument_key, asked_at, error=str(exc)[:300])
    with svc.engine.begin() as conn:
        repo.save_raw(
            conn,
            svc.store,
            got.content,
            kind=kind,
            source=upstox.SOURCE,
            url=got.url,
            content_type=got.content_type,
            fetched_at=got.fetched_at,
            meta={"instrument_key": instrument_key, "http_status": got.status},
        )
    if not got.ok:
        return Candles(instrument_key, asked_at, error=f"HTTP {got.status}: {got.content[:200]!r}")
    try:
        parsed = upstox.parse_candles(got.content, instrument_key=instrument_key)
    except PayloadError as exc:
        return Candles(instrument_key, asked_at, error=str(exc)[:300])
    return Candles(instrument_key, asked_at, parsed.records)


async def fetch_intraday(svc: Services, instrument_key: str) -> Candles:
    """The current day's one-minute candles of one stock."""
    url = upstox.intraday_url(svc.settings.upstox_api_base, instrument_key)
    return await _candles(svc, instrument_key, url, upstox.INTRADAY_KIND)


async def fetch_session(svc: Services, instrument_key: str, day: date) -> Candles:
    """One past session's one-minute candles (for the previous close)."""
    url = upstox.candles_url(svc.settings.upstox_api_base, instrument_key, "minutes", 1, day, day)
    return await _candles(svc, instrument_key, url, upstox.CANDLES_KIND)
