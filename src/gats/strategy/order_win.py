"""S1, intraday: buy after an order win that is large for the company.

The hypothesis G1b tests: a disclosed order that is large relative to the
company's revenue moves the price over the following minutes, and enough of
the move is left after we can act. These rules only express the trade;
whether it has an edge is for the gates, and the parameters are fixed by a
pre-registration before any backtest uses them.

Timing: a filing during the session is acted on at once (until the entry
cutoff); one outside market hours, or on a day without a session, yields a
signal the engine executes at the next open. A filing that reaches the
strategy late (``max_signal_delay``) is skipped: its reaction is gone.
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import ClassVar

from pydantic import Field

from gats.strategy.base import (
    BarEvent,
    Context,
    MarketEvent,
    Product,
    Signal,
    Strategy,
    StrategyParams,
)
from gats.timeutil import to_ist

_SESSION_CLOSE = time(15, 30)


class OrderWinParams(StrategyParams):
    min_amount_vs_revenue: float = Field(gt=0)  # order value / trailing revenue
    trade_unknown_size: bool = False  # no ratio (no revenue, no amount): skip unless True
    product: Product = "intraday"
    entry_cutoff_ist: time = time(15, 0)  # later in the session: too little time left
    max_signal_delay_minutes: int = Field(default=30, gt=0)
    hold_minutes: int = Field(gt=0, le=375)
    square_off_ist: time = time(15, 15)  # flatten intraday positions before the broker does


class OrderWinDrift(Strategy[OrderWinParams]):
    name: ClassVar[str] = "order_win_drift"
    params_model: ClassVar[type[OrderWinParams]] = OrderWinParams

    def on_event(self, event: MarketEvent, ctx: Context) -> list[Signal]:
        p = self.params
        key = event.instrument_key
        if event.event_type != "ORDER_WIN" or ctx.position(key) is not None or ctx.pending(key):
            return []
        if ctx.now - event.available_at > timedelta(minutes=p.max_signal_delay_minutes):
            return []
        ratio = event.facts.get("amount_vs_revenue")
        if ratio is None and not p.trade_unknown_size:
            return []
        if ratio is not None and float(ratio) < p.min_amount_vs_revenue:
            return []
        clock = to_ist(ctx.now).time()
        if ctx.trading_day and p.entry_cutoff_ist <= clock < _SESSION_CLOSE:
            return []  # too little of today's session left
        reason = f"{self.name}: order win #{event.event_id}"
        return [Signal(key, "buy", p.product, reason, ctx.now)]

    def on_bar(self, bar: BarEvent, ctx: Context) -> list[Signal]:
        p = self.params
        held = ctx.position(bar.instrument_key)
        if held is None or held.quantity <= 0 or not held.reason.startswith(self.name):
            return []
        if ctx.pending(bar.instrument_key) < 0:  # the exit is already on its way
            return []
        clock = to_ist(ctx.now).time()
        timed_out = ctx.now - held.opened_at >= timedelta(minutes=p.hold_minutes)
        closing = held.product == "intraday" and clock >= p.square_off_ist
        if not (timed_out or closing):
            return []
        why = "held long enough" if timed_out else "square-off time"
        return [
            Signal(
                bar.instrument_key,
                "sell",
                held.product,
                f"{self.name}: exit ({why})",
                ctx.now,
                quantity=held.quantity,
                closes=True,
            )
        ]
