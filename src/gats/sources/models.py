"""Parsed record types shared by source parsers and the repository."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Generic, TypeVar

T = TypeVar("T")


class PayloadError(ValueError):
    """The payload is not in the expected shape (e.g. an HTML block page instead of JSON)."""


@dataclass(frozen=True, slots=True)
class AnnouncementRecord:
    source: str
    source_ann_id: str
    symbol: str | None
    scrip_code: str | None
    isin: str | None
    company_name: str | None
    category: str | None
    subcategory: str | None
    subject: str | None
    details: str | None
    attachment_url: str | None
    exch_submitted_ts: datetime | None
    exch_disseminated_ts: datetime | None
    event_ts: datetime | None
    # Attachment size in bytes as the exchange reports it (BSE exact; NSE
    # rounded for display). Lets the same PDF be recognised on both exchanges.
    attachment_size: int | None = None


@dataclass(frozen=True, slots=True)
class EodRecord:
    trade_date: date
    symbol: str
    series: str
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    last: float | None
    close: float | None
    avg_price: float | None
    volume: int | None
    turnover_lacs: float | None
    num_trades: int | None
    deliv_qty: int | None
    deliv_pct: float | None


@dataclass(frozen=True, slots=True)
class BandRecord:
    symbol: str
    series: str
    band: str | None
    remarks: str | None


@dataclass(frozen=True, slots=True)
class InstrumentRecord:
    symbol: str
    series: str
    isin: str | None
    name: str | None
    listing_date: date | None
    face_value: float | None
    market_lot: int | None


@dataclass(slots=True)
class ParseResult(Generic[T]):
    records: list[T]
    warnings: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
