"""Risk rules: every order passes them or is refused, with the reason (T6.4).

The engine sees two neutral snapshots, the order (:class:`OrderIntent`) and
the account (:class:`Exposure`), so the backtest, paper and live runtimes
can all feed it. Rules are checked in a fixed order and the first refusal
wins, so a refusal always names one reason.

Exits reduce risk, so they pass every rule except the exchange's order
rate: halting exits because the day went badly would only make it worse.

Two rules are kill criteria rather than limits (DESIGN, Gates): a fall from
the equity peak beyond the drawdown limit, and a run of trades that lost
beyond what chance explains. Either one disables new entries until a person
has looked; nothing in the code switches a strategy back on by itself.
Missing information fails closed: an unknown liquidity is a refusal, and so
is unknown surveillance status unless the configuration allows it (old
backtests have no surveillance history).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from gats.strategy.base import Product, Side
from gats.tdist import t_sf
from gats.timeutil import to_ist

Liquidity = Callable[[str, date], float | None]  # median daily turnover, rupees
Flags = Callable[[str, date], frozenset[str] | None]  # e.g. {"ASM"}; None = unknown
Halted = Callable[[], bool]  # is the kill switch on?


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    max_position_pct_equity: float = Field(gt=0, le=1)
    max_open_positions: int = Field(ge=1)
    max_daily_loss_pct_equity: float = Field(gt=0, le=1)
    max_drawdown_pct_equity: float = Field(gt=0, le=1)
    max_symbol_notional: float = Field(gt=0)
    min_median_turnover_rs: float = Field(ge=0)
    block_flags: frozenset[str] = frozenset()
    unknown_flags: Literal["allow", "refuse"] = "refuse"
    max_orders_per_second: int = Field(ge=1, le=10)  # 10: the exchange threshold
    expectancy_window_trades: int = Field(ge=5)
    expectancy_confidence: float = Field(gt=0.5, lt=1)
    max_data_age_s: float = Field(gt=0)
    kill_switch_file: Path | None = None


@dataclass(frozen=True)
class OrderIntent:
    instrument_key: str
    side: Side
    product: Product
    quantity: int
    price: float  # the order's reference or limit price
    closes: bool  # reduces an existing position
    at: datetime

    @property
    def notional(self) -> float:
        return self.quantity * self.price


@dataclass(frozen=True)
class Exposure:
    """The account as the runtime sees it when the order is made."""

    equity: float
    day_start_equity: float
    positions: dict[str, float] = field(default_factory=dict)  # key -> notional held
    buying: dict[str, float] = field(default_factory=dict)  # key -> notional in working buys
    last_data_at: dict[str, datetime] = field(default_factory=dict)
    orders_this_second: int = 0
    in_session: bool = True
    peak_equity: float | None = None  # the highest equity so far (None: not tracked)
    recent_nets: tuple[float, ...] = ()  # net rupees of the latest closed trades, oldest first


def losing_beyond_chance(nets: Sequence[float], confidence: float) -> bool:
    """Is the whole confidence interval of the mean result below zero?
    (A one-sample t interval: trades are few, and their spread is unknown.)"""
    n = len(nets)
    mean = sum(nets) / n
    if mean >= 0 or n < 2:
        return False
    spread = math.sqrt(sum((x - mean) ** 2 for x in nets) / (n - 1))
    if spread == 0:
        return True  # every trade lost the same amount
    t = mean / (spread / math.sqrt(n))
    return t_sf(-t, n - 1) < (1 - confidence) / 2


class RiskEngine:
    def __init__(
        self,
        limits: RiskLimits,
        version: str,
        *,
        liquidity: Liquidity | None = None,
        flags: Flags | None = None,
        halted: Halted | None = None,
        root: Path = Path(),
    ) -> None:
        self.limits = limits
        self.version = version
        self.liquidity = liquidity
        self.flags = flags
        self.halted = halted or self.kill_switch_on
        self.root = root

    def kill_switch_on(self) -> bool:
        """The kill switch is a file, so that stopping new entries needs no
        working software: creating it by hand is enough."""
        switch = self.limits.kill_switch_file
        return switch is not None and (self.root / switch).exists()

    @classmethod
    def load(cls, path: Path, **lookups: Liquidity | Flags | Halted | None) -> RiskEngine:
        raw = path.read_bytes()
        limits = RiskLimits.model_validate(yaml.safe_load(raw))
        digest = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()[:8]
        return cls(limits, f"{limits.version}+{digest}", **lookups)  # type: ignore[arg-type]

    def check(self, order: OrderIntent, account: Exposure) -> str | None:
        """None if the order may go, else the first rule it breaks."""
        lim = self.limits
        if account.orders_this_second >= lim.max_orders_per_second:
            return f"order rate: {lim.max_orders_per_second} orders in this second already"
        if order.closes:
            return None
        return self._entry(order, account)

    def _entry(self, order: OrderIntent, account: Exposure) -> str | None:
        lim = self.limits
        key, day = order.instrument_key, to_ist(order.at).date()
        if self.halted():
            return f"kill switch: {lim.kill_switch_file} exists"
        loss = account.day_start_equity - account.equity
        if loss >= lim.max_daily_loss_pct_equity * account.day_start_equity:
            return f"daily loss limit: down {loss:,.0f} today"
        if account.peak_equity:
            fallen = (account.peak_equity - account.equity) / account.peak_equity
            if fallen >= lim.max_drawdown_pct_equity:
                return (
                    f"drawdown limit: {fallen:.1%} below the peak (limit "
                    f"{lim.max_drawdown_pct_equity:.0%}); the strategy is disabled"
                )
        latest = account.recent_nets[-lim.expectancy_window_trades :]
        if len(latest) >= lim.expectancy_window_trades and losing_beyond_chance(
            latest, lim.expectancy_confidence
        ):
            mean = sum(latest) / len(latest)
            return (
                f"expectancy: the last {len(latest)} trades lost Rs {-mean:,.0f} each on "
                "average, beyond chance; the strategy is disabled"
            )
        names = set(account.positions) | set(account.buying)
        if key not in names and len(names) >= lim.max_open_positions:
            return f"open positions: {len(names)} already (limit {lim.max_open_positions})"
        if order.notional > lim.max_position_pct_equity * account.equity:
            share = f"{lim.max_position_pct_equity:.0%}"
            return f"position size: {order.notional:,.0f} is over {share} of equity"
        in_symbol = account.positions.get(key, 0.0) + account.buying.get(key, 0.0) + order.notional
        if in_symbol > lim.max_symbol_notional:
            return f"symbol cap: {in_symbol:,.0f} in {key} (limit {lim.max_symbol_notional:,.0f})"
        turnover = self.liquidity(key, day) if self.liquidity else None
        if turnover is None or turnover < lim.min_median_turnover_rs:
            shown = "unknown" if turnover is None else f"{turnover:,.0f}"
            return f"liquidity: median turnover {shown} (minimum {lim.min_median_turnover_rs:,.0f})"
        flags = self.flags(key, day) if self.flags else None
        if flags is None and lim.unknown_flags == "refuse":
            return "surveillance status unknown"
        if flags and flags & lim.block_flags:
            return f"surveillance: {', '.join(sorted(flags & lim.block_flags))}"
        seen = account.last_data_at.get(key)
        if account.in_session and (
            seen is None or (order.at - seen).total_seconds() > lim.max_data_age_s
        ):
            return "stale data: no recent price for this stock"
        return None
