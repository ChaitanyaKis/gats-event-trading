"""The paper run's journal (T7.1): every input is taken once, a restart
replays to the same account, and a replay that would decide differently is
refused. Market data is SYNTHETIC."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Engine as Database
from sqlalchemy import select, text

from gats.backtest.engine import BacktestResult, Engine, EngineConfig, Listener
from gats.db.schema import paper_executions, paper_journal, paper_orders, paper_runs
from gats.risk.engine import RiskEngine, RiskLimits
from gats.runtime.journal import (
    Build,
    DayClose,
    DesignChanged,
    Diverged,
    Item,
    PaperRun,
    Reference,
    Tape,
    decode,
    encode,
    item_key,
)
from gats.strategy.base import BarEvent, MarketEvent, Strategy
from tests.test_engine import COSTS, D1, D2, A, B, Scripted, at, bars, buy, flat, sell

LIMITS = RiskLimits.model_validate(
    yaml.safe_load((Path(__file__).parents[1] / "configs" / "risk.yaml").read_text("utf-8"))
)
CREATED = at(9, 0)
DELAY = timedelta(seconds=3)  # how long after a bar closes the runtime has it


@dataclass
class FakeWorld:
    turnover: float | None = 5e7
    listed: frozenset[str] | None = frozenset()
    kill: bool = False

    def liquidity(self, instrument_key: str, day: Any) -> float | None:
        return self.turnover

    def flags(self, instrument_key: str, day: Any) -> frozenset[str] | None:
        return self.listed

    def halted(self) -> bool:
        return self.kill

    def session(self, day: Any) -> bool:
        return True


def plan(quantity: int = 10) -> Scripted:
    return Scripted(
        {
            (A, at(10, 0)): [buy(quantity=quantity)],
            (A, at(10, 10)): [sell(when=at(10, 11))],
        }
    )


def builder(strategy: Strategy) -> Build:  # type: ignore[type-arg]
    def build(tape: Tape, listener: Listener) -> Engine:
        risk = RiskEngine(
            LIMITS, "test", liquidity=tape.liquidity, flags=tape.flags, halted=tape.halted
        )
        config = EngineConfig(initial_cash=1_000_000.0, notional_per_trade=50_000.0)
        return Engine(
            strategy, COSTS, config, risk=risk, listener=listener, is_session=tape.session
        )

    return build


def open_run(db: Database, strategy: Strategy | None = None, design_hash: str = "d1") -> PaperRun:  # type: ignore[type-arg]
    with db.begin() as conn:
        return PaperRun.open(
            conn,
            name="test-run",
            strategy="scripted:v0",
            design_hash=design_hash,
            design={"strategy": "scripted:v0", "engine": {"latency_s": 5.0}},
            build=builder(strategy or plan()),
            now=CREATED,
        )


def feed(db: Database, run: PaperRun, items: list[Item], world: FakeWorld | None = None) -> None:
    with db.begin() as conn:
        for item in items:
            when = item.closed_at + DELAY if isinstance(item, BarEvent) else at(15, 35)
            run.apply(conn, item, when, world or FakeWorld())


def account(result: BacktestResult) -> dict[str, Any]:
    return {
        "cash": round(result.cash, 6),
        "positions": {k: (p.quantity, p.entry_price) for k, p in result.positions.items()},
        "orders": [(o.order_id, o.quantity, o.filled, o.status, o.note) for o in result.orders],
        "fills": [(e.order_id, e.side, e.quantity, e.price, e.at) for e in result.executions],
        "equity": result.equity,
    }


SESSION: list[Item] = [*bars(A, at(10, 0), flat(100, 20)), DayClose(D1)]


def test_items_survive_the_journal_unchanged() -> None:
    event = MarketEvent(7, 3, A, "ORDER_WIN", at(10, 0), {"amount_vs_revenue": 0.25})
    items: list[Item] = [
        event,
        bars(A, at(10, 0), flat(100.05, 1))[0],
        Reference(A, 99.5, at(15, 30)),
        DayClose(D1),
    ]
    assert [decode(*encode(item)) for item in items] == items
    assert [item_key(item) for item in items] == [
        "event|7",
        f"bar|{A}|2026-07-14T04:30:00+00:00",
        f"ref|{A}|2026-07-14T10:00:00+00:00",
        "close|2026-07-14",
    ]
    with pytest.raises(ValueError, match="unknown journal kind"):
        decode("tick", {})


def test_a_run_is_recorded_with_its_design_and_only_resumes_as_that_design(
    engine: Database,
) -> None:
    run = open_run(engine)
    with engine.begin() as conn:
        row = conn.execute(select(paper_runs)).one()
    assert (row.name, row.strategy, row.design_hash) == ("test-run", "scripted:v0", "d1")
    assert row.design["engine"] == {"latency_s": 5.0} and row.created_at == CREATED
    assert open_run(engine).id == run.id  # the same design resumes the same run
    with pytest.raises(DesignChanged, match=r"new paper run"):
        open_run(engine, design_hash="d2")
    with (
        engine.begin() as conn,
        pytest.raises(DesignChanged, match=r"engine.latency_s: 5.0 -> 1.0"),
    ):
        PaperRun.open(
            conn,
            name="test-run",
            strategy="scripted:v0",
            design_hash="d3",
            design={"strategy": "scripted:v0", "engine": {"latency_s": 1.0}},
            build=builder(plan()),
            now=CREATED,
        )


def test_every_step_is_journaled_with_what_it_caused(engine: Database) -> None:
    run = open_run(engine)
    feed(engine, run, SESSION)
    with engine.begin() as conn:
        journal = conn.execute(select(paper_journal).order_by(paper_journal.c.seq)).all()
        orders = conn.execute(select(paper_orders).order_by(paper_orders.c.order_id)).all()
        fills = conn.execute(select(paper_executions).order_by(paper_executions.c.seq)).all()
    assert [row.seq for row in journal] == list(range(1, 22))
    assert [row.kind for row in journal] == ["bar"] * 20 + ["close"]
    assert journal[0].at == at(10, 1) + DELAY and journal[0].payload["close"] == 100
    # Only the day's first step (which also sent the entry) asked anything.
    asked = {row.seq: sorted(row.answers) for row in journal if row.answers}
    assert asked == {
        1: [f"flags|{A}|2026-07-14", "halted", f"liquidity|{A}|2026-07-14", "session|2026-07-14"]
    }  # the calendar once a day, the risk rules once per entry
    assert [(o.side, o.quantity, o.filled, o.status) for o in orders] == [
        ("buy", 10, 10, "filled"),
        ("sell", 10, 10, "filled"),
    ]
    assert orders[0].submitted_at == at(10, 1) + DELAY and orders[1].closes
    assert [(f.seq, f.order_id, f.side, f.quantity) for f in fills] == [
        (1, 1, "buy", 10),
        (2, 2, "sell", 10),
    ]
    assert fills[0].at == at(10, 2) and fills[0].charges > 0


def test_a_restart_replays_to_the_same_account(engine: Database) -> None:
    whole = open_run(engine)
    feed(engine, whole, SESSION)
    expected = account(whole.engine.result())

    with engine.begin() as conn:
        for table in (paper_executions, paper_orders, paper_journal, paper_runs):
            conn.execute(table.delete())
    first = open_run(engine)
    feed(engine, first, SESSION[:6])  # stop with the entry filled and the exit not yet sent
    assert first.engine.result().positions[A].quantity == 10

    resumed = open_run(engine)  # a new process: nothing but the database
    assert account(resumed.engine.result()) == account(first.engine.result())
    assert resumed.last_bar == {A: at(10, 5)} and resumed.last_at == at(10, 6) + DELAY
    assert all(resumed.seen(item) for item in SESSION[:6])
    feed(engine, resumed, [item for item in SESSION if not resumed.seen(item)])
    assert account(resumed.engine.result()) == expected
    assert account(open_run(engine).engine.result()) == expected  # and again, after the close


def test_a_replay_is_answered_from_the_tape_not_from_today(engine: Database) -> None:
    run = open_run(engine)
    feed(engine, run, SESSION[:3], FakeWorld(kill=True))  # the switch was on at the time
    (order,) = run.engine.result().orders
    assert order.status == "rejected" and "kill switch" in order.note

    replayed = open_run(engine)  # no world at all: the tape answers
    (again,) = replayed.engine.result().orders
    assert (again.status, again.note) == (order.status, order.note)
    with engine.begin() as conn:
        answers = conn.execute(select(paper_journal.c.answers).where(paper_journal.c.seq == 1))
        # Refused at the kill switch, before the other risk rules were asked.
        assert answers.scalar_one() == {"halted": True, "session|2026-07-14": True}


def test_unknown_answers_are_recorded_as_unknown(engine: Database) -> None:
    run = open_run(engine)
    feed(engine, run, SESSION[:3], FakeWorld(turnover=None, listed=None))
    assert "liquidity" in run.engine.result().orders[0].note
    assert "liquidity" in open_run(engine).engine.result().orders[0].note
    with engine.begin() as conn:
        conn.execute(text("UPDATE paper_journal SET answers = '{\"halted\": false}' WHERE seq = 1"))
    with pytest.raises(Diverged, match="never asked"):
        open_run(engine)


def test_a_replay_that_would_decide_differently_is_refused(engine: Database) -> None:
    run = open_run(engine)
    feed(engine, run, SESSION)
    with pytest.raises(Diverged, match=r"order 1: quantity: recorded 10, replay 20"):
        open_run(engine, plan(quantity=20))  # "the code changed": same design name, new rules
    with pytest.raises(Diverged, match=r"journal step 1 .*the replay did not"):
        open_run(engine, Scripted())  # never trades, so never asks the risk rules
    keen = plan()
    keen.on_bars[(A, at(10, 15))] = [sell(when=at(10, 16))]  # one more order, late in the day
    with pytest.raises(Diverged, match=r"2 orders recorded, the replay gives 3"):
        open_run(engine, keen)
    open_run(engine)  # the unchanged system still resumes
    with engine.begin() as conn:
        conn.execute(text("UPDATE paper_executions SET price = price + 1 WHERE seq = 1"))
    with pytest.raises(Diverged, match=r"fill 1: price: recorded 101\.\d+, replay 100\.\d+"):
        open_run(engine)


def test_an_input_is_taken_once_and_bars_in_order(engine: Database) -> None:
    run = open_run(engine)
    feed(engine, run, SESSION[:4])
    with engine.begin() as conn, pytest.raises(ValueError, match="already taken"):
        run.apply(conn, SESSION[3], at(10, 30), FakeWorld())
    assert run.seen(SESSION[0]) and not run.seen(SESSION[4])
    assert not run.seen(bars(B, at(10, 0), flat(50, 1))[0])  # another stock has its own order
    event = MarketEvent(7, 3, A, "ORDER_WIN", at(10, 4))
    assert not run.seen(event)
    with engine.begin() as conn:
        run.apply(conn, event, at(10, 5), FakeWorld())
        run.apply(conn, Reference(B, 50.0, at(15, 30, D1 - timedelta(days=1))), at(10, 5), None)
    assert run.seen(event) and run.securities == {A: 3}
    assert run.seen(Reference(B, 50.0, at(15, 30, D1 - timedelta(days=1))))
    assert run.engine.state.last_price[B] == 50.0
    with engine.begin() as conn, pytest.raises(ValueError, match="cannot close"):
        run.apply(conn, DayClose(D2), at(15, 35), None)
    resumed = open_run(engine)
    assert resumed.events == {7} and resumed.engine.state.last_price[B] == 50.0


def test_a_step_that_fails_leaves_nothing_behind(engine: Database) -> None:
    run = open_run(engine)
    bar = SESSION[0]
    assert isinstance(bar, BarEvent)
    with engine.begin() as conn, pytest.raises(ValueError, match="look-ahead"):
        run.apply(conn, bar, bar.start, FakeWorld())  # before the bar has closed
    with engine.begin() as conn:
        assert conn.execute(select(paper_journal)).all() == []
    feed(engine, run, SESSION[:1])
    assert run.last_bar == {A: at(10, 0)}
