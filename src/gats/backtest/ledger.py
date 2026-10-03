"""Ledger and metrics for a backtest run (T6.5).

Trades are built from fills, first in first out per stock, and each trade
carries its share of both sides' charges. So results can be read trade by
trade and still add up: the trades' net P&L, less what is still held,
equals the fills' cash flows to the paisa (:func:`reconciliation_gap`).

Metrics are computed from the equity curve. An event-driven run has bars
only on event days; pass the trading sessions so idle days count as flat
days. Without them a Sharpe ratio would be annualised from the busy days
alone and flatter the strategy.

The tax view only sorts results into categories (it computes no tax):
intraday trades are speculative business income; delivery trades are
short-term capital gains, or long-term when held for more than 12 months
(listed equity: Income Tax Department, incometaxindia.gov.in/sale-of-shares,
read 2026-10-03).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from gats.backtest.engine import BacktestResult, Execution
from gats.strategy.base import Product
from gats.timeutil import to_ist

TRADING_DAYS = 252  # the annualisation convention for daily ratios


@dataclass(frozen=True)
class Trade:
    """One closed lot: a buy fill (or part of one) matched with a sell."""

    instrument_key: str
    product: Product
    quantity: int
    opened_at: datetime
    closed_at: datetime
    entry_price: float
    exit_price: float
    charges: float  # both sides, pro rata
    entry_reason: str
    exit_reason: str

    @property
    def gross(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity

    @property
    def net(self) -> float:
        return self.gross - self.charges

    @property
    def tax_category(self) -> str:
        if self.product == "intraday":
            return "speculative business income (intraday)"
        opened, closed = to_ist(self.opened_at).date(), to_ist(self.closed_at).date()
        return (
            "long-term capital gain (delivery, over 12 months)"
            if closed > _a_year_after(opened)
            else "short-term capital gain (delivery)"
        )


def _a_year_after(day: date) -> date:
    try:
        return day.replace(year=day.year + 1)
    except ValueError:  # 29 February
        return day.replace(year=day.year + 1, day=28)


@dataclass
class _Lot:
    fill: Execution
    remaining: int


@dataclass(frozen=True)
class OpenLot:
    instrument_key: str
    quantity: int
    entry_price: float
    charges: float  # the buy side's share for what is still held

    @property
    def cost(self) -> float:
        return self.quantity * self.entry_price + self.charges


def build_trades(executions: Sequence[Execution]) -> tuple[list[Trade], list[OpenLot]]:
    """Closed trades (FIFO) and the lots still open."""
    lots: dict[str, list[_Lot]] = {}
    closed: list[Trade] = []
    for fill in sorted(executions, key=lambda e: e.at):
        if fill.side == "buy":
            lots.setdefault(fill.instrument_key, []).append(_Lot(fill, fill.quantity))
            continue
        to_match = fill.quantity
        queue = lots.get(fill.instrument_key, [])
        while to_match > 0:
            if not queue:
                raise ValueError(f"sold {fill.instrument_key} without holding it at {fill.at}")
            lot = queue[0]
            quantity = min(lot.remaining, to_match)
            charges = (
                lot.fill.charges.total * quantity / lot.fill.quantity
                + fill.charges.total * quantity / fill.quantity
            )
            closed.append(
                Trade(
                    fill.instrument_key,
                    fill.product,
                    quantity,
                    lot.fill.at,
                    fill.at,
                    lot.fill.price,
                    fill.price,
                    charges,
                    lot.fill.reason,
                    fill.reason,
                )
            )
            lot.remaining -= quantity
            to_match -= quantity
            if lot.remaining == 0:
                queue.pop(0)
    still_open = [
        OpenLot(
            key,
            lot.remaining,
            lot.fill.price,
            lot.fill.charges.total * lot.remaining / lot.fill.quantity,
        )
        for key, queue in lots.items()
        for lot in queue
    ]
    return closed, still_open


def cash_flow(executions: Sequence[Execution]) -> float:
    """What the fills did to cash: sales less purchases less every charge."""
    return math.fsum(
        (1 if e.side == "sell" else -1) * e.price * e.quantity - e.charges.total for e in executions
    )


def reconciliation_gap(executions: Sequence[Execution]) -> float:
    """Trades' net P&L minus open lots' cost, minus the fills' cash flow:
    zero when the ledger accounts for every paisa."""
    closed, still_open = build_trades(executions)
    explained = math.fsum(t.net for t in closed) - math.fsum(lot.cost for lot in still_open)
    return explained - cash_flow(executions)


def daily_returns(
    equity: dict[date, float], initial: float, sessions: Sequence[date] | None = None
) -> list[float]:
    """Day-over-day returns of the equity curve. With ``sessions``, every
    session counts and days without data carry the last equity (flat)."""
    days = sorted(equity) if sessions is None else sorted(sessions)
    returns: list[float] = []
    previous = initial
    for day in days:
        current = equity.get(day, previous)
        returns.append(current / previous - 1 if previous else 0.0)
        previous = current
    return returns


def max_drawdown(equity: Sequence[float]) -> tuple[float, float]:
    """(largest peak-to-trough fall as a fraction of the peak, in rupees)."""
    peak = worst = worst_rs = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0 and (peak - value) / peak > worst:
            worst, worst_rs = (peak - value) / peak, peak - value
    return worst, worst_rs


def sharpe(returns: Sequence[float]) -> float | None:
    """Annualised mean over standard deviation of daily returns (risk-free
    rate taken as zero); None when it cannot be estimated."""
    if len(returns) < 2:
        return None
    spread = statistics.stdev(returns)
    return None if spread == 0 else statistics.fmean(returns) / spread * math.sqrt(TRADING_DAYS)


def sortino(returns: Sequence[float]) -> float | None:
    """Like Sharpe, but only falls count as risk."""
    if len(returns) < 2:
        return None
    downside = math.sqrt(statistics.fmean(min(r, 0.0) ** 2 for r in returns))
    return None if downside == 0 else statistics.fmean(returns) / downside * math.sqrt(TRADING_DAYS)


@dataclass(frozen=True)
class Metrics:
    trades: int
    net_pnl: float
    gross_pnl: float
    charges: float
    win_rate: float | None
    avg_net_per_trade: float | None
    profit_factor: float | None  # net wins / net losses
    turnover: float  # rupees traded, both sides
    turnover_ratio: float | None  # turnover / average equity
    total_return: float
    max_drawdown: float
    max_drawdown_rs: float
    sharpe: float | None
    sortino: float | None
    days: int
    capacity_multiple: float | None  # see capacity_multiple()
    reconciliation_gap: float
    tax: dict[str, float] = field(default_factory=dict)


def capacity_multiple(executions: Sequence[Execution], participation: float) -> float | None:
    """How many times larger the entries could have been before the
    participation cap bound: the 25th percentile over entry fills of
    (cap * bar volume / filled quantity). Below 1 the cap already binds."""
    room = sorted(
        participation * e.bar_volume / e.quantity
        for e in executions
        if e.side == "buy" and e.bar_volume > 0
    )
    if not room:
        return None
    return room[max(0, math.ceil(0.25 * len(room)) - 1)]


def summarize(
    result: BacktestResult, *, participation: float, sessions: Sequence[date] | None = None
) -> Metrics:
    closed, _ = build_trades(result.executions)
    nets = [t.net for t in closed]
    wins = math.fsum(n for n in nets if n > 0)
    losses = -math.fsum(n for n in nets if n < 0)
    returns = daily_returns(result.equity, result.initial_cash, sessions)
    curve = [result.initial_cash]
    for r in returns:
        curve.append(curve[-1] * (1 + r))
    drawdown, drawdown_rs = max_drawdown(curve)
    turnover = math.fsum(e.price * e.quantity for e in result.executions)
    final = curve[-1]
    tax: dict[str, float] = {}
    for trade in closed:
        tax[trade.tax_category] = tax.get(trade.tax_category, 0.0) + trade.net
    return Metrics(
        trades=len(closed),
        net_pnl=math.fsum(nets),
        gross_pnl=math.fsum(t.gross for t in closed),
        charges=math.fsum(t.charges for t in closed),
        win_rate=sum(n > 0 for n in nets) / len(nets) if nets else None,
        avg_net_per_trade=statistics.fmean(nets) if nets else None,
        profit_factor=wins / losses if losses > 0 else None,
        turnover=turnover,
        turnover_ratio=turnover / statistics.fmean(curve)
        if curve and statistics.fmean(curve)
        else None,
        total_return=final / result.initial_cash - 1 if result.initial_cash else 0.0,
        max_drawdown=drawdown,
        max_drawdown_rs=drawdown_rs,
        sharpe=sharpe(returns),
        sortino=sortino(returns),
        days=len(returns),
        capacity_multiple=capacity_multiple(result.executions, participation),
        reconciliation_gap=reconciliation_gap(result.executions),
        tax=tax,
    )
