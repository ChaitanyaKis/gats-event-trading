"""Indian cash-equity trading costs from a file of verified rates (T6.1).

Why a data file: the rates change by circular (NSE revised its transaction
charges for 2021, 2023, 2024 and 2026), every value needs its source and the
date it was checked, and a backtest must say which regime it priced.

Two ways to price an order:

- ``at=None`` (the default) uses the latest regime: "what would this cost
  if traded now", the forward-looking question research asks.
- ``at=<date>`` uses the regime in force that day, for reconciling old
  contract notes. A component with no verified period covering the date
  raises :class:`UnverifiedPeriod` rather than extrapolating.

Numbers are rupees, unrounded except where a rule rounds (STT to the rupee).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

Product = Literal["delivery", "intraday"]
Side = Literal["buy", "sell"]
RATE_COMPONENTS = ("stt", "exchange_transaction", "ipft", "sebi_fee", "stamp_duty")
GST_BASE_NAMES = frozenset({"brokerage", "dp", *RATE_COMPONENTS})


class UnverifiedPeriod(ValueError):
    """No verified rate covers the requested date."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, frozen=True)


class _Period(_Strict):
    start: date = Field(alias="from")
    end: date | None = Field(default=None, alias="to")
    source: str | list[str]
    checked: date
    note: str | None = None

    @model_validator(mode="after")
    def _ordered(self) -> _Period:
        if self.end is not None and self.end < self.start:
            raise ValueError(f"period ends ({self.end}) before it starts ({self.start})")
        if not self.source:
            raise ValueError("every period needs a source")
        return self

    def covers(self, day: date) -> bool:
        return self.start <= day and (self.end is None or day <= self.end)


class SideRates(_Strict):
    buy: float = Field(ge=0, le=0.01)
    sell: float = Field(ge=0, le=0.01)


class RatePeriod(_Period):
    delivery: SideRates
    intraday: SideRates
    round_to: float | None = Field(default=None, gt=0)


class BrokerageRule(_Strict):
    per_order: float = Field(ge=0)
    cap_fraction: float | None = Field(default=None, gt=0, le=0.05)


class BrokeragePeriod(_Period):
    delivery: BrokerageRule
    intraday: BrokerageRule


class DpPeriod(_Period):
    delivery_sell_per_scrip_day: float = Field(ge=0)


class GstPeriod(_Period):
    rate: float = Field(ge=0, le=0.5)
    base: dict[Product, list[str]]

    @model_validator(mode="after")
    def _known_base(self) -> GstPeriod:
        for product, names in self.base.items():
            unknown = set(names) - GST_BASE_NAMES
            if unknown:
                raise ValueError(f"GST base for {product}: unknown components {sorted(unknown)}")
        return self


def _check_timeline(name: str, periods: list[Any]) -> None:
    if not periods:
        raise ValueError(f"{name}: no periods")
    for before, after in pairwise(periods):
        if before.end is None or after.start <= before.end:
            raise ValueError(f"{name}: periods overlap or are out of order at {after.start}")


class Components(_Strict):
    brokerage: list[BrokeragePeriod]
    stt: list[RatePeriod]
    exchange_transaction: list[RatePeriod]
    ipft: list[RatePeriod]
    sebi_fee: list[RatePeriod]
    stamp_duty: list[RatePeriod]
    dp: list[DpPeriod]
    gst: list[GstPeriod]

    @model_validator(mode="after")
    def _timelines(self) -> Components:
        for name in type(self).model_fields:
            _check_timeline(name, getattr(self, name))
        return self


class CostFile(_Strict):
    version: str
    broker: str
    exchange: str
    components: Components


@dataclass(frozen=True)
class Fill:
    """One executed order, as the cost rules see it."""

    side: Side
    product: Product
    price: float
    quantity: int
    day: date
    # Depository charges are per scrip per day: only the day's first
    # delivery sell of a scrip pays them (the ledger knows which that is).
    dp_applies: bool = True

    @property
    def value(self) -> float:
        return self.price * self.quantity


@dataclass(frozen=True)
class Charges:
    brokerage: float
    stt: float
    exchange_transaction: float
    ipft: float
    sebi_fee: float
    stamp_duty: float
    dp: float
    gst: float

    @property
    def total(self) -> float:
        return float(sum(getattr(self, f.name) for f in fields(self)))


def _round(value: float, step: float) -> float:
    """Half-up rounding to ``step`` (Python's round() rounds half to even)."""
    units = (Decimal(str(value)) / Decimal(str(step))).quantize(Decimal(1), ROUND_HALF_UP)
    return float(units * Decimal(str(step)))


class CostModel:
    def __init__(self, spec: CostFile, version: str) -> None:
        self.spec = spec
        self.version = version

    @classmethod
    def load(cls, path: Path) -> CostModel:
        raw = path.read_bytes()
        spec = CostFile.model_validate(yaml.safe_load(raw))
        digest = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()[:8]
        return cls(spec, f"{spec.version}+{digest}")

    def _period(self, name: str, at: date | None) -> Any:
        periods: list[Any] = getattr(self.spec.components, name)
        if at is None:
            return periods[-1]
        for period in periods:
            if period.covers(at):
                return period
        raise UnverifiedPeriod(
            f"{name}: no verified rate for {at} (verified from {periods[0].start})"
        )

    def rate(self, name: str, product: Product, side: Side, at: date | None = None) -> float:
        """A percentage component's rate (fraction of traded value)."""
        period: RatePeriod = self._period(name, at)
        rates: SideRates = getattr(period, product)
        return float(getattr(rates, side))

    def earliest_verified(self) -> date:
        """The first day on which every component has a verified rate."""
        starts: list[date] = [
            getattr(self.spec.components, name)[0].start for name in Components.model_fields
        ]
        return max(starts)

    def charges(self, fill: Fill, *, at: date | None = None) -> Charges:
        value = fill.value
        parts: dict[str, float] = {}
        rule: BrokerageRule = getattr(self._period("brokerage", at), fill.product)
        brokerage = rule.per_order
        if rule.cap_fraction is not None:
            brokerage = min(brokerage, value * rule.cap_fraction)
        parts["brokerage"] = brokerage
        for name in RATE_COMPONENTS:
            period: RatePeriod = self._period(name, at)
            amount = value * float(getattr(getattr(period, fill.product), fill.side))
            parts[name] = _round(amount, period.round_to) if period.round_to else amount
        dp: DpPeriod = self._period("dp", at)
        delivery_sell = fill.product == "delivery" and fill.side == "sell"
        parts["dp"] = dp.delivery_sell_per_scrip_day if delivery_sell and fill.dp_applies else 0.0
        gst: GstPeriod = self._period("gst", at)
        parts["gst"] = gst.rate * sum(parts[name] for name in gst.base[fill.product])
        return Charges(**parts)
