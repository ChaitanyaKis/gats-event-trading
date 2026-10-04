"""The live pilot loop end to end (M8): the paper engine decides, the order
layer mirrors its orders to a broker, follows them and reconciles.

The broker is a fake in this file: no test can send a real order. The
market and the filing are the ones of the paper runtime's tests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import respx
from sqlalchemy import select

from gats.broker.base import BrokerError, BrokerOrder, BrokerPosition, BrokerState
from gats.db.schema import live_breaches, live_orders
from gats.ingest import Services
from gats.oms import gate
from gats.oms.caps import LiveCaps
from gats.oms.orders import Oms
from gats.runtime.live import LiveSession, run_live
from gats.runtime.paper import PaperRuntime, System
from tests.conftest import FakeClock
from tests.test_paper_runtime import (
    DAY,
    KEY,
    Broker,
    at,
    file_order_win,
    make_broker,
    make_system,
    mock_http,
)

CAPS = LiveCaps(
    max_capital_rs=100_000,
    max_daily_loss_rs=5_000,
    max_position_rs=60_000,
    max_orders_per_day=10,
    max_excess_slippage_bps=500,  # the fake broker fills at the limit: about 190 bps worse
)


@pytest.fixture
def broker(svc: Services, clock: FakeClock) -> Broker:
    return make_broker(svc, clock)


@pytest.fixture
def system(svc: Services, tmp_path: Path) -> System:
    return make_system(svc, tmp_path)


class SimBroker:
    """Fills every order at once at its limit: far simpler than a market,
    enough to follow the plumbing. ``fill = False`` leaves orders open."""

    def __init__(self) -> None:
        self.placed: list[BrokerOrder] = []
        self.cancelled: list[str] = []
        self.book: dict[str, int] = {}
        self.extra: list[BrokerPosition] = []
        self.fill = True
        self.filled: set[str] = set()

    async def place(self, order: BrokerOrder) -> str:
        self.placed.append(order)
        order_id = f"B{len(self.placed)}"
        if self.fill:
            signed = order.quantity if order.side == "buy" else -order.quantity
            self.book[order.instrument_key] = self.book.get(order.instrument_key, 0) + signed
            self.filled.add(order_id)
        return order_id

    async def cancel(self, order_id: str) -> None:
        if order_id in self.filled:
            raise BrokerError(f"cancel {order_id}: already complete")
        self.cancelled.append(order_id)

    async def order(self, order_id: str) -> BrokerState:
        order = self.placed[int(order_id[1:]) - 1]
        if order_id in self.filled:
            return BrokerState(
                order_id, "complete", order.quantity, 0, order.limit_price, order.client_id, None
            )
        return BrokerState(order_id, "open", 0, order.quantity, None, order.client_id, None)

    async def positions(self) -> list[BrokerPosition]:
        mine = [BrokerPosition(key, "I", n, 0.0) for key, n in self.book.items() if n]
        return mine + self.extra


class Heard:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


def live(
    svc: Services, system: System, tmp_path: Path, caps: LiveCaps = CAPS
) -> tuple[LiveSession, SimBroker, Heard]:
    runtime = PaperRuntime(svc, system, "pilot")
    report = tmp_path / "M7_paper.md"
    report.write_text("**G3: PASS**\n", encoding="utf-8", newline="\n")
    with svc.engine.begin() as conn:
        approval = gate.approve(
            conn, design_hash=system.design_hash, caps=caps, report=report,
            typed=gate.confirmation(system.design_hash, caps), now=svc.clock(), valid_days=30,
        )  # fmt: skip
    sim, heard = SimBroker(), Heard()
    assert system.kill_switch is not None
    oms = Oms(
        svc.engine, sim, caps, approval_id=approval, run_id=runtime.run.id,
        kill_switch=system.kill_switch, clock=svc.clock, done_statuses={"complete": "filled"},
    )  # fmt: skip
    return LiveSession(svc, runtime, oms, heard), sim, heard


async def session_until(
    session: LiveSession, clock: FakeClock, start: datetime, end: datetime, svc: Services
) -> None:
    """Tick every 15 s of fake time; an order win is filed at 10:00:30."""
    clock.now = start
    while clock.now <= end:
        if clock.now == at(10, 0, 30):
            await file_order_win(svc)
        await session.after_tick(await session.runtime.tick())
        clock.now += timedelta(seconds=15)


def real_orders(svc: Services) -> list[tuple[str, int, str, int]]:
    with svc.engine.begin() as conn:
        rows = conn.execute(select(live_orders).order_by(live_orders.c.engine_order_id)).all()
    return [(r.side, r.quantity, r.state, r.filled_quantity) for r in rows]


@respx.mock
async def test_a_live_session_mirrors_the_engine_and_ends_flat(
    svc: Services,
    clock: FakeClock,
    broker: Broker,
    system: System,
    tmp_path: Path,
) -> None:
    mock_http(svc, broker)
    session, sim, heard = live(svc, system, tmp_path)
    await session_until(session, clock, at(10, 0), at(11, 5), svc)
    engine = session.runtime.run.engine
    entry, exit_ = engine.orders
    assert [(o.side, o.quantity, o.limit_price) for o in sim.placed] == [
        ("buy", 497, entry.limit),  # the engine's own protected limit, as a real LIMIT order
        ("sell", 497, exit_.limit),
    ]
    assert [o.client_id for o in sim.placed] == [
        f"gats-{session.runtime.run.id}-1",
        f"gats-{session.runtime.run.id}-2",
    ]
    assert real_orders(svc) == [("buy", 497, "filled", 497), ("sell", 497, "filled", 497)]
    assert session.oms.held() == {} and sim.book == {KEY: 0} and not session.oms.halted
    await session_until(session, clock, at(15, 35), at(15, 35), svc)  # the close
    told = "\n".join(heard.messages)
    assert f"REAL ORDER sent: buy 497 {KEY}" in told and f"REAL ORDER sent: sell 497 {KEY}" in told
    assert "FILL: buy 497" in told and f"[pilot] {DAY}: equity Rs" in told
    assert "SWITCHED OFF" not in told and sim.cancelled == []


@respx.mock
async def test_a_cap_breach_sends_nothing_switches_off_and_never_sells_short(
    svc: Services,
    clock: FakeClock,
    broker: Broker,
    system: System,
    tmp_path: Path,
) -> None:
    mock_http(svc, broker)
    small = CAPS.model_copy(update={"max_position_rs": 10_000})  # the engine wants Rs 50,000
    session, sim, heard = live(svc, system, tmp_path, small)
    await session_until(session, clock, at(10, 0), at(11, 5), svc)
    assert sim.placed == []  # neither the oversized entry nor an exit of shares never bought
    assert real_orders(svc) == [("buy", 497, "refused", 0), ("sell", 497, "refused", 0)]
    assert session.oms.halted and session.oms.held() == {}
    with svc.engine.begin() as conn:
        (stop,) = conn.execute(select(live_breaches)).all()
        notes = dict(conn.execute(select(live_orders.c.side, live_orders.c.message)).all())
    assert stop.kind == "cap" and "position size" in stop.detail
    assert notes["sell"] == "nothing held at the broker to sell"
    told = "\n".join(heard.messages)
    assert "REAL ORDER refused: buy 497" in told and told.count("SWITCHED OFF") == 1


@respx.mock
async def test_an_unfilled_real_order_is_cancelled_when_the_engine_moves_on(
    svc: Services,
    clock: FakeClock,
    broker: Broker,
    system: System,
    tmp_path: Path,
) -> None:
    """The simulation fills and later exits; the real order just sits. It
    must not be left working behind the engine's back, and the exit must
    not sell what was never bought."""
    mock_http(svc, broker)
    session, sim, _ = live(svc, system, tmp_path)
    sim.fill = False
    await session_until(session, clock, at(10, 0), at(10, 10), svc)
    assert real_orders(svc) == [("buy", 497, "sent", 0)] and sim.cancelled == []
    entry = session.runtime.run.engine.orders[0]
    assert entry.status == "filled"  # in the simulation only
    entry.status = "expired"  # as if its time had run out there too
    assert await session.oms.cancel_expired([entry]) == 1
    assert sim.cancelled == ["B1"] and real_orders(svc) == [("buy", 497, "cancelled", 0)]
    assert await session.oms.cancel_expired([entry]) == 0  # once
    assert not session.oms.halted and session.oms.sellable(KEY) == 0


@respx.mock
async def test_a_position_nobody_sent_switches_trading_off_on_the_second_look(
    svc: Services,
    clock: FakeClock,
    broker: Broker,
    system: System,
    tmp_path: Path,
) -> None:
    mock_http(svc, broker)
    session, sim, heard = live(svc, system, tmp_path)
    sim.extra = [BrokerPosition("NSE_EQ|INE000000X01", "D", 25, 100.0)]
    clock.now = at(9, 30)
    await session.after_tick(await session.runtime.tick())
    assert not session.oms.halted  # once could be a fill between two questions
    clock.now = at(9, 30, 45)
    await session.after_tick(await session.runtime.tick())
    assert not session.oms.halted  # not compared again within the minute
    clock.now = at(9, 31, 5)
    await session.after_tick(await session.runtime.tick())
    assert session.oms.halted
    with svc.engine.begin() as conn:
        (stop,) = conn.execute(select(live_breaches)).all()
    assert stop.kind == "reconcile" and "the broker says 25" in stop.detail
    assert sum("SWITCHED OFF" in m for m in heard.messages) == 1


async def test_the_live_loop_keeps_ticking_after_a_failure_and_says_so(
    svc: Services,
    system: System,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc.settings.paper_poll_s = 0.001
    session, _, heard = live(svc, system, tmp_path)
    stop = asyncio.Event()
    calls: list[int] = []
    tick = session.runtime.tick

    async def flaky():  # type: ignore[no-untyped-def]
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        if len(calls) == 4:
            stop.set()
        return await tick()

    monkeypatch.setattr(session.runtime, "tick", flaky)
    await asyncio.wait_for(run_live(svc, session.runtime, session.oms, stop, heard), 5)
    assert len(calls) == 4  # an open position must keep getting its exits
    assert heard.messages[0].startswith("[pilot] LIVE trading started")
    assert any("live tick failed: RuntimeError: database is locked" in m for m in heard.messages)
    assert heard.messages[-1] == "[pilot] LIVE trading stopped"


@respx.mock
async def test_real_fills_much_worse_than_the_model_switch_trading_off(
    svc: Services, clock: FakeClock, broker: Broker, system: System, tmp_path: Path
) -> None:
    """The fake broker fills at the limit, about 2% away from the price the
    order was decided on, where the simulation fills within a few basis
    points. Two such orders against a 50 bps tolerance: stop."""
    mock_http(svc, broker)
    tight = CAPS.model_copy(update={"max_excess_slippage_bps": 50})
    session, _, heard = live(svc, system, tmp_path, tight)
    session.slippage_window = 2
    await session_until(session, clock, at(10, 0), at(10, 30), svc)
    (entry_only,) = session.excess_slippage_bps()
    assert 150 < entry_only < 250 and not session.oms.halted  # one order is not a pattern
    await session_until(session, clock, at(10, 30, 15), at(11, 6), svc)
    worse = session.excess_slippage_bps()
    assert len(worse) == 2 and all(150 < w < 250 for w in worse)
    assert session.oms.halted
    with svc.engine.begin() as conn:
        (stop,) = conn.execute(select(live_breaches)).all()
    assert stop.kind == "slippage"
    assert "bps worse than the model over the last 2 orders (tolerance 50 bps)" in stop.detail
    assert sum("SWITCHED OFF" in m for m in heard.messages) == 1
