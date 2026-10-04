"""Hard caps on real money (T8.3).

The risk engine's limits are part of the strategy's design and scale with
equity. These caps are different: absolute rupee amounts the human chose
for the pilot, checked on every real order after the risk engine has
already agreed. A breach is not a refusal to shrug off: it means the system
tried to do something the human said it must never do, so live trading
switches itself off (the kill switch file) and says so.

Exits pass the size caps (getting out must stay possible) but still count
against the daily order cap's hard stop, which is set far above normal use.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class LiveCaps(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_capital_rs: float = Field(ge=0)
    max_daily_loss_rs: float = Field(ge=0)
    max_position_rs: float = Field(ge=0)
    max_orders_per_day: int = Field(ge=0)

    def unset(self) -> list[str]:
        """Caps still at 0: the human has not decided them."""
        return [name for name, value in self.model_dump().items() if not value]

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class LiveSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    paper: Path
    caps: LiveCaps
    approval_valid_days: int = Field(ge=1, le=90)
    # The broker's status words that end an order -> our state. Only words
    # read in the broker's documentation belong here.
    order_end_statuses: dict[str, str] = Field(default_factory=lambda: {"complete": "filled"})


def load_live(path: Path) -> LiveSpec:
    return LiveSpec.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


@dataclass(frozen=True)
class Account:
    """The real account as the order layer knows it when an order is made."""

    deployed_rs: float  # cost of open positions plus working buy orders
    in_stock_rs: float  # the same, in the order's stock
    day_loss_rs: float  # today's realised plus unrealised loss (positive = losing)
    orders_today: int


def breach(caps: LiveCaps, notional_rs: float, closes: bool, account: Account) -> str | None:
    """The cap this order would break, or None. Checked in a fixed order so
    a breach always names one cap."""
    if account.orders_today >= caps.max_orders_per_day:
        return (
            f"order count: {account.orders_today} real orders today (cap {caps.max_orders_per_day})"
        )
    if closes:
        return None
    if account.day_loss_rs >= caps.max_daily_loss_rs:
        return f"daily loss: down Rs {account.day_loss_rs:,.0f} (cap {caps.max_daily_loss_rs:,.0f})"
    if account.in_stock_rs + notional_rs > caps.max_position_rs:
        total = account.in_stock_rs + notional_rs
        return f"position size: Rs {total:,.0f} in one stock (cap {caps.max_position_rs:,.0f})"
    if account.deployed_rs + notional_rs > caps.max_capital_rs:
        total = account.deployed_rs + notional_rs
        return f"capital: Rs {total:,.0f} deployed (cap {caps.max_capital_rs:,.0f})"
    return None
