"""The live pilot's locks (M8): the gate, the caps, the order layer, the
broker adapter and the alerts. No network and no broker: respx and a fake.

Broker replies used here are the examples in the broker's documentation,
not real replies: an order API cannot be probed without placing an order.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import Engine as Database
from sqlalchemy import select
from typer.testing import CliRunner

from gats.alerts import NoAlerts, TelegramAlerter, alerter, tick_messages
from gats.backtest.engine import Order
from gats.broker import upstox
from gats.broker.base import (
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerState,
    OutcomeUnknown,
)
from gats.cli import app
from gats.db.schema import live_approvals, live_breaches, live_orders, paper_runs
from gats.ingest import Services
from gats.net import FetchError
from gats.oms import gate
from gats.oms.caps import Account, LiveCaps, breach, load_live
from gats.oms.orders import IllegalTransition, Oms, advance, client_id, reconcile, state_of
from gats.strategy.base import Signal

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 10, 5, 4, 30, tzinfo=UTC)  # 10:00 IST, a Monday
KEY = "NSE_EQ|INE062A01020"
CAPS = LiveCaps(
    max_capital_rs=20_000, max_daily_loss_rs=500, max_position_rs=10_000, max_orders_per_day=6
)
DESIGN = "abcdef0123456789"


# --- caps ------------------------------------------------------------------------


def test_the_shipped_caps_are_unset_so_live_cannot_start() -> None:
    spec = load_live(ROOT / "configs" / "live.yaml")
    assert spec.caps.unset() == [
        "max_capital_rs", "max_daily_loss_rs", "max_position_rs", "max_orders_per_day",
    ]  # fmt: skip
    assert spec.order_end_statuses == {"complete": "filled"} and CAPS.unset() == []
    assert CAPS.digest != CAPS.model_copy(update={"max_capital_rs": 20_001}).digest


def test_each_cap_names_itself_and_exits_stay_possible() -> None:
    calm = Account(deployed_rs=5_000, in_stock_rs=0, day_loss_rs=0, orders_today=1)
    assert breach(CAPS, 5_000, False, calm) is None
    assert "position size" in (breach(CAPS, 10_001, False, calm) or "")
    full = Account(deployed_rs=16_000, in_stock_rs=0, day_loss_rs=0, orders_today=1)
    assert "capital" in (breach(CAPS, 5_000, False, full) or "")
    losing = Account(deployed_rs=0, in_stock_rs=0, day_loss_rs=500, orders_today=1)
    assert "daily loss" in (breach(CAPS, 1_000, False, losing) or "")
    assert breach(CAPS, 1_000, True, losing) is None  # getting out is never capped by size or loss
    busy = Account(deployed_rs=0, in_stock_rs=0, day_loss_rs=0, orders_today=6)
    assert "order count" in (breach(CAPS, 1_000, True, busy) or "")  # a runaway loop is stopped


# --- the gate --------------------------------------------------------------------


def report(tmp_path: Path, verdict: str | None = "PASS") -> Path:
    path = tmp_path / "M7_paper.md"
    line = f"**G3: {verdict}**\n" if verdict else "no verdict here\n"
    path.write_text(f"# G3 paper report\n\n{line}\ndetails\n", encoding="utf-8", newline="\n")
    return path


def test_every_missing_piece_is_named(engine: Database, tmp_path: Path) -> None:
    unset = LiveCaps(max_capital_rs=0, max_daily_loss_rs=0, max_position_rs=0, max_orders_per_day=0)
    with engine.begin() as conn:
        found = gate.problems(
            conn, design_hash=DESIGN, caps=unset, report=tmp_path / "none.md", now=NOW
        )
    assert len(found) == 3
    assert "caps are not set" in found[0] and "no G3 verdict" in found[1]
    assert "no approval" in found[2] and "gats gate approve" in found[2]
    assert gate.g3_verdict(report(tmp_path, None)) is None
    with engine.begin() as conn:
        failed = gate.problems(
            conn, design_hash=DESIGN, caps=CAPS, report=report(tmp_path, "FAIL"), now=NOW
        )
    assert any("did not pass" in p for p in failed)


def test_an_approval_needs_the_exact_sentence_and_a_passed_gate(
    engine: Database, tmp_path: Path
) -> None:
    passed = report(tmp_path)
    phrase = gate.confirmation(DESIGN, CAPS)
    assert phrase == f"I approve live trading of design {DESIGN} with at most Rs 20,000 at risk"

    def approve(typed: str, caps: LiveCaps = CAPS, path: Path = passed) -> int:
        with engine.begin() as conn:
            return gate.approve(
                conn, design_hash=DESIGN, caps=caps, report=path, typed=typed, now=NOW,
                valid_days=30,
            )  # fmt: skip

    for wrong in ("", "yes", phrase.lower(), phrase.replace("20,000", "200,000")):
        with pytest.raises(gate.ApprovalRefused, match="not typed exactly"):
            approve(wrong)
    with pytest.raises(gate.ApprovalRefused, match="G3 has not passed"):
        approve(phrase, path=report(tmp_path, "FAIL"))
    with pytest.raises(gate.ApprovalRefused, match="caps are not set"):
        approve(phrase, caps=CAPS.model_copy(update={"max_daily_loss_rs": 0}))
    with engine.begin() as conn:
        assert conn.execute(select(live_approvals)).all() == []  # nothing was recorded

    number = approve(phrase, path=report(tmp_path))
    with engine.begin() as conn:
        row = conn.execute(select(live_approvals)).one()
        ok = gate.problems(conn, design_hash=DESIGN, caps=CAPS, report=passed, now=NOW)
        assert gate.approval_id(conn, DESIGN, CAPS) == number
    assert ok == [] and row.confirmation == phrase and row.caps["max_capital_rs"] == 20_000
    assert row.expires_at == NOW + timedelta(days=30) and row.created_by


def test_an_approval_covers_one_design_one_set_of_caps_one_report_for_a_while(
    engine: Database, tmp_path: Path
) -> None:
    passed = report(tmp_path)
    with engine.begin() as conn:
        number = gate.approve(
            conn, design_hash=DESIGN, caps=CAPS, report=passed,
            typed=gate.confirmation(DESIGN, CAPS), now=NOW, valid_days=30,
        )  # fmt: skip

    def problems(**changes: Any) -> list[str]:
        args: dict[str, Any] = {"design_hash": DESIGN, "caps": CAPS, "report": passed, "now": NOW}
        with engine.begin() as conn:
            return gate.problems(conn, **(args | changes))

    assert problems() == []
    assert "no approval" in problems(design_hash="another-design")[0]
    raised = CAPS.model_copy(update={"max_capital_rs": 50_000})
    assert "no approval" in problems(caps=raised)[0]  # a raised cap is a new decision
    assert "expired" in problems(now=NOW + timedelta(days=31))[0]
    passed.write_text(passed.read_text("utf-8") + "\nedited\n", encoding="utf-8", newline="\n")
    assert "report changed" in problems()[0]
    with engine.begin() as conn:
        assert gate.revoke(conn, number, "pilot over", NOW)
        assert not gate.revoke(conn, number, "again", NOW)
    assert any("revoked: pilot over" in p for p in problems())


runner = CliRunner()


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with the real configs and no .env."""
    shutil.copytree(ROOT / "configs", tmp_path / "configs")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GATS_DATA_DIR", str(tmp_path / "data"))
    for name in ("GATS_LIVE_ENABLED", "GATS_UPSTOX_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def test_gats_live_refuses_and_says_why(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = runner.invoke(app, ["live", "--name", "pilot"])
    assert result.exit_code == 1
    out = result.stdout
    assert "REFUSED: live trading may not start" in out
    for reason in (
        "live trading is off",
        "caps are not set",
        "no G3 verdict",
        "no approval",
        "no broker access token",
    ):
        assert reason in out
    # Switching live on is not enough: the human's approval is still missing.
    monkeypatch.setenv("GATS_LIVE_ENABLED", "true")
    monkeypatch.setenv("GATS_UPSTOX_ACCESS_TOKEN", "not-a-real-token")
    again = runner.invoke(app, ["live", "--name", "pilot"])
    assert again.exit_code == 1 and "live trading is off" not in again.stdout
    assert "no approval" in again.stdout and "not-a-real-token" not in again.stdout
    assert not (workdir / "data" / "KILL").exists()


def test_gats_gate_approve_refuses_without_a_person_at_a_terminal(workdir: Path) -> None:
    result = runner.invoke(app, ["gate", "approve"], input="I approve everything\n")
    assert result.exit_code == 1
    assert "REFUSED: this needs an interactive terminal" in result.stdout
    status = runner.invoke(app, ["gate", "status"])
    assert status.exit_code == 0 and "may NOT start" in status.stdout


# --- the order layer ---------------------------------------------------------------


class FakeBroker:
    def __init__(self) -> None:
        self.placed: list[BrokerOrder] = []
        self.fail: Exception | None = None
        self.reports: dict[str, BrokerState] = {}
        self.held: list[BrokerPosition] = []

    async def place(self, order: BrokerOrder) -> str:
        self.placed.append(order)
        if self.fail is not None:
            raise self.fail
        return f"B{len(self.placed)}"

    async def cancel(self, order_id: str) -> None:
        raise AssertionError("not used")

    async def order(self, order_id: str) -> BrokerState:
        return self.reports[order_id]

    async def positions(self) -> list[BrokerPosition]:
        return self.held


def engine_order(order_id: int, quantity: int = 10, limit: float = 500.0, **kw: Any) -> Order:
    side = kw.pop("side", "buy")
    signal = Signal(KEY, side, "intraday", "test", NOW, closes=side == "sell")
    return Order(order_id, signal, quantity, limit, NOW, NOW)


@pytest.fixture
def oms(engine: Database, tmp_path: Path) -> Oms:
    with engine.begin() as conn:
        conn.execute(
            paper_runs.insert().values(
                id=1, name="pilot", strategy="s", design_hash=DESIGN, design={}, created_at=NOW
            )
        )
        approval = gate.approve(
            conn, design_hash=DESIGN, caps=CAPS, report=report(tmp_path),
            typed=gate.confirmation(DESIGN, CAPS), now=NOW, valid_days=30,
        )  # fmt: skip
    return Oms(
        engine, FakeBroker(), CAPS, approval_id=approval, run_id=1,
        kill_switch=tmp_path / "data" / "KILL", clock=lambda: NOW,
        done_statuses={"complete": "filled"},
    )  # fmt: skip


def stored(db: Database) -> list[Any]:
    with db.begin() as conn:
        return list(conn.execute(select(live_orders).order_by(live_orders.c.created_at)).all())


async def test_an_order_is_written_down_sent_once_and_followed(oms: Oms, engine: Database) -> None:
    broker: Any = oms.broker
    assert await oms.submit(engine_order(1), 0.0) == "sent"
    assert await oms.submit(engine_order(1), 0.0) == "sent"  # a restart replays it: not resent
    assert len(broker.placed) == 1
    sent = broker.placed[0]
    assert (sent.client_id, sent.quantity, sent.limit_price) == ("gats-1-1", 10, 500.0)
    (row,) = stored(engine)
    assert (row.state, row.broker_order_id, row.filled_quantity) == ("sent", "B1", 0)
    assert oms.open_orders() == 1 and oms.held() == {}

    broker.reports["B1"] = BrokerState("B1", "open", 4, 6, 499.5, "gats-1-1", None)
    assert await oms.refresh() == 1 and oms.held() == {KEY: 4}  # a partial fill
    broker.reports["B1"] = BrokerState("B1", "complete", 10, 0, 499.8, "gats-1-1", None)
    assert await oms.refresh() == 1 and await oms.refresh() == 0
    (row,) = stored(engine)
    assert (row.state, row.filled_quantity, row.average_price) == ("filled", 10, 499.8)
    broker.held = [BrokerPosition(KEY, "I", 10, 499.8)]
    assert await oms.check_positions() == [] and not oms.halted


async def test_an_order_with_no_answer_is_never_resent_and_switches_trading_off(
    oms: Oms, engine: Database
) -> None:
    broker: Any = oms.broker
    broker.fail = OutcomeUnknown("place gats-1-1: ReadTimeout")
    assert await oms.submit(engine_order(1), 0.0) == "unknown"
    assert await oms.submit(engine_order(1), 0.0) == "unknown" and len(broker.placed) == 1
    assert oms.halted and "unknown_order" in oms.kill_switch.read_text("utf-8")
    with engine.begin() as conn:
        (halt,) = conn.execute(select(live_breaches)).all()
    assert halt.kind == "unknown_order" and "may exist there" in halt.detail
    broker.fail = None
    assert await oms.submit(engine_order(2), 0.0) == "refused"  # no entries while switched off
    assert await oms.submit(engine_order(3, side="sell"), 0.0) == "sent"  # exits still go out
    assert [o.client_id for o in broker.placed] == ["gats-1-1", "gats-1-3"]


async def test_a_refusal_by_the_broker_is_final_and_a_cap_breach_switches_off(
    oms: Oms, engine: Database
) -> None:
    broker: Any = oms.broker
    broker.fail = BrokerError("place: refused: insufficient funds")
    assert await oms.submit(engine_order(1), 0.0) == "rejected" and not oms.halted
    broker.fail = None
    assert await oms.submit(engine_order(2, quantity=30), 0.0) == "refused"  # Rs 15,000 in a stock
    assert oms.halted and len(broker.placed) == 1  # the oversized order never left
    rows = {r.client_id: r for r in stored(engine)}
    assert "position size" in rows["gats-1-2"].message and rows["gats-1-1"].state == "rejected"


async def test_a_book_that_differs_from_the_brokers_switches_trading_off(oms: Oms) -> None:
    broker: Any = oms.broker
    broker.held = [BrokerPosition("NSE_EQ|INE000000X01", "D", 25, 100.0)]  # nothing we sent
    (mismatch,) = await oms.check_positions()
    assert str(mismatch) == "NSE_EQ|INE000000X01: we hold 0, the broker says 25"
    assert oms.halted and "reconcile" in oms.kill_switch.read_text("utf-8")


def test_the_state_machine_and_its_helpers() -> None:
    assert advance("intent", "sent") == "sent" and advance("sent", "filled") == "filled"
    for state, new in [("filled", "sent"), ("refused", "sent"), ("sent", "intent"), ("x", "sent")]:
        with pytest.raises(IllegalTransition):
            advance(state, new)
    assert client_id(3, 17) == "gats-3-17"
    working = BrokerState("B1", "open", 3, 7, 10.0, None, None)
    assert state_of(working, 10, {"complete": "filled"}) == "sent"
    assert state_of(working, 3, {}) == "filled"  # the quantity decides, whatever the word
    gone = BrokerState("B1", "Cancelled", 0, 10, None, None, None)
    assert state_of(gone, 10, {"cancelled": "cancelled"}) == "cancelled"
    assert state_of(gone, 10, {}) == "sent"  # an unknown word: keep following it
    ours = {"A": 10, "B": 5}
    theirs = [BrokerPosition("A", "I", 4, 1.0), BrokerPosition("A", "D", 6, 1.0)]
    assert [str(m) for m in reconcile(ours, theirs)] == ["B: we hold 5, the broker says 0"]


# --- the broker adapter --------------------------------------------------------------

ORDER_DETAILS = {  # upstox.com/developer/api-documentation/get-order-details (2026-10-04)
    "status": "success",
    "data": {
        "exchange": "NSE", "product": "D", "price": 571.0, "quantity": 1, "status": "complete",
        "tag": None, "instrument_token": KEY, "order_type": "LIMIT", "validity": "DAY",
        "transaction_type": "BUY", "average_price": 570.95, "filled_quantity": 1,
        "pending_quantity": 0, "status_message": None, "order_id": "231019025562880",
    },
}  # fmt: skip


def test_every_order_is_a_limit_order_with_our_key_as_its_tag() -> None:
    body = upstox.place_body(BrokerOrder("gats-1-7", KEY, "buy", "intraday", 12, 571.456))
    assert body == {
        "quantity": 12, "product": "I", "validity": "DAY", "price": 571.46, "tag": "gats-1-7",
        "instrument_token": KEY, "order_type": "LIMIT", "transaction_type": "BUY",
        "disclosed_quantity": 0, "trigger_price": 0, "is_amo": False, "slice": False,
    }  # fmt: skip
    assert upstox.place_body(BrokerOrder("k", KEY, "sell", "delivery", 1, 10.0))["product"] == "D"
    for bad in (BrokerOrder("k", KEY, "buy", "intraday", 0, 10.0),
                BrokerOrder("k", KEY, "buy", "intraday", 5, 0.0)):  # fmt: skip
        with pytest.raises(ValueError, match="not an order"):
            upstox.place_body(bad)


def test_documented_replies_are_read_and_anything_else_is_an_error() -> None:
    placed = {"status": "success", "data": {"order_ids": ["1644490272000"]}}
    assert upstox.parse_place(json.dumps(placed).encode()) == "1644490272000"
    sliced = {"status": "success", "data": {"order_ids": ["1", "2"]}}
    for bad in (sliced, {"status": "error", "errors": [{"errorCode": "UDAPI1154"}]}, [], "x"):
        with pytest.raises(BrokerError):
            upstox.parse_place(json.dumps(bad).encode())
    with pytest.raises(BrokerError, match="not JSON"):
        upstox.parse_place(b"<html>")
    state = upstox.parse_order(json.dumps(ORDER_DETAILS).encode())
    assert (state.order_id, state.status, state.filled_quantity, state.pending_quantity) == (
        "231019025562880", "complete", 1, 0,
    )  # fmt: skip
    assert state.average_price == 570.95 and state_of(state, 1, {}) == "filled"
    positions = {"status": "success", "data": [
        {"instrument_token": "NSE_FO|52618", "product": "D", "quantity": 15, "average_price": 2.65}
    ]}  # fmt: skip
    assert upstox.parse_positions(json.dumps(positions).encode()) == [
        BrokerPosition("NSE_FO|52618", "D", 15, 2.65)
    ]


@respx.mock
async def test_placing_is_sent_exactly_once_whatever_happens(svc: Services) -> None:
    broker = upstox.UpstoxBroker(svc)
    order = BrokerOrder("gats-1-1", KEY, "buy", "intraday", 1, 571.0)
    with pytest.raises(upstox.TokenMissing):
        await broker.place(order)
    svc.settings.upstox_access_token = SecretStr("order-token")
    route = respx.post("https://api-hft.upstox.com/v3/order/place")
    route.mock(return_value=httpx.Response(503))  # a status the client would normally retry
    with pytest.raises(OutcomeUnknown):
        await broker.place(order)
    assert route.call_count == 1  # not retried: the first request may have got through
    route.mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(OutcomeUnknown):
        await broker.place(order)
    assert route.call_count == 2
    route.mock(return_value=httpx.Response(400, json={"status": "error", "errors": []}))
    with pytest.raises(BrokerError, match="HTTP 400"):
        await broker.place(order)
    ok = {"status": "success", "data": {"order_ids": ["77"]}, "metadata": {"latency": 30}}
    route.mock(return_value=httpx.Response(200, json=ok))
    assert await broker.place(order) == "77"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer order-token"
    assert json.loads(request.content)["order_type"] == "LIMIT"


# --- alerts ----------------------------------------------------------------------


@respx.mock
async def test_alerts_reach_telegram_and_the_token_reaches_no_log(
    svc: Services, caplog: pytest.LogCaptureFixture
) -> None:
    assert isinstance(alerter(svc), NoAlerts) and not await alerter(svc).send("hello")
    svc.settings.telegram_bot_token = SecretStr("123:SECRET-TOKEN")
    svc.settings.telegram_chat_id = "42"
    route = respx.post("https://api.telegram.org/bot123:SECRET-TOKEN/sendMessage")
    route.mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    alerts = alerter(svc)
    assert isinstance(alerts, TelegramAlerter)
    assert await alerts.send("FILL: buy 10") is True
    assert json.loads(route.calls.last.request.content) == {"chat_id": "42", "text": "FILL: buy 10"}
    assert await alerts.send("FILL: buy 10") is False and route.call_count == 1  # not repeated

    caplog.set_level(logging.DEBUG)
    route.mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"})
    )
    assert await alerts.send("second") is False
    route.mock(side_effect=httpx.ConnectError("https://api.telegram.org/bot123:SECRET-TOKEN/x"))
    assert await alerts.send("third") is False  # an alert that fails is dropped, never raised
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "chat not found" in logged and "telegram:sendMessage" in logged
    assert "SECRET-TOKEN" not in logged
    with pytest.raises(FetchError) as caught:
        await svc.client.post_json(
            "https://api.telegram.org/bot123:SECRET-TOKEN/sendMessage", {}, label="x", retries=0
        )
    assert "SECRET-TOKEN" not in str(caught.value)


def test_tick_messages_say_what_happened() -> None:
    refused = engine_order(1)
    refused.status, refused.note = "rejected", "risk: kill switch: data/KILL exists"
    messages = tick_messages("s1", [engine_order(2), refused], [], ["NSE_EQ|X: HTTP 503"])
    assert messages[0].startswith("[s1] SIGNAL: buy 10") and "limit 500.00" in messages[0]
    assert messages[1].startswith("[s1] REFUSED: buy 10") and "kill switch" in messages[1]
    assert messages[2] == "[s1] ERROR: NSE_EQ|X: HTTP 503"
