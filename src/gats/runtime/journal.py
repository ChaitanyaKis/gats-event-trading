"""A paper run and its journal (T7.1).

A paper run is the backtest engine fed live. Everything the engine is given
goes into ``paper_journal`` in the order it was given: each filing, each
closed one-minute bar, each reference price and each day's close, with the
moment it was handed over. The engine is deterministic, so replaying the
journal rebuilds the account exactly. That gives:

- **Restart.** After a crash, a reboot or a deploy the run resumes where it
  stopped, with no hand-written snapshot of the engine's state to keep in
  step with the engine.
- **Audit.** Any past decision can be replayed with what was known then.
- **An honest record.** Orders and fills are also stored as they happen. A
  replay must reproduce them; if changed code would decide differently now,
  the run refuses to resume instead of quietly rewriting its history.

The risk rules ask about things outside the journal (liquidity, surveillance
lists, the kill switch). :class:`Tape` writes each answer next to the step
that asked, and a replay is answered from the tape, so it never depends on
today's database or file system.

A step and what it caused are stored in one transaction. Paper trading has
no effect outside the database, so "the step happened if and only if its
journal row exists" needs no write-ahead log. (Real orders will: T8.1.)
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import Connection, Row, select

from gats.backtest.engine import Engine, Execution, Listener, Order
from gats.db import repo
from gats.db.schema import paper_executions, paper_journal, paper_orders, paper_runs
from gats.research.registry import git_state
from gats.strategy.base import BarEvent, MarketEvent
from gats.timeutil import to_utc


class DesignChanged(RuntimeError):
    """The run was started as a different trading system."""


class Diverged(RuntimeError):
    """Replaying the journal no longer gives what the run recorded."""


@dataclass(frozen=True)
class Reference:
    """The last price of a stock from a session the engine was not fed."""

    instrument_key: str
    price: float
    as_of: datetime


@dataclass(frozen=True)
class DayClose:
    """The session of ``day`` is over."""

    day: date


Item = MarketEvent | BarEvent | Reference | DayClose


def item_key(item: Item) -> str:
    """The natural key that keeps one input from being taken twice."""
    if isinstance(item, MarketEvent):
        return f"event|{item.event_id}"
    if isinstance(item, BarEvent):
        return f"bar|{item.instrument_key}|{to_utc(item.start).isoformat()}"
    if isinstance(item, Reference):
        return f"ref|{item.instrument_key}|{to_utc(item.as_of).isoformat()}"
    return f"close|{item.day.isoformat()}"


def encode(item: Item) -> tuple[str, dict[str, Any]]:
    if isinstance(item, MarketEvent):
        return "event", {
            "event_id": item.event_id,
            "security_id": item.security_id,
            "instrument_key": item.instrument_key,
            "event_type": item.event_type,
            "available_at": to_utc(item.available_at).isoformat(),
            "facts": dict(item.facts),
        }
    if isinstance(item, BarEvent):
        return "bar", {
            "instrument_key": item.instrument_key,
            "start": to_utc(item.start).isoformat(),
            "open": item.open,
            "high": item.high,
            "low": item.low,
            "close": item.close,
            "volume": item.volume,
        }
    if isinstance(item, Reference):
        return "ref", {
            "instrument_key": item.instrument_key,
            "price": item.price,
            "as_of": to_utc(item.as_of).isoformat(),
        }
    return "close", {"day": item.day.isoformat()}


def decode(kind: str, payload: Mapping[str, Any]) -> Item:
    if kind == "event":
        return MarketEvent(
            int(payload["event_id"]),
            int(payload["security_id"]),
            str(payload["instrument_key"]),
            str(payload["event_type"]),
            datetime.fromisoformat(payload["available_at"]),
            dict(payload.get("facts") or {}),
        )
    if kind == "bar":
        return BarEvent(
            str(payload["instrument_key"]),
            datetime.fromisoformat(payload["start"]),
            float(payload["open"]),
            float(payload["high"]),
            float(payload["low"]),
            float(payload["close"]),
            int(payload["volume"]),
        )
    if kind == "ref":
        return Reference(
            str(payload["instrument_key"]),
            float(payload["price"]),
            datetime.fromisoformat(payload["as_of"]),
        )
    if kind == "close":
        return DayClose(date.fromisoformat(payload["day"]))
    raise ValueError(f"unknown journal kind {kind!r}")


class World(Protocol):
    """What the risk rules may ask about the world outside the journal."""

    def liquidity(self, instrument_key: str, day: date) -> float | None: ...

    def flags(self, instrument_key: str, day: date) -> frozenset[str] | None: ...

    def halted(self) -> bool: ...


class Tape:
    """Answers the risk rules: from the world when live, writing each answer
    down; from what was written down when replaying."""

    def __init__(self) -> None:
        self.world: World | None = None
        self.recorded: dict[str, Any] | None = None  # set while replaying
        self.answers: dict[str, Any] = {}

    def begin(self, world: World | None, recorded: Mapping[str, Any] | None = None) -> None:
        self.world = world
        self.recorded = None if recorded is None else dict(recorded)
        self.answers = {}

    def liquidity(self, instrument_key: str, day: date) -> float | None:
        answer = self._ask(
            f"liquidity|{instrument_key}|{day.isoformat()}",
            lambda world: world.liquidity(instrument_key, day),
        )
        return None if answer is None else float(answer)

    def flags(self, instrument_key: str, day: date) -> frozenset[str] | None:
        def ask(world: World) -> list[str] | None:
            got = world.flags(instrument_key, day)
            return None if got is None else sorted(got)

        answer = self._ask(f"flags|{instrument_key}|{day.isoformat()}", ask)
        return None if answer is None else frozenset(answer)

    def halted(self) -> bool:
        return bool(self._ask("halted", lambda world: world.halted()))

    def _ask(self, question: str, live: Callable[[World], Any]) -> Any:
        if question in self.answers:  # a step is one instant: one answer per question
            return self.answers[question]
        if self.recorded is not None:
            if question not in self.recorded:
                raise Diverged(f"the replay asks {question!r}, which the run never asked here")
            answer = self.recorded[question]
        else:
            if self.world is None:
                raise RuntimeError(f"nobody to ask {question!r}: no world was given")
            answer = live(self.world)
        self.answers[question] = answer
        return answer


class _Heard:
    """Collects what one step caused."""

    def __init__(self) -> None:
        self.orders: list[Order] = []
        self.executions: list[Execution] = []

    def order(self, order: Order) -> None:
        self.orders.append(order)

    def execution(self, execution: Execution) -> None:
        self.executions.append(execution)

    def take(self) -> tuple[list[Order], list[Execution]]:
        taken = (self.orders, self.executions)
        self.orders, self.executions = [], []
        return taken


@dataclass(frozen=True)
class Applied:
    """What one step caused: the new orders and the fills."""

    seq: int
    orders: list[Order]
    executions: list[Execution]


Build = Callable[[Tape, Listener], Engine]
_OrderState = tuple[int, str, str, datetime | None]


def _state(order: Order) -> _OrderState:
    return order.filled, order.status, order.note, order.expires_at


def _order_row(run_id: int, order: Order) -> dict[str, Any]:
    signal = order.signal
    return {
        "run_id": run_id,
        "order_id": order.order_id,
        "instrument_key": signal.instrument_key,
        "side": signal.side,
        "product": signal.product,
        "quantity": order.quantity,
        "limit_price": order.limit,
        "closes": signal.closes,
        "reason": signal.reason,
        "submitted_at": order.submitted_at,
        "eligible_at": order.eligible_at,
        "expires_at": order.expires_at,
        "filled": order.filled,
        "status": order.status,
        "note": order.note,
    }


def _fill_row(run_id: int, seq: int, execution: Execution) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "seq": seq,
        "order_id": execution.order_id,
        "instrument_key": execution.instrument_key,
        "side": execution.side,
        "product": execution.product,
        "quantity": execution.quantity,
        "price": execution.price,
        "at": execution.at,
        "charges": execution.charges.total,
        "reason": execution.reason,
        "bar_volume": execution.bar_volume,
    }


def _differences(stored: Mapping[str, Any], now: Mapping[str, Any]) -> list[str]:
    """Columns in which a stored row and the replay's row disagree."""
    out = []
    for column, value in now.items():
        was = stored[column]
        if isinstance(value, float) and isinstance(was, (int, float)):
            same = math.isclose(float(was), value, rel_tol=1e-9, abs_tol=1e-9)
        else:
            same = was == value
        if not same:
            out.append(f"{column}: recorded {was!r}, replay {value!r}")
    return out


