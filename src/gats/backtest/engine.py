"""Event-driven backtest engine and simulated broker (T6.3).

Replays events and one-minute bars in the order they became known and
runs a strategy against them exactly as a live runtime would. Each fill
rule leans pessimistic, because a backtest that flatters a strategy is
worse than none:

- **Latency.** An order reaches the market ``latency_s`` after the decision
  and can only fill in a bar that *starts* after that, never inside the bar
  in which it was decided (nothing is known about the path within a bar).
- **Protection band.** Orders without a limit become limits at the
  reference price +/- ``protection_band``; a gap through the band means no
  fill, as with a protected market order.
- **Participation.** At most ``participation`` of a bar's volume; the rest
  waits for later bars until ``order_ttl_minutes`` runs out.
- **Slippage.** Half the spread plus impact growing with participation, in
  basis points, never through the limit.
- **Circuits.** No buy in a bar locked at the upper limit, no sell at the
  lower. With unknown limits, a flat bar at the day's high (low) counts as
  locked for buys (sells).
- **Intraday square-off** at ``square_off_ist``: open positions are sold
  and no new intraday position opens from then on; positions still open
  when the day's data ends are closed at the last price, flagged.
- **Delivery is T+1**: shares bought today cannot be sold today, and sale
  proceeds become usable on the next trading day.
- **No leverage**: every buy needs the cash for the shares and the charges.

Long positions only (S1 is long-only).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Literal, Protocol

from gats.backtest.costs import Charges, CostModel, Fill
from gats.risk.engine import Exposure, OrderIntent
from gats.strategy.base import (
    BarEvent,
    MarketEvent,
    Position,
    Product,
    Side,
    Signal,
    StaticContext,
    Strategy,
)
from gats.timeutil import to_ist

OrderStatus = Literal["working", "filled", "expired", "rejected"]
CircuitLimits = Callable[[str, date], tuple[float, float] | None]


@dataclass(frozen=True)
class EngineConfig:
    initial_cash: float
    notional_per_trade: float  # sizing when a signal names no quantity
    latency_s: float = 5.0  # decision -> order at the exchange (use measured p95)
    protection_band: float = 0.02
    participation: float = 0.10
    half_spread_bps: float = 5.0
    impact_bps: float = 50.0  # added at 100% participation, linearly
    order_ttl_minutes: int = 30
    square_off_ist: time = time(15, 20)
    block_flat_bars: bool = True


@dataclass
class Order:
    order_id: int
    signal: Signal
    quantity: int
    limit: float
    submitted_at: datetime
    eligible_at: datetime
    # Set at the first bar the order can trade in: an order sent after the
    # close starts its time to live at the next open, not overnight.
    expires_at: datetime | None = None
    filled: int = 0
    status: OrderStatus = "working"
    note: str = ""

    @property
    def remaining(self) -> int:
        return self.quantity - self.filled


@dataclass(frozen=True)
class Execution:
    order_id: int
    instrument_key: str
    side: Side
    product: Product
    quantity: int
    price: float
    at: datetime
    charges: Charges
    reason: str


class RiskGate(Protocol):
    """The risk engine's veto (gats.risk.engine.RiskEngine in real runs);
    returns why an order is refused, or None."""

    def check(self, order: OrderIntent, account: Exposure) -> str | None: ...


class AllowAll:
    def check(self, order: OrderIntent, account: Exposure) -> str | None:
        return None


_SESSION_OPEN, _SESSION_CLOSE = time(9, 15), time(15, 30)


@dataclass
class EngineState:
    cash: float
    unsettled: list[tuple[date, float]] = field(default_factory=list)  # (usable from, amount)
    positions: dict[str, Position] = field(default_factory=dict)
    last_price: dict[str, float] = field(default_factory=dict)
    day_high: dict[str, float] = field(default_factory=dict)
    day_low: dict[str, float] = field(default_factory=dict)
    bought_on: dict[str, date] = field(default_factory=dict)  # delivery buys, for T+1
    dp_paid: set[tuple[date, str]] = field(default_factory=set)
    last_data_at: dict[str, datetime] = field(default_factory=dict)
    day_start_equity: float = 0.0
    day: date | None = None


@dataclass
class BacktestResult:
    orders: list[Order]
    executions: list[Execution]
    cash: float
    positions: dict[str, Position]
    equity: dict[date, float]  # cash + positions at the day's last price
    duplicates: int = 0  # closing signals dropped because an exit was already working

    @property
    def charges(self) -> float:
        return sum(e.charges.total for e in self.executions)


def moment(item: MarketEvent | BarEvent) -> datetime:
    """When an item becomes known: events when available, bars when closed."""
    return item.available_at if isinstance(item, MarketEvent) else item.closed_at


class Engine:
    def __init__(
        self,
        strategy: Strategy,  # type: ignore[type-arg]
        costs: CostModel,
        config: EngineConfig,
        *,
        risk: RiskGate | None = None,
        circuit_limits: CircuitLimits | None = None,
    ) -> None:
        self.strategy = strategy
        self.costs = costs
        self.config = config
        self.risk = risk or AllowAll()
        self.circuit_limits = circuit_limits
        self.state = EngineState(cash=config.initial_cash, day_start_equity=config.initial_cash)
        self.orders: list[Order] = []
        self.executions: list[Execution] = []
        self.duplicates = 0
        self.equity: dict[date, float] = {}

    # --- the loop -----------------------------------------------------------------

    def run(self, items: Iterable[MarketEvent | BarEvent]) -> BacktestResult:
        ordered = sorted(items, key=lambda i: (moment(i), isinstance(i, MarketEvent)))
        for item in ordered:
            now = moment(item)
            self._new_day(to_ist(item.start if isinstance(item, BarEvent) else now).date())
            if isinstance(item, BarEvent):
                self._on_bar(item)
                signals = self.strategy.on_bar(item, self._context(now))
            else:
                signals = self.strategy.on_event(item, self._context(now))
            for signal in signals:
                self._submit(signal, now)
        self._end_of_day()
        return BacktestResult(
            self.orders,
            self.executions,
            self.state.cash,
            dict(self.state.positions),
            self.equity,
            self.duplicates,
        )

    def _context(self, now: datetime) -> StaticContext:
        working: dict[str, int] = {}
        for order in self.orders:
            if order.status == "working":
                sign = 1 if order.signal.side == "buy" else -1
                key = order.signal.instrument_key
                working[key] = working.get(key, 0) + sign * order.remaining
        return StaticContext(now, dict(self.state.positions), working)

    def _new_day(self, day: date) -> None:
        st = self.state
        if st.day == day:
            return
        if st.day is not None:
            self._end_of_day()
        st.day = day
        st.day_high.clear()
        st.day_low.clear()
        for usable_from, amount in list(st.unsettled):
            if usable_from <= day:
                st.cash += amount
                st.unsettled.remove((usable_from, amount))
        st.day_start_equity = self._equity()

    def _equity(self) -> float:
        st = self.state
        held = sum(
            p.quantity * st.last_price.get(k, p.entry_price) for k, p in st.positions.items()
        )
        return st.cash + sum(amount for _, amount in st.unsettled) + held

    def _exposure(self, now: datetime) -> Exposure:
        """The account as the risk engine sees it at ``now``."""
        st = self.state
        buying: dict[str, float] = {}
        this_second = 0
        for order in self.orders:
            if order.status == "rejected":
                continue  # never left for the broker
            if order.submitted_at.replace(microsecond=0) == now.replace(microsecond=0):
                this_second += 1
            if order.status == "working" and order.signal.side == "buy":
                key = order.signal.instrument_key
                buying[key] = buying.get(key, 0.0) + order.remaining * order.limit
        return Exposure(
            equity=self._equity(),
            day_start_equity=st.day_start_equity,
            positions={
                k: p.quantity * st.last_price.get(k, p.entry_price) for k, p in st.positions.items()
            },
            buying=buying,
            last_data_at=dict(st.last_data_at),
            orders_this_second=this_second,
            in_session=_SESSION_OPEN <= to_ist(now).time() < _SESSION_CLOSE,
        )

    def _end_of_day(self) -> None:
        st = self.state
        if st.day is None:
            return
        for key, pos in list(st.positions.items()):
            if pos.product == "intraday" and key in st.last_price:
                self._execute(
                    None,
                    key,
                    "sell",
                    pos.product,
                    pos.quantity,
                    st.last_price[key],
                    self._close_time(st.day),
                    "intraday position open at the end of the data",
                )
        close = self._close_time(st.day)
        for order in self.orders:
            sent_today = order.submitted_at < close
            if order.status == "working" and order.signal.product == "intraday" and sent_today:
                order.status, order.note = "expired", "end of day"
        self.equity[st.day] = self._equity()

    @staticmethod
    def _close_time(day: date) -> datetime:
        from gats.timeutil import ist_datetime

        return ist_datetime(day, time(15, 30))

    # --- orders -------------------------------------------------------------------

    def _submit(self, signal: Signal, now: datetime) -> Order | None:
        st, cfg = self.state, self.config
        if signal.closes and self._exit_working(signal.instrument_key):
            self.duplicates += 1  # an exit is already working: sending another adds nothing
            return None
        ref = st.last_price.get(signal.instrument_key)
        order_id = len(self.orders) + 1
        if ref is None:
            return self._reject(order_id, signal, now, "no reference price yet")
        held = st.positions.get(signal.instrument_key)
        if signal.side == "sell":
            if held is None:
                return self._reject(order_id, signal, now, "nothing to sell (long only)")
            quantity = min(signal.quantity or held.quantity, held.quantity)
            if held.product == "delivery" and st.bought_on.get(signal.instrument_key) == st.day:
                return self._reject(order_id, signal, now, "T+1: bought today")
            limit = signal.limit_price or ref * (1 - cfg.protection_band)
        else:
            quantity = signal.quantity or math.floor(cfg.notional_per_trade / ref)
            limit = signal.limit_price or ref * (1 + cfg.protection_band)
        if quantity <= 0:
            return self._reject(order_id, signal, now, "size rounds to zero shares")
        eligible = now + timedelta(seconds=cfg.latency_s)
        order = Order(order_id, signal, quantity, limit, now, eligible)
        intent = OrderIntent(
            signal.instrument_key, signal.side, signal.product, quantity, limit, signal.closes, now
        )
        refusal = self.risk.check(intent, self._exposure(now))
        self.orders.append(order)
        if refusal is not None:
            order.status, order.note = "rejected", f"risk: {refusal}"
            return order
        return order

    def _reject(self, order_id: int, signal: Signal, now: datetime, why: str) -> Order:
        order = Order(
            order_id,
            signal,
            signal.quantity or 0,
            signal.limit_price or 0.0,
            now,
            now,
            status="rejected",
            note=why,
        )
        self.orders.append(order)
        return order

    # --- bars ---------------------------------------------------------------------

    def _on_bar(self, bar: BarEvent) -> None:
        st, cfg = self.state, self.config
        key = bar.instrument_key
        bar_time = to_ist(bar.start).time()
        held = st.positions.get(key)
        due = held is not None and held.product == "intraday" and bar_time >= cfg.square_off_ist
        if held is not None and due and not self._exit_working(key):
            forced = Signal(
                key,
                "sell",
                "intraday",
                "auto square-off",
                bar.start,
                quantity=held.quantity,
                closes=True,
            )
            self._submit(forced, bar.start - timedelta(seconds=cfg.latency_s))
        for order in self.orders:
            if order.status != "working" or order.signal.instrument_key != key:
                continue
            if bar.start < order.eligible_at:
                continue
            if order.expires_at is None:
                order.expires_at = bar.start + timedelta(minutes=cfg.order_ttl_minutes)
            if bar.start >= order.expires_at:
                order.status, order.note = "expired", "time to live ran out"
                continue
            late_entry = order.signal.product == "intraday" and not order.signal.closes
            if late_entry and bar_time >= cfg.square_off_ist:
                order.status, order.note = "expired", "square-off time: no new intraday positions"
                continue
            self._try_fill(order, bar)
        st.last_price[key] = bar.close
        st.last_data_at[key] = bar.closed_at
        st.day_high[key] = max(st.day_high.get(key, bar.high), bar.high)
        st.day_low[key] = min(st.day_low.get(key, bar.low), bar.low)

    def _exit_working(self, key: str) -> bool:
        return any(
            o.status == "working" and o.signal.closes and o.signal.instrument_key == key
            for o in self.orders
        )

    def _locked(self, side: Side, bar: BarEvent) -> bool:
        st = self.state
        limits = (
            self.circuit_limits(bar.instrument_key, to_ist(bar.start).date())
            if self.circuit_limits
            else None
        )
        if limits is not None:
            lower, upper = limits
            return bar.low >= upper if side == "buy" else bar.high <= lower
        if not self.config.block_flat_bars or bar.high != bar.low:
            return False
        if side == "buy":
            return bar.high >= st.day_high.get(bar.instrument_key, bar.high)
        return bar.low <= st.day_low.get(bar.instrument_key, bar.low)

    def _try_fill(self, order: Order, bar: BarEvent) -> None:
        cfg = self.config
        side = order.signal.side
        if self._locked(side, bar):
            return
        quantity = min(order.remaining, math.floor(cfg.participation * bar.volume))
        if side == "sell":
            held = self.state.positions.get(bar.instrument_key)
            if held is None:  # closed meanwhile (square-off, another exit)
                order.status, order.note = "expired", "position already closed"
                return
            quantity = min(quantity, held.quantity)
        if quantity <= 0:
            return
        slip = (cfg.half_spread_bps + cfg.impact_bps * quantity / bar.volume) / 1e4
        if side == "buy":
            if bar.open <= order.limit:
                price = min(bar.open * (1 + slip), order.limit)
            elif bar.low <= order.limit:
                price = order.limit
            else:
                return
            affordable = math.floor(
                self.state.cash / (price * (1 + 0.01))
            )  # 1% headroom for charges
            quantity = min(quantity, affordable)
            if quantity <= 0:
                order.status, order.note = "rejected", "not enough cash"
                return
        else:
            if bar.open >= order.limit:
                price = max(bar.open * (1 - slip), order.limit)
            elif bar.high >= order.limit:
                price = order.limit
            else:
                return
        self._execute(
            order,
            bar.instrument_key,
            side,
            order.signal.product,
            quantity,
            price,
            bar.start,
            order.signal.reason,
        )

    # --- bookkeeping --------------------------------------------------------------

    def _execute(
        self,
        order: Order | None,
        key: str,
        side: Side,
        product: Product,
        quantity: int,
        price: float,
        at: datetime,
        reason: str,
    ) -> None:
        st = self.state
        day = to_ist(at).date()
        dp_applies = side == "sell" and product == "delivery" and (day, key) not in st.dp_paid
        charges = self.costs.charges(Fill(side, product, price, quantity, day, dp_applies))
        if dp_applies:
            st.dp_paid.add((day, key))
        value = price * quantity
        held = st.positions.get(key)
        if side == "buy":
            st.cash -= value + charges.total
            if held is None:
                st.positions[key] = Position(key, quantity, product, at, price, reason)
            else:
                total = held.quantity + quantity
                avg = (held.entry_price * held.quantity + value) / total
                st.positions[key] = replace(held, quantity=total, entry_price=avg)
            if product == "delivery":
                st.bought_on[key] = day
        else:
            assert held is not None
            proceeds = value - charges.total
            if product == "delivery":
                st.unsettled.append((day + timedelta(days=1), proceeds))
            else:
                st.cash += proceeds
            left = held.quantity - quantity
            if left > 0:
                st.positions[key] = replace(held, quantity=left)
            else:
                del st.positions[key]
                st.bought_on.pop(key, None)
        if order is not None:
            order.filled += quantity
            if order.remaining == 0:
                order.status = "filled"
        self.executions.append(
            Execution(
                order.order_id if order else 0,
                key,
                side,
                product,
                quantity,
                price,
                at,
                charges,
                reason,
            )
        )
