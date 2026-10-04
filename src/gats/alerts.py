"""Alerts to the human (T7.3): signals, fills, errors and a daily summary.

Telegram's Bot API (verified from core.telegram.org/bots/api, 2026-10-04):
``POST https://api.telegram.org/bot<token>/sendMessage`` with a JSON body
``{"chat_id", "text"}``; the reply always has a Boolean ``ok`` and, on
failure, a ``description``.

The bot token is part of the URL, so the URL never reaches a log line or an
error message: requests carry a label instead. An alert that cannot be sent
is logged and dropped: alerting must never stop the thing it reports on.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from typing import Protocol

from gats.backtest.engine import BacktestResult, Execution, Order
from gats.ingest import Services
from gats.logging_setup import kv
from gats.net import FetchError
from gats.timeutil import to_ist

log = logging.getLogger(__name__)
_LABEL = "telegram:sendMessage"


class Alerter(Protocol):
    async def send(self, text: str) -> bool:
        """True if the message was delivered."""
        ...


class NoAlerts:
    """Alerts are not configured: say so once, then stay quiet."""

    def __init__(self) -> None:
        self._said = False

    async def send(self, text: str) -> bool:
        if not self._said:
            self._said = True
            log.warning(
                "alerts are off: set GATS_TELEGRAM_BOT_TOKEN and GATS_TELEGRAM_CHAT_ID in .env"
            )
        return False


class TelegramAlerter:
    def __init__(self, svc: Services, repeat_after_s: float = 600.0) -> None:
        self._svc = svc
        self._repeat_after = timedelta(seconds=repeat_after_s)
        self._sent: dict[str, datetime] = {}  # text -> when, to not repeat a stuck error

    async def send(self, text: str) -> bool:
        settings = self._svc.settings
        token, chat = settings.telegram_bot_token, settings.telegram_chat_id
        if token is None or not chat:
            return False
        now = self._svc.clock()
        last = self._sent.get(text)
        if last is not None and now - last < self._repeat_after:
            return False  # the same words, minutes ago: once is enough
        self._sent = {t: at for t, at in self._sent.items() if now - at < self._repeat_after}
        self._sent[text] = now
        url = f"{settings.telegram_api_base.rstrip('/')}/bot{token.get_secret_value()}/sendMessage"
        body = {"chat_id": chat, "text": text[: settings.alerts_max_chars]}
        try:
            got = await self._svc.client.post_json(url, body, label=_LABEL, retries=1)
        except FetchError as exc:
            log.warning("alert not sent %s", kv(error=str(exc)))
            return False
        try:
            reply = json.loads(got.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            reply = {}
        if not (got.ok and isinstance(reply, dict) and reply.get("ok") is True):
            why = reply.get("description") if isinstance(reply, dict) else None
            log.warning("alert refused %s", kv(status=got.status, why=str(why)[:200]))
            return False
        return True


def alerter(svc: Services) -> Alerter:
    s = svc.settings
    if s.telegram_bot_token is None or not s.telegram_chat_id:
        return NoAlerts()
    return TelegramAlerter(svc)


def tick_messages(
    run: str, orders: Sequence[Order], fills: Sequence[Execution], errors: Sequence[str]
) -> list[str]:
    """One message per order, fill and error of a tick."""
    out = []
    for order in orders:
        signal = order.signal
        head = "REFUSED" if order.status == "rejected" else "SIGNAL"
        why = f" ({order.note})" if order.note else ""
        out.append(
            f"[{run}] {head}: {signal.side} {order.quantity} {signal.instrument_key} "
            f"limit {order.limit:.2f}{why}\n{signal.reason}"
        )
    for fill in fills:
        out.append(
            f"[{run}] FILL: {fill.side} {fill.quantity} {fill.instrument_key} @ {fill.price:.2f} "
            f"at {to_ist(fill.at):%H:%M} (charges Rs {fill.charges.total:.2f})\n{fill.reason}"
        )
    out += [f"[{run}] ERROR: {error}"[:500] for error in errors]
    return out


def daily_summary(run: str, day: date, result: BacktestResult) -> str:
    """The day in a few lines: equity, its change, and what traded."""
    days = sorted(result.equity)
    equity = result.equity.get(day, result.cash)
    before = [d for d in days if d < day]
    base = result.equity[before[-1]] if before else result.initial_cash
    todays = [e for e in result.executions if to_ist(e.at).date() == day]
    sent = [o for o in result.orders if to_ist(o.submitted_at).date() == day]
    refused = sum(o.status == "rejected" for o in sent)
    held = (
        ", ".join(f"{k} x {p.quantity}" for k, p in sorted(result.positions.items())) or "nothing"
    )
    return (
        f"[{run}] {day}: equity Rs {equity:,.0f} ({equity - base:+,.0f} today, "
        f"{equity - result.initial_cash:+,.0f} since the start)\n"
        f"orders {len(sent)} ({refused} refused), fills {len(todays)}, "
        f"charges Rs {sum(e.charges.total for e in todays):,.2f}\nholding: {held}"
    )
