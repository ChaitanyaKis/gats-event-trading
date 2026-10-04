"""The live pilot runtime (M8). Started only by ``gats live``, by a human.

It is the paper runtime with one addition: every order the engine sends is
also sent to the broker as a protected limit order, through the order layer
(:mod:`gats.oms.orders`). The engine stays the decision maker and keeps its
own simulated book, exactly as in paper trading, so the live pilot can be
compared with paper order by order. The real book is the broker's:

- real fills are followed and stored (``live_orders``);
- a real order whose engine order ended without filling is cancelled;
- what the fills add up to is reconciled with the broker's positions;
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
from datetime import datetime

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
from gats.runtime.paper import FATAL, PaperRuntime, System, TickReport

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


class LiveSession:
    """What a live run does after each tick of the engine: mirror the new
    orders, follow them, reconcile with the broker, and tell the human."""

    def __init__(
        self, svc: Services, runtime: PaperRuntime, oms: Oms, alerts: Alerter | None = None
    ) -> None:
        self.svc = svc
        self.runtime = runtime
        self.oms = oms
        self.alerts = alerts or NoAlerts()
        self._refreshed: datetime | None = None
        self._reconciled: datetime | None = None
        self._differing: list[str] = []  # what the last comparison disagreed on
        self._was_halted = oms.halted

    def _due(self, last: datetime | None, every_s: float, now: datetime) -> bool:
        return last is None or (now - last).total_seconds() >= every_s

    async def after_tick(self, report: TickReport) -> None:
        name, oms = self.runtime.name, self.oms
        engine = self.runtime.run.engine
        for order in report.orders:
            if order.status == "rejected":
                continue  # the risk engine refused it: nothing to send
            state = await oms.submit(order, engine.day_loss())
            await self.alerts.send(
                f"[{name}] REAL ORDER {state}: {order.signal.side} {order.quantity} "
                f"{order.signal.instrument_key} limit {order.limit:.2f}"
            )
        for message in tick_messages(name, [], report.fills, report.errors):
            await self.alerts.send(message)
        now = self.svc.clock()
        if oms.open_orders() and self._due(self._refreshed, _REFRESH_S, now):
            self._refreshed = now
            await oms.cancel_expired(engine.orders)
            await oms.refresh()
        if self._due(self._reconciled, _RECONCILE_S, now):
            self._reconciled = now
            # One disagreement can be a fill that landed between two
            # questions. The same one twice in a row is a real difference.
            differing = [str(m) for m in await oms.differences()]
            if differing and differing == self._differing:
                oms.halt("reconcile", "; ".join(differing))
            self._differing = differing
        if oms.halted and not self._was_halted:
            self._was_halted = True
            await self.alerts.send(
                f"[{name}] LIVE TRADING SWITCHED OFF. No new entries until a person removes "
                f"{oms.kill_switch}; see docs/RUNBOOK_LIVE.md."
            )
        if report.closed is not None:
            await self.alerts.send(daily_summary(name, report.closed, engine.result()))


async def run_live(
    svc: Services,
    runtime: PaperRuntime,
    oms: Oms,
    stop: asyncio.Event,
    alerts: Alerter | None = None,
) -> None:
    """Tick, then mirror, follow and reconcile, until told to stop."""
    session = LiveSession(svc, runtime, oms, alerts)
    design = runtime.system.design_hash
    log.warning("LIVE run starting %s", kv(run=runtime.name, design=design))
    await session.alerts.send(f"[{runtime.name}] LIVE trading started (design {design})")
    failures = 0
    try:
        while not stop.is_set():
            delay = svc.settings.paper_poll_s
            try:
                await session.after_tick(await runtime.tick())
                failures = 0
            except FATAL:
                raise
            except Exception as exc:
                # Keep going: stopping here would leave open positions with
                # nothing sending their exits. The human is told at once.
                failures += 1
                log.exception("live tick failed %s", kv(run=runtime.name, failures=failures))
                await session.alerts.send(
                    f"[{runtime.name}] ERROR: live tick failed: {type(exc).__name__}: {exc}"[:500]
                )
                delay = min(svc.settings.job_error_backoff_max_s, delay * 2 ** min(failures, 10))
            await sleep_or_stop(stop, delay)
    finally:
        await runtime.aclose()
        log.warning("LIVE run stopped %s", kv(run=runtime.name))
        await session.alerts.send(f"[{runtime.name}] LIVE trading stopped")
