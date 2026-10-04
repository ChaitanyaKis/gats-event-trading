"""The paper runtime end to end (T7.1): a filing goes through the recorder's
hand-off, the real strategy trades it on live bars, and the day is closed.

The market is SYNTHETIC and the broker is mocked with respx; the filing's
attachment is a real order-win PDF. The clock is a fake one, so a whole
session runs in seconds.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select

from gats.db import repo
from gats.db.schema import (
    announcements,
    paper_executions,
    paper_journal,
    paper_orders,
    surveillance_versions,
)
from gats.ingest import Services
from gats.oms import gate
from gats.recorder import HandOffJob, build_jobs
from gats.runtime import g3
from gats.runtime.journal import DesignChanged, Diverged
from gats.runtime.paper import PaperRuntime, System, TickReport, load_system, run_paper
from gats.runtime.status import Latency, latency, run_status, runs
from gats.sources.models import AnnouncementRecord
from gats.timeutil import IST, ist_datetime, to_ist
from tests.conftest import FakeClock
from tests.test_event_study import Market
from tests.test_results_ingest import quarter

ROOT = Path(__file__).parents[1]
PDF = (
    ROOT / "tests" / "fixtures" / "real" / "order_win_attachment.pdf"
).read_bytes()  # Rs 60 crore
DAY = date(2024, 4, 2)  # the Tuesday after the synthetic market's history ends
EVE = date(2024, 4, 1)
KEY = "NSE_EQ|INE00000A0101"  # AAA in the synthetic market
ATTACHMENT = "https://nsearchives.nseindia.com/corporate/AAA_order.pdf"
INTRADAY = re.compile(r"https://api\.upstox\.com/v3/historical-candle/intraday/(.+)/minutes/1$")
SESSION = re.compile(r"https://api\.upstox\.com/v3/historical-candle/NSE_EQ.+/minutes/1/(.+)/(.+)$")
OPEN, CLOSE = time(9, 15), time(15, 30)
MARGIN = timedelta(seconds=3)


def at(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return ist_datetime(day, time(hh, mm, ss))


def price(minute: datetime) -> float:
    """A slow, steady climb through the session."""
    return 100 + 0.01 * ((minute - at(9, 15, day=to_ist(minute).date())).total_seconds() // 60)


def candle(minute: datetime, forming: bool) -> list[Any]:
    stamp = minute.astimezone(IST).isoformat()
    p = price(minute)
    if forming:  # one trade so far, at a price no finished bar ever shows
        return [stamp, 777.0, 777.0, 777.0, 777.0, 1, 0]
    return [stamp, p, p + 0.2, p - 0.2, p + 0.01, 10_000, 0]


class Broker:
    """The broker's candle API, as of the fake clock: the current day's
    candles newest first, the one still forming included."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.asked: list[datetime] = []
        self.down_until: datetime | None = None

    def intraday(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        now = self.clock.now
        self.asked.append(now)
        if self.down_until is not None and now < self.down_until:
            return httpx.Response(503)
        day = to_ist(now).date()
        candles, minute = [], at(9, 15, day=day)
        while minute < min(now, at(15, 30, day=day)):
            candles.append(candle(minute, forming=minute + timedelta(minutes=1) > now))
            minute += timedelta(minutes=1)
        return httpx.Response(200, json={"status": "success", "data": {"candles": candles[::-1]}})

    def session(self, request: httpx.Request) -> httpx.Response:
        day = date.fromisoformat(request.url.path.rsplit("/", 1)[1])
        candles = [
            candle(at(9, 15, day=day) + timedelta(minutes=i), forming=False) for i in range(375)
        ]
        return httpx.Response(200, json={"status": "success", "data": {"candles": candles[::-1]}})


@pytest.fixture
def broker(svc: Services, clock: FakeClock) -> Broker:
    """The synthetic market (AAA: Rs 400 crore a year of revenue, liquid, on
    no surveillance list) and a broker to ask for candles."""
    svc.settings.upstox_analytics_token = SecretStr("test-token")
    with svc.engine.begin() as conn:
        market = Market(conn, svc.store)
        market.list_stocks(["AAA", "BBB"])
        market.write_prices(["AAA", "BBB"])
        doc = market.doc()
        for end, public in [
            (date(2023, 3, 31), date(2023, 5, 10)),
            (date(2023, 6, 30), date(2023, 8, 10)),
            (date(2023, 9, 30), date(2023, 11, 10)),
            (date(2023, 12, 31), date(2024, 2, 10)),
        ]:
            quarter(conn, doc, end, 100, public)
        conn.execute(
            surveillance_versions.insert().values(
                entity_key="LTASM:ZZZ", valid_from=date(2024, 3, 1), list_name="LTASM",
                symbol="ZZZ", available_at=datetime(2024, 3, 1, tzinfo=UTC), raw_doc_id=doc,
                parser_version="v",
            )
        )  # fmt: skip
    clock.now = at(9, 10)
    return Broker(clock)


@pytest.fixture
def system(svc: Services, tmp_path: Path) -> System:
    """The real paper configuration; only the kill switch lives elsewhere."""
    loaded = load_system(Path("configs/paper.yaml"), svc.settings, root=ROOT)
    return replace(loaded, root=tmp_path)


def mock_http(svc: Services, broker: Broker) -> None:
    respx.get(svc.settings.nse_home_url).mock(return_value=httpx.Response(200))
    respx.get(ATTACHMENT).mock(return_value=httpx.Response(200, content=PDF))
    respx.get(url__regex=INTRADAY).mock(side_effect=broker.intraday)
    respx.get(url__regex=SESSION).mock(side_effect=broker.session)


async def file_order_win(svc: Services) -> int:
    """An order win appears on NSE now, and the recorder hands it off."""
    now = svc.clock()
    record = AnnouncementRecord(
        source="NSE", source_ann_id=f"live-{now.isoformat()}", symbol="AAA", scrip_code=None,
        isin=None, company_name="AAA Ltd", category="Bagging/Receiving of orders/contracts",
        subcategory=None, subject="Receipt of order", details=None, attachment_url=ATTACHMENT,
        exch_submitted_ts=None, exch_disseminated_ts=now, event_ts=now,
    )  # fmt: skip
    with svc.engine.begin() as conn:
        page = repo.save_raw(
            conn, svc.store, now.isoformat().encode(), kind="t", source="NSE", url="u",
            content_type=None, fetched_at=now,
        )  # fmt: skip
        repo.insert_announcements(
            conn, [record], raw_doc_id=page, parser_version="v", mode="live", fetched_at=now,
            now=now,
        )  # fmt: skip
        filing = int(
            conn.execute(
                select(announcements.c.id).where(
                    announcements.c.source_ann_id == record.source_ann_id
                )
            ).scalar_one()
        )
    (hand_off,) = [j for j in build_jobs(svc) if isinstance(j, HandOffJob)]
    outcome = await hand_off.run_once(svc)
    assert outcome.meta["texts"] == 1
    return filing


async def run_session(
    runtimes: dict[str, PaperRuntime],
    clock: FakeClock,
    start: datetime,
    end: datetime,
    happenings: dict[datetime, Callable[[], Any]] | None = None,
    step_s: int = 15,
) -> dict[str, list[TickReport]]:
    """Tick every runtime every ``step_s`` seconds of fake time."""
    reports: dict[str, list[TickReport]] = {name: [] for name in runtimes}
    clock.now = start
    while clock.now <= end:
        happening = (happenings or {}).get(clock.now)
        if happening is not None:
            result = happening()
            if hasattr(result, "__await__"):
                await result
        for name in list(runtimes):
            reports[name].append(await runtimes[name].tick())
        clock.now += timedelta(seconds=step_s)
    return reports


def journal(svc: Services, run_id: int) -> list[Any]:
    with svc.engine.begin() as conn:
        return list(
            conn.execute(
                select(paper_journal)
                .where(paper_journal.c.run_id == run_id)
                .order_by(paper_journal.c.seq)
            ).all()
        )


def fills(svc: Services, run_id: int) -> list[tuple[str, int, float, datetime]]:
    with svc.engine.begin() as conn:
        rows = conn.execute(
            select(paper_executions)
            .where(paper_executions.c.run_id == run_id)
            .order_by(paper_executions.c.seq)
        ).all()
    return [(r.side, r.quantity, r.price, r.at) for r in rows]


@respx.mock
async def test_a_full_session_with_a_restart_in_the_middle(
    svc: Services, clock: FakeClock, broker: Broker, system: System
) -> None:
    mock_http(svc, broker)
    runtimes = {
        "whole": PaperRuntime(svc, system, "whole"),
        "restarted": PaperRuntime(svc, system, "restarted"),
    }
    filed: list[int] = []

    async def order_win() -> None:
        filed.append(await file_order_win(svc))

    def crash() -> None:  # a new process: nothing but the database survives
        runtimes["restarted"] = PaperRuntime(svc, system, "restarted")

    reports = await run_session(
        runtimes, clock, at(9, 10), at(15, 36), {at(10, 0, 30): order_win, at(10, 30): crash}
    )
    whole = runtimes["whole"]
    rows = journal(svc, whole.run.id)

    # The filing: handed over in the tick it became readable, with its facts.
    (event,) = [r for r in rows if r.kind == "event"]
    assert event.payload["event_id"] == filed[0] and event.at == at(10, 0, 30)
    assert event.payload["available_at"] == at(10, 0, 30).isoformat()
    facts = event.payload["facts"]
    assert facts["amount_inr"] == 600_000_000.0  # read from the real PDF by the rules
    assert facts["amount_vs_revenue"] == pytest.approx(0.15)  # Rs 60 crore on Rs 400 crore
    assert sorted(event.answers) == [
        f"flags|{KEY}|2024-04-02", "halted", f"liquidity|{KEY}|2024-04-02",
    ]  # fmt: skip
    assert event.answers["halted"] is False and event.answers[f"flags|{KEY}|2024-04-02"] == []

    # Bars: the day so far when the filing arrived, then one a minute while
    # the trade was open. Never the candle still forming, never early.
    bars = [r for r in rows if r.kind == "bar"]
    starts = [datetime.fromisoformat(r.payload["start"]) for r in bars]
    assert starts[0] == at(9, 15) and starts == sorted(set(starts))
    assert [r.at for r in bars[:45]] == [at(10, 0, 30)] * 45  # 09:15 to 09:59, with the filing
    assert all(r.payload["open"] != 777.0 and r.payload["volume"] == 10_000 for r in bars)
    assert all(r.at >= s + timedelta(minutes=1) + MARGIN for r, s in zip(bars, starts, strict=True))
    assert rows.index(event) == 45  # the filing came after the bars known by then
    assert rows[0].answers == {"session|2024-04-02": True}  # the calendar, once a day

    # The trade: in at the first bar to start after the decision, out an hour later.
    with svc.engine.begin() as conn:
        orders = conn.execute(
            select(paper_orders)
            .where(paper_orders.c.run_id == whole.run.id)
            .order_by(paper_orders.c.order_id)
        ).all()
    entry, exit_ = orders
    assert (entry.side, entry.status, entry.submitted_at) == ("buy", "filled", at(10, 0, 30))
    assert entry.quantity == 497 == entry.filled  # Rs 50,000 at the 09:59 close of 100.45
    assert f"order win #{filed[0]}" in entry.reason
    assert (exit_.side, exit_.status, exit_.closes) == ("sell", "filled", True)
    assert "held long enough" in exit_.reason and exit_.submitted_at == at(11, 1, 15)
    bought, sold = fills(svc, whole.run.id)
    assert bought[0] == "buy" and bought[3] == at(10, 1)  # not the 10:00 bar: it had begun
    assert bought[2] == pytest.approx(price(at(10, 1)) * (1 + (5 + 50 * 497 / 10_000) / 1e4))
    assert sold[0] == "sell" and sold[3] == at(11, 2) and sold[1] == 497

    # Once flat, the stock is no longer asked for; the day is closed after 15:35.
    assert max(broker.asked) == at(11, 3, 15)
    assert starts[-1] == at(11, 2)
    assert rows[-1].kind == "close" and rows[-1].at == at(15, 35)
    result = whole.run.engine.result()
    assert result.positions == {} and list(result.equity) == [DAY]
    assert result.equity[DAY] == pytest.approx(result.cash) and result.cash > 1_000_000  # it rose
    closed = [r.closed for r in reports["whole"] if r.closed]
    assert closed == [DAY]
    assert sum(r.bars for r in reports["whole"]) == len(bars)

    # The stored record says the same, without replaying anything.
    with svc.engine.begin() as conn:
        status = run_status(conn, "whole")
        assert run_status(conn, "nobody") is None and len(runs(conn)) == 2
    assert status is not None and status.design_hash == system.design_hash
    assert status.steps == {"bar": len(bars), "event": 1, "close": 1}
    assert status.orders == {"filled": 2} and status.fills == 2 and status.open_quantity == {}
    assert status.sold > status.bought > 49_000 and status.charges > 0
    assert status.last_step_at == at(15, 35)
    assert status.hand_over == Latency(1, 0.0, 0.0, 0.0)  # readable and handed over in one tick
    assert status.feed == Latency(1, 0.0, 0.0, 0.0)  # the test files it at its exchange time

    # The run that crashed at 10:30 and came back did exactly the same.
    again = runtimes["restarted"]
    assert fills(svc, again.run.id) == fills(svc, whole.run.id)
    assert again.run.engine.result().cash == result.cash
    assert [(r.kind, r.item_key, r.at) for r in journal(svc, again.run.id)] == [
        (r.kind, r.item_key, r.at) for r in rows
    ]
    # And a fresh process still replays the whole day to the same account.
    assert PaperRuntime(svc, system, "whole").run.engine.result().cash == result.cash


@respx.mock
async def test_a_filing_before_the_open_is_bought_at_the_open(
    svc: Services, clock: FakeClock, broker: Broker, system: System
) -> None:
    mock_http(svc, broker)
    clock.now = at(8, 25)  # the run starts before the filing: older filings are not its business
    runtime = PaperRuntime(svc, system, "pre-open")
    await run_session(
        {"pre-open": runtime}, clock, at(8, 25), at(9, 20), {at(8, 30): lambda: file_order_win(svc)}
    )
    rows = journal(svc, runtime.run.id)
    reference, event = rows[0], rows[1]
    last = at(15, 29, day=EVE)  # yesterday's last candle
    assert reference.kind == "ref" and reference.payload["price"] == pytest.approx(
        price(last) + 0.01
    )
    assert reference.payload["as_of"] == at(15, 30, day=EVE).isoformat()
    assert event.kind == "event" and event.at == at(8, 30)
    (order,) = runtime.run.engine.orders
    assert order.status == "filled" and order.submitted_at == at(8, 30)
    assert order.quantity == int(50_000 // (price(last) + 0.01))  # sized from yesterday's close
    ((side, _, paid, when),) = fills(svc, runtime.run.id)
    assert side == "buy" and when == at(9, 15) and paid > 100  # the open, plus slippage
    assert min(broker.asked) == at(8, 30)  # asked once for the filing ...
    assert sorted(broker.asked)[1] == at(9, 15)  # ... then not again until the market opened


@respx.mock
async def test_the_kill_switch_refuses_entries_and_the_refusal_is_replayed(
    svc: Services, clock: FakeClock, broker: Broker, system: System, tmp_path: Path
) -> None:
    mock_http(svc, broker)
    assert system.kill_switch == tmp_path / "data" / "KILL"
    system.kill_switch.parent.mkdir(exist_ok=True)
    system.kill_switch.write_text("stop", encoding="utf-8")
    runtime = PaperRuntime(svc, system, "halted")
    await run_session(
        {"halted": runtime},
        clock,
        at(10, 0),
        at(10, 3),
        {at(10, 0, 30): lambda: file_order_win(svc)},
    )
    (order,) = runtime.run.engine.orders
    assert order.status == "rejected" and "kill switch" in order.note
    assert broker.asked == [at(10, 0, 30)]  # nothing to watch after a refusal

    system.kill_switch.unlink()  # the switch is off again; the past does not change
    (replayed,) = PaperRuntime(svc, system, "halted").run.engine.orders
    assert (replayed.status, replayed.note) == (order.status, order.note)
    with svc.engine.begin() as conn:
        status = run_status(conn, "halted")
    assert status is not None and status.refusals == {"kill switch": 1}
    assert status.orders == {"rejected": 1} and status.fills == 0


@respx.mock
async def test_a_filing_waits_for_a_price_when_the_broker_is_down(
    svc: Services, clock: FakeClock, broker: Broker, system: System
) -> None:
    mock_http(svc, broker)
    broker.down_until = at(10, 2)
    runtime = PaperRuntime(svc, system, "outage")
    session = await run_session(
        {"outage": runtime},
        clock,
        at(10, 0),
        at(10, 5),
        {at(10, 0, 30): lambda: file_order_win(svc)},
    )
    reports = session["outage"]
    failed = [r for r in reports if r.errors]
    assert failed and "HTTP 503" in failed[0].errors[0] and failed[0].waiting == 1
    (event,) = [r for r in journal(svc, runtime.run.id) if r.kind == "event"]
    assert event.at == at(10, 2)  # handed over when a price was there, 90 s late and still traded
    (order,) = runtime.run.engine.orders
    assert order.status == "filled" and order.submitted_at == at(10, 2)


@respx.mock
async def test_a_changed_design_is_a_new_run(
    svc: Services, clock: FakeClock, broker: Broker, system: System
) -> None:
    PaperRuntime(svc, system, "s1")
    slower = replace(system, design_hash="another", design=system.design | {"engine": {}})
    with pytest.raises(DesignChanged, match="new paper run"):
        PaperRuntime(svc, slower, "s1")
    assert PaperRuntime(svc, slower, "s2").run.id != PaperRuntime(svc, system, "s1").run.id


def test_the_design_names_everything_that_decides_a_result(system: System) -> None:
    assert set(system.design) == {"strategy", "costs", "risk", "engine", "feed"}
    assert system.design["strategy"].startswith("order_win_drift:v1+")
    assert system.design["engine"]["latency_s"] == 1.0
    feed = system.design["feed"]
    assert feed["source"] == "NSE" and feed["event_types"] == ["ORDER_WIN"]
    assert feed["extractor"].startswith("cascade:order-rules-") and feed["taxonomy"]
    assert len(system.design_hash) == 16


def test_latency_summary_uses_nearest_ranks() -> None:
    assert latency([]) is None
    assert latency([7.0]) == Latency(1, 7.0, 7.0, 7.0)
    sample = [float(n) for n in range(1, 101)]  # 1..100 seconds
    assert latency(sample[::-1]) == Latency(100, 50.0, 95.0, 100.0)
    assert latency([1.0, 2.0, 30.0]) == Latency(
        3, 2.0, 30.0, 30.0
    )  # a small sample: p95 is its worst


async def test_the_loop_survives_a_failed_tick_and_stops_for_a_broken_record(
    svc: Services, system: System, monkeypatch: pytest.MonkeyPatch
) -> None:
    svc.settings.paper_poll_s = 0.001
    runtime = PaperRuntime(svc, system, "loop")
    stop = asyncio.Event()
    calls: list[int] = []
    tick = runtime.tick

    async def flaky() -> TickReport:
        calls.append(len(calls) + 1)
        if len(calls) == 2:
            raise RuntimeError("database is locked")
        if len(calls) == 4:
            stop.set()
        return await tick()

    monkeypatch.setattr(runtime, "tick", flaky)
    await asyncio.wait_for(run_paper(svc, runtime, stop), 5)
    state = json.loads(svc.settings.paper_heartbeat_path.read_text("utf-8"))["jobs"]["paper"]
    assert calls == [1, 2, 3, 4]  # the failure cost one tick, not the run
    assert state["run"] == "loop" and state["design"] == system.design_hash
    assert state["consecutive_failures"] == 0 and "database is locked" in state["last_error"]
    assert state["positions"] == 0 and state["working"] == 0

    async def broken() -> TickReport:
        raise Diverged("fill 1: price: recorded 101.0, replay 100.0")

    monkeypatch.setattr(runtime, "tick", broken)
    with pytest.raises(Diverged):  # no retry can make a changed system the same one
        await asyncio.wait_for(run_paper(svc, runtime, asyncio.Event()), 5)


@respx.mock
async def test_the_g3_report_judges_the_run_against_its_own_backtest(
    svc: Services, clock: FakeClock, broker: Broker, system: System, tmp_path: Path
) -> None:
    mock_http(svc, broker)
    runtime = PaperRuntime(svc, system, "g3")
    await run_session(
        {"g3": runtime}, clock, at(9, 10), at(15, 36), {at(10, 0, 30): lambda: file_order_win(svc)}
    )
    shipped = g3.load_spec(ROOT / "configs" / "g3.yaml")
    assert (shipped.min_days, shipped.min_trades, shipped.backtest_latency_s) == (60, 30, 5.0)
    with svc.engine.begin() as conn:
        text, checks = g3.build_report(conn, system, "g3", shipped, at(16, 0))
    assert g3.verdict(checks) == "FAIL" and "**G3: FAIL**" in text  # one day is not two months
    failed = [c.name for c in checks if not c.passed]
    assert failed == ["at least 60 days of paper trading", "at least 30 closed trades"]
    by_name = {c.name: c for c in checks}
    assert by_name["signal count within 20% of the backtest"].detail == "paper 1, backtest 1"
    assert by_name["fill rate within 10% of the backtest"].detail == "paper 100.0%, backtest 100.0%"
    assert "| Entry orders sent | 1 | 1 |" in text and "| Closed trades | 1 | 1 |" in text

    lenient = shipped.model_copy(update={"min_days": 1, "min_trades": 1})
    with svc.engine.begin() as conn:
        text, checks = g3.build_report(conn, system, "g3", lenient, at(16, 0))
    assert g3.verdict(checks) == "PASS"
    report = tmp_path / "M7_paper.md"
    report.write_text(text, encoding="utf-8", newline="\n")
    assert gate.g3_verdict(report) == "PASS"  # the line the live gate reads


def test_expectancy_interval_and_a_losing_run() -> None:
    spec = g3.load_spec(ROOT / "configs" / "g3.yaml").model_copy(
        update={"min_days": 0, "min_trades": 1, "bootstrap_resamples": 500}
    )
    assert g3.expectancy_interval([], [], spec) is None
    days = [date(2026, 10, d) for d in (5, 5, 6, 7, 8, 9)]
    mean, low, high = g3.expectancy_interval([-50.0, -40.0, -60.0, -45.0, -55.0, -50.0], days, spec)  # type: ignore[misc]
    assert mean == -50.0 and low < mean < high < 0
    losing = g3.Side(entries=6, filled=6, slippage_bps=12.0, nets=[-50.0] * 6, days=days)
    same = g3.Side(entries=6, filled=6, slippage_bps=12.0, nets=[-50.0] * 6, days=days)
    checks = g3.judge(losing, same, 90, spec)
    assert g3.verdict(checks) == "FAIL"
    assert [c.name for c in checks if not c.passed] == [
        "net expectancy: lower end of the 95% interval not below zero"
    ]
    fewer = g3.Side(entries=4, filled=2, slippage_bps=30.0, nets=[10.0], days=days[:1])
    names = [c.name for c in g3.judge(fewer, same, 90, spec) if not c.passed]
    assert len(names) == 3 and "signal count" in names[0] and "slippage" in names[2]
