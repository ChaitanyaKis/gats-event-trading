"""Real orders (T8.1): written down before they are sent, sent once,
followed until they end, and reconciled against the broker.

The rules that keep a small pilot safe:

- **Intent first.** The order row is stored before the request goes out.
  A crash can therefore never leave an order at the broker that the system
  does not know about.
- **One key, one order.** The client id is derived from the run and the
  engine's order id, and the row's primary key. Submitting the same engine
  order twice (a restart, a replay) finds the row and sends nothing.
- **Never resend.** If a request gets no answer the order's state is
  ``unknown``: it may exist at the broker. The system does not guess and
  does not retry. It switches trading off and tells the human.
- **Caps before the wire.** Every order passes :mod:`gats.oms.caps`; a
  breach switches trading off too.
- **Reconcile.** What the stored fills add up to must equal what the broker
  says is held. Any difference switches trading off.

"Switching off" is the kill switch file the risk engine already obeys: no
new entries, exits still allowed, and a human has to remove the file.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Connection, Engine, Row, func, select

from gats.backtest.engine import Order
from gats.broker.base import (
    Broker,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerState,
    OutcomeUnknown,
)
from gats.db.schema import live_breaches, live_orders
from gats.logging_setup import kv
from gats.oms.caps import Account, LiveCaps, breach
from gats.timeutil import to_ist

log = logging.getLogger(__name__)

# intent: stored, not sent. sent: the broker accepted it. Terminal states end
# an order. unknown: sent with no answer; only a human or the broker's own
# report resolves it.
TERMINAL: Final = frozenset({"filled", "cancelled", "rejected", "refused"})
_NEXT: Final[Mapping[str, frozenset[str]]] = {
    "intent": frozenset({"sent", "unknown", "rejected", "refused"}),
    "sent": frozenset({"sent", "filled", "cancelled", "rejected", "unknown"}),
    "unknown": frozenset({"sent", "filled", "cancelled", "rejected", "unknown"}),
}


class IllegalTransition(RuntimeError):
    pass


def advance(state: str, new: str) -> str:
    """The new state, or an error for a move the state machine forbids
    (anything out of a terminal state, or backwards)."""
    if new not in _NEXT.get(state, frozenset()):
        raise IllegalTransition(f"an order cannot go from {state!r} to {new!r}")
    return new


def client_id(run_id: int, engine_order_id: int) -> str:
    """Stable across restarts and replays: the same engine order always
    maps to the same real order."""
    return f"gats-{run_id}-{engine_order_id}"


def state_of(report: BrokerState, quantity: int, done_statuses: Mapping[str, str]) -> str:
    """Our state for a broker report. Quantities decide a fill (they are
    documented fields); the broker's status word is used only through
    ``done_statuses``, a configured map of the words that end an order."""
    if report.filled_quantity >= quantity:
        return "filled"
    return done_statuses.get(report.status.lower(), "sent")


@dataclass(frozen=True)
class Mismatch:
    instrument_key: str
    ours: int
    brokers: int

    def __str__(self) -> str:
        return f"{self.instrument_key}: we hold {self.ours}, the broker says {self.brokers}"


def reconcile(ours: Mapping[str, int], brokers: Sequence[BrokerPosition]) -> list[Mismatch]:
    """Stocks where our book and the broker's disagree (zero on one side
    counts: a position we do not know about is the dangerous case)."""
    theirs: dict[str, int] = {}
    for position in brokers:
        theirs[position.instrument_key] = theirs.get(position.instrument_key, 0) + position.quantity
    return [
        Mismatch(key, ours.get(key, 0), theirs.get(key, 0))
        for key in sorted(set(ours) | set(theirs))
        if ours.get(key, 0) != theirs.get(key, 0)
    ]


class Oms:
    def __init__(
        self,
        db: Engine,
        broker: Broker,
        caps: LiveCaps,
        *,
        approval_id: int,
        run_id: int,
        kill_switch: Path,
        clock: Callable[[], datetime],
        done_statuses: Mapping[str, str],
    ) -> None:
        self.db = db
        self.broker = broker
        self.caps = caps
        self.approval_id = approval_id
        self.run_id = run_id
        self.kill_switch = kill_switch
        self.clock = clock
        self.done_statuses = dict(done_statuses)

    # --- switching off ------------------------------------------------------------

    def halt(self, kind: str, detail: str) -> None:
        """Switch live trading off and record why. Idempotent."""
        now = self.clock()
        with self.db.begin() as conn:
            conn.execute(live_breaches.insert().values(at=now, kind=kind, detail=detail))
        self.kill_switch.parent.mkdir(parents=True, exist_ok=True)
        if not self.kill_switch.exists():
            self.kill_switch.write_text(
                f"{now.isoformat()} {kind}: {detail}\n", encoding="utf-8", newline="\n"
            )
        log.error("LIVE TRADING SWITCHED OFF %s", kv(kind=kind, detail=detail))

    @property
    def halted(self) -> bool:
        return self.kill_switch.exists()

    # --- the book -----------------------------------------------------------------

    def _rows(self, conn: Connection) -> list[Row[Any]]:
        return list(
            conn.execute(select(live_orders).where(live_orders.c.run_id == self.run_id)).all()
        )

    def held(self) -> dict[str, int]:
        """Net shares per stock that the stored fills add up to."""
        with self.db.begin() as conn:
            rows = self._rows(conn)
        book: dict[str, int] = {}
        for row in rows:
            signed = row.filled_quantity if row.side == "buy" else -row.filled_quantity
            book[row.instrument_key] = book.get(row.instrument_key, 0) + signed
        return {key: n for key, n in book.items() if n}

    def sellable(self, instrument_key: str) -> int:
        """Shares of a stock that real fills hold and no working sell
        order already covers."""
        with self.db.begin() as conn:
            rows = [r for r in self._rows(conn) if r.instrument_key == instrument_key]
        held = sum(r.filled_quantity if r.side == "buy" else -r.filled_quantity for r in rows)
        leaving = sum(
            r.quantity - r.filled_quantity
            for r in rows
            if r.side == "sell" and r.state not in TERMINAL
        )
        return max(int(held - leaving), 0)

    def account(self, instrument_key: str, day_loss_rs: float) -> Account:
        """What the caps look at. Costs use limit prices for working buys
        (the most they can cost) and fill prices for what is held."""
        now = self.clock()
        today = to_ist(now).date()
        with self.db.begin() as conn:
            rows = self._rows(conn)
        deployed = in_stock = 0.0
        orders_today = 0
        for row in rows:
            if row.state != "refused" and to_ist(row.created_at).date() == today:
                orders_today += 1
            price = row.average_price or row.limit_price
            if row.side == "buy":
                working = 0 if row.state in TERMINAL else row.quantity - row.filled_quantity
                cost = row.filled_quantity * price + working * row.limit_price
            else:
                cost = -row.filled_quantity * price
            deployed += cost
            if row.instrument_key == instrument_key:
                in_stock += cost
        return Account(max(deployed, 0.0), max(in_stock, 0.0), day_loss_rs, orders_today)

    # --- sending ------------------------------------------------------------------

    async def submit(self, order: Order, day_loss_rs: float) -> str:
        """Send one engine order as a real, protected limit order. Returns
        the stored state. An order already known is not sent again."""
        signal = order.signal
        key = client_id(self.run_id, order.order_id)
        now = self.clock()
        with self.db.begin() as conn:
            known = conn.execute(
                select(live_orders.c.state).where(live_orders.c.client_id == key)
            ).scalar()
        if known is not None:
            return str(known)
        quantity = order.quantity
        broken: str | None = None
        switch_off = True  # a refusal that means something upstream is wrong
        if signal.side == "sell":
            # Long only: a real sell never exceeds what the real fills hold.
            # The engine's simulated position can be larger (a real entry
            # that was refused or never filled), and selling shares that are
            # not there would open a short.
            quantity = min(quantity, self.sellable(signal.instrument_key))
            if quantity <= 0:
                broken, switch_off = "nothing held at the broker to sell", False
        elif self.halted:
            broken, switch_off = "live trading is switched off (kill switch)", False
        if broken is None:
            account = self.account(signal.instrument_key, day_loss_rs)
            broken = breach(self.caps, quantity * order.limit, signal.closes, account)
        row = {
            "client_id": key,
            "approval_id": self.approval_id,
            "run_id": self.run_id,
            "engine_order_id": order.order_id,
            "instrument_key": signal.instrument_key,
            "side": signal.side,
            "product": signal.product,
            "quantity": quantity if quantity > 0 else order.quantity,
            "limit_price": order.limit,
            "closes": signal.closes,
            "state": "refused" if broken else "intent",
            "filled_quantity": 0,
            "message": broken,
            "created_at": now,
            "updated_at": now,
        }
        with self.db.begin() as conn:
            conn.execute(live_orders.insert().values(**row))
        if broken:
            if switch_off and not self.halted:
                self.halt("cap", f"{key}: {broken}")
            return "refused"
        request = BrokerOrder(
            key, signal.instrument_key, signal.side, signal.product, quantity, order.limit
        )
        try:
            broker_id = await self.broker.place(request)
        except OutcomeUnknown as exc:
            self._set(key, "intent", "unknown", message=str(exc)[:300])
            self.halt("unknown_order", f"{key}: no answer from the broker; it may exist there")
            return "unknown"
        except BrokerError as exc:
            self._set(key, "intent", "rejected", message=str(exc)[:300])
            return "rejected"
        self._set(key, "intent", "sent", broker_order_id=broker_id)
        return "sent"

    def _set(self, key: str, state: str, new: str, **values: Any) -> None:
        advance(state, new)
        with self.db.begin() as conn:
            conn.execute(
                live_orders.update()
                .where(live_orders.c.client_id == key)
                .values(state=new, updated_at=self.clock(), **values)
            )

    # --- following ----------------------------------------------------------------

    async def refresh(self) -> int:
        """Ask the broker about every order that has not ended; returns how
        many changed."""
        with self.db.begin() as conn:
            rows = [
                r
                for r in self._rows(conn)
                if r.state in ("sent", "unknown") and r.broker_order_id is not None
            ]
        changed = 0
        for row in rows:
            try:
                report = await self.broker.order(row.broker_order_id)
            except BrokerError as exc:
                log.warning("order not refreshed %s", kv(order=row.client_id, error=str(exc)))
                continue
            new = state_of(report, row.quantity, self.done_statuses)
            same = (new, report.filled_quantity) == (row.state, row.filled_quantity)
            if same:
                continue
            self._set(
                row.client_id,
                row.state,
                new,
                filled_quantity=report.filled_quantity,
                average_price=report.average_price,
                message=report.message,
            )
            changed += 1
        return changed

    async def differences(self) -> list[Mismatch]:
        """Where our book and the broker's disagree, after asking about
        every working order first (a fill we have not heard of yet is not a
        disagreement). Nothing is switched off here."""
        await self.refresh()
        try:
            positions = await self.broker.positions()
        except BrokerError as exc:
            log.warning("positions not checked %s", kv(error=str(exc)))
            return []
        return reconcile(self.held(), positions)

    async def check_positions(self) -> list[Mismatch]:
        """Compare our book with the broker's; a difference switches off."""
        mismatches = await self.differences()
        if mismatches:
            self.halt("reconcile", "; ".join(str(m) for m in mismatches))
        return mismatches

    async def cancel_expired(self, engine_orders: Sequence[Order]) -> int:
        """Cancel the real order of every engine order that has ended
        without filling in the simulation (its time ran out, the day
        closed). Left alone it could fill later, behind the engine's back.
        Returns how many cancels the broker accepted."""
        ended = {o.order_id for o in engine_orders if o.status in ("expired", "rejected")}
        with self.db.begin() as conn:
            rows = [
                r
                for r in self._rows(conn)
                if r.state == "sent" and r.engine_order_id in ended and r.broker_order_id
            ]
        done = 0
        for row in rows:
            try:
                await self.broker.cancel(row.broker_order_id)
            except OutcomeUnknown as exc:
                self._set(row.client_id, "sent", "unknown", message=str(exc)[:300])
                self.halt("unknown_order", f"{row.client_id}: cancel got no answer")
                continue
            except BrokerError as exc:  # already filled or gone: the next refresh says which
                log.warning("cancel refused %s", kv(order=row.client_id, error=str(exc)))
                continue
            self._set(row.client_id, "sent", "cancelled", message="ended in the simulation")
            done += 1
        return done

    def open_orders(self) -> int:
        with self.db.begin() as conn:
            return int(
                conn.execute(
                    select(func.count())
                    .select_from(live_orders)
                    .where(
                        live_orders.c.run_id == self.run_id,
                        live_orders.c.state.in_(("intent", "sent", "unknown")),
                    )
                ).scalar_one()
            )