def _flat(design: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in design.items():
        if isinstance(value, Mapping):
            out.update(_flat(value, f"{prefix}{name}."))
        else:
            out[f"{prefix}{name}"] = value
    return out


class PaperRun:
    """One paper run: its engine, and the journal that can rebuild it.

    If :meth:`apply` raises, the engine may be half-way through a step:
    discard the object and :meth:`open` the run again.
    """

    def __init__(self, run_id: int, name: str, engine: Engine, tape: Tape, heard: _Heard) -> None:
        self.id = run_id
        self.name = name
        self.engine = engine
        self._tape = tape
        self._heard = heard
        self._seq = 0
        self._fills = 0
        self._stored: dict[int, _OrderState] = {}
        self._working: set[int] = set()
        self._keys: set[str] = set()  # references and day closes taken
        self.events: set[int] = set()
        self.last_bar: dict[str, datetime] = {}  # instrument -> start of its last bar
        self.securities: dict[str, int] = {}  # instrument -> security, from the events
        self.last_at: datetime | None = None

    @classmethod
    def open(
        cls,
        conn: Connection,
        *,
        name: str,
        strategy: str,
        design_hash: str,
        design: Mapping[str, Any],
        build: Build,
        now: datetime,
        root: Path = Path(),
    ) -> PaperRun:
        """Start the run called ``name``, or resume it by replaying its
        journal. A run belongs to one design: resuming under another is
        refused, because the result would be the record of no system."""
        row = conn.execute(select(paper_runs).where(paper_runs.c.name == name)).first()
        if row is None:
            sha, dirty = git_state(root)
            key = conn.execute(
                paper_runs.insert().values(
                    name=name,
                    strategy=strategy,
                    design_hash=design_hash,
                    design=dict(design),
                    git_sha=sha,
                    git_dirty=dirty,
                    created_at=now,
                )
            ).inserted_primary_key
            assert key is not None
            run_id = int(key[0])
        else:
            if row.design_hash != design_hash:
                was, new = _flat(row.design), _flat(design)
                changed = [
                    f"{k}: {was.get(k)!r} -> {new.get(k)!r}"
                    for k in sorted(set(was) | set(new))
                    if was.get(k) != new.get(k)
                ]
                raise DesignChanged(
                    f"paper run {name!r} was started as design {row.design_hash} and the "
                    f"configuration is now {design_hash} ({'; '.join(changed) or 'see the design'}"
                    "). A changed design is a new paper run: give it a new name."
                )
            run_id = int(row.id)
        tape, heard = Tape(), _Heard()
        run = cls(run_id, name, build(tape, heard), tape, heard)
        run._replay(conn)
        return run

    # --- taking inputs ------------------------------------------------------------

    def seen(self, item: Item) -> bool:
        """Has this input been taken already? For a bar that includes any
        bar not newer than the stock's last one: bars are taken in order."""
        if isinstance(item, MarketEvent):
            return item.event_id in self.events
        if isinstance(item, BarEvent):
            last = self.last_bar.get(item.instrument_key)
            return last is not None and item.start <= last
        return item_key(item) in self._keys

    def apply(self, conn: Connection, item: Item, at: datetime, world: World | None) -> Applied:
        """Hand one input to the engine at ``at`` and store, in the caller's
        transaction, the journal row and everything the step caused."""
        if self.seen(item):
            raise ValueError(f"{item_key(item)} was already taken by run {self.name!r}")
        self._tape.begin(world)
        self._feed(item, at)
        kind, payload = encode(item)
        self._seq += 1
        conn.execute(
            paper_journal.insert().values(
                run_id=self.id,
                seq=self._seq,
                kind=kind,
                item_key=item_key(item),
                at=at,
                payload=payload,
                answers=self._tape.answers or None,
            )
        )
        orders, executions = self._heard.take()
        self._store(conn, orders, executions)
        self._note(item, at)
        return Applied(self._seq, orders, executions)

    def _feed(self, item: Item, at: datetime) -> None:
        if isinstance(item, Reference):
            self.engine.reference(item.instrument_key, item.price, item.as_of)
        elif isinstance(item, DayClose):
            if self.engine.state.day != item.day:
                raise ValueError(
                    f"cannot close {item.day}: the engine is on {self.engine.state.day}"
                )
            self.engine.finish()
        else:
            self.engine.step(item, at)

    def _note(self, item: Item, at: datetime) -> None:
        if isinstance(item, MarketEvent):
            self.events.add(item.event_id)
            self.securities[item.instrument_key] = item.security_id
        elif isinstance(item, BarEvent):
            self.last_bar[item.instrument_key] = item.start
        else:
            self._keys.add(item_key(item))
        self.last_at = at

    def _store(self, conn: Connection, new: list[Order], executions: list[Execution]) -> None:
        orders = self.engine.orders
        changed = []
        for order_id in sorted(self._working | {o.order_id for o in new}):
            order = orders[order_id - 1]  # ids are positions in the engine's list
            if self._stored.get(order_id) != _state(order):
                self._stored[order_id] = _state(order)
                changed.append(_order_row(self.id, order))
            if order.status == "working":
                self._working.add(order_id)
            else:
                self._working.discard(order_id)
        repo.upsert(
            conn,
            paper_orders,
            changed,
            ["run_id", "order_id"],
            ["expires_at", "filled", "status", "note"],
        )
        rows = [_fill_row(self.id, self._fills + i, e) for i, e in enumerate(executions, start=1)]
        if rows:
            conn.execute(paper_executions.insert(), rows)
        self._fills += len(rows)

    # --- resuming -----------------------------------------------------------------

    def _replay(self, conn: Connection) -> None:
        rows = conn.execute(
            select(paper_journal)
            .where(paper_journal.c.run_id == self.id)
            .order_by(paper_journal.c.seq)
        ).all()
        for row in rows:
            item = decode(row.kind, row.payload)
            recorded = dict(row.answers or {})
            self._tape.begin(None, recorded)
            try:
                self._feed(item, row.at)
            except ValueError as exc:
                raise Diverged(f"journal step {row.seq} ({row.item_key}): {exc}") from exc
            if set(self._tape.answers) != set(recorded):
                unasked = sorted(set(recorded) - set(self._tape.answers))
                raise Diverged(
                    f"journal step {row.seq} ({row.item_key}): the run asked {unasked} "
                    "and the replay did not"
                )
            self._seq = int(row.seq)
            self._note(item, row.at)
        self._tape.begin(None)
        self._heard.take()
        self._verify(conn)

    def _verify(self, conn: Connection) -> None:
        """The replay must be the run: same orders, same fills."""
        fills = conn.execute(
            select(paper_executions)
            .where(paper_executions.c.run_id == self.id)
            .order_by(paper_executions.c.seq)
        ).all()
        orders = conn.execute(
            select(paper_orders)
            .where(paper_orders.c.run_id == self.id)
            .order_by(paper_orders.c.order_id)
        ).all()
        # Orders first: a different decision explains the different fills after it.
        self._compare("order", orders, [_order_row(self.id, o) for o in self.engine.orders])
        self._compare("fill", fills, [_fill_row(self.id, 0, e) for e in self.engine.executions])
        self._fills = len(self.engine.executions)
        self._stored = {o.order_id: _state(o) for o in self.engine.orders}
        self._working = {o.order_id for o in self.engine.orders if o.status == "working"}

    def _compare(
        self, what: str, stored: Sequence[Row[Any]], replayed: list[dict[str, Any]]
    ) -> None:
        advice = (
            "The code or a configuration file changed since the run recorded this. Its record "
            "stands as it is; continue under the new code as a new paper run (a new name)."
        )
        if len(stored) != len(replayed):
            raise Diverged(
                f"paper run {self.name!r}: {len(stored)} {what}s recorded, the replay gives "
                f"{len(replayed)}. {advice}"
            )
        for number, (row, now) in enumerate(zip(stored, replayed, strict=True), start=1):
            now.pop("seq", None)
            differences = _differences(dict(row._mapping), now)
            if differences:
                raise Diverged(
                    f"paper run {self.name!r}, {what} {number}: {'; '.join(differences)}. {advice}"
                )
