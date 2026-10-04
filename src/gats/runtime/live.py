"""The live pilot runtime (M8). Started only by ``gats live``, by a human.

It is the paper runtime with one addition: every order the engine sends is
also sent to the broker as a protected limit order, through the order layer
(:mod:`gats.oms.orders`). The engine stays the decision maker and keeps its
own simulated book, exactly as in paper trading, so the live pilot can be
compared with paper order by order. The real book is the broker's:

- real fills are followed and stored (``live_orders``);
- what they add up to is reconciled with the broker's positions;
- a cap breach, an order with no answer, or a book that does not match the
  broker switches trading off (the kill switch) and alerts the human.

A pilot design, deliberately simple: when the real fill differs from the
simulated one (a partial fill, no fill), the two books drift, and the
system does not try to be clever about it. It stops and asks.

Nothing in this module runs unless :func:`problems` returns an empty list.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import Connection

from gats.alerts import Alerter, NoAlerts, daily_summary, tick_messages
from gats.broker.upstox import UpstoxBroker
from gats.config import Settings
from gats.ingest import Services
from gats.logging_setup import kv
from gats.oms import gate
from gats.oms.caps import LiveSpec
from gats.oms.orders import Oms
from gats.recorder import sleep_or_stop
from gats.runtime.paper import PaperRuntime, System

log = logging.getLogger(__name__)
_REFRESH_S = 5.0  # between questions about working orders
_RECONCILE_S = 60.0  # between comparisons with the broker's positions


def problems(
    conn: Connection, settings: Settings, system: System, spec: LiveSpec, now: datetime
) -> list[str]:
    """Every reason live trading may not start. Empty means every lock is
    open: the switch, the caps, gate G3, the human's approval, the token."""
    found = []
    if not settings.live_enabled:
        found.append("live trading is off (GATS_LIVE_ENABLED is not true)")
    found += gate.problems(
        conn,
        design_hash=system.design_hash,
        caps=spec.caps,
        report=settings.g3_report_path,
        now=now,
    )
    token = settings.upstox_access_token
    if token is None or not token.get_secret_value().strip():
        found.append("no broker access token (GATS_UPSTOX_ACCESS_TOKEN)")
    return found


def build_oms(svc: Services, runtime: PaperRuntime, spec: LiveSpec, approval: int) -> Oms:
    switch = runtime.system.kill_switch
    if switch is None:
        raise RuntimeError("live trading needs a kill switch file in the risk limits")
    return Oms(
        svc.engine,
        UpstoxBroker(svc),
        spec.caps,
        approval_id=approval,
        run_id=runtime.run.id,
        kill_switch=switch,
        clock=svc.clock,
        done_statuses=spec.order_end_statuses,
    )


async def run_live(
    svc: Services,
    runtime: PaperRuntime,
    oms: Oms,
    stop: asyncio.Event,
    alerts: Alerter | None = None,
) -> None:
    """Tick, mirror new orders to the broker, follow them, reconcile."""
    alerts = alerts or NoAlerts()
    settings = svc.settings
    refreshed = reconciled = svc.clock() - timedelta(days=1)
    was_halted = oms.halted
    log.warning("LIVE run starting %s", kv(run=runtime.name, design=runtime.system.design_hash))
    await alerts.send(
        f"[{runtime.name}] LIVE trading started (design {runtime.system.design_hash})"
    )
    try:
        while not stop.is_set():
            report = await runtime.tick()
            engine = runtime.run.engine
            for order in report.orders:
                if order.status == "rejected":
                    continue  # the risk engine refused it: nothing to send
                state = await oms.submit(order, engine.day_loss())
                await alerts.send(
                    f"[{runtime.name}] REAL ORDER {state}: {order.signal.side} {order.quantity} "
                    f"{order.signal.instrument_key} limit {order.limit:.2f}"
                )
            for message in tick_messages(runtime.name, [], report.fills, report.errors):
                await alerts.send(message)
            now = svc.clock()
            if (now - refreshed).total_seconds() >= _REFRESH_S and oms.open_orders():
                refreshed = now
                await oms.refresh()
            if (now - reconciled).total_seconds() >= _RECONCILE_S:
                reconciled = now
                await oms.check_positions()
            if oms.halted and not was_halted:
                was_halted = True
                await alerts.send(
                    f"[{runtime.name}] LIVE TRADING SWITCHED OFF: see `gats live status`. "
                    f"No new entries until a person removes {oms.kill_switch}."
                )
            if report.closed is not None:
                await alerts.send(daily_summary(runtime.name, report.closed, engine.result()))
            await sleep_or_stop(stop, settings.paper_poll_s)
    finally:
        await runtime.aclose()
        log.warning("LIVE run stopped %s", kv(run=runtime.name))
        await alerts.send(f"[{runtime.name}] LIVE trading stopped")
