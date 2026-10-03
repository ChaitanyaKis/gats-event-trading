"""The strategy interface and S1 order-win drift (T6.2)."""

from __future__ import annotations

import copy
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from gats.strategy.base import (
    BarEvent,
    MarketEvent,
    Position,
    Signal,
    StaticContext,
    Strategy,
)
from gats.strategy.order_win import OrderWinDrift
from gats.timeutil import ist_datetime

CONFIG = Path(__file__).parents[1] / "configs" / "strategies" / "order_win_drift.yaml"
KEY = "NSE_EQ|INE002A01018"
DAY = date(2026, 7, 14)


def at(hh: int, mm: int, day: date = DAY) -> datetime:
    return ist_datetime(day, time(hh, mm))


def order_win(when: datetime, ratio: float | None = 0.4, event_id: int = 1) -> MarketEvent:
    facts = {} if ratio is None else {"amount_vs_revenue": ratio}
    return MarketEvent(event_id, 7, KEY, "ORDER_WIN", when, facts)


@pytest.fixture
def strategy() -> OrderWinDrift:
    loaded = OrderWinDrift.from_yaml(CONFIG)
    assert isinstance(loaded, OrderWinDrift)
    return loaded


class TestConfig:
    def test_version_names_the_rules(self, strategy: OrderWinDrift, tmp_path: Path) -> None:
        assert strategy.version.startswith("order_win_drift:v1+")
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
        raw["params"]["hold_minutes"] = 30
        changed = tmp_path / "s.yaml"
        changed.write_text(yaml.safe_dump(raw), encoding="utf-8")
        assert OrderWinDrift.from_yaml(changed).version != strategy.version  # a new trial

    @pytest.mark.parametrize(
        ("change", "error"),
        [
            ({"strategy": "something_else"}, ValueError),
            (
                {"params": {"min_amount_vs_revenue": 0.1, "hold_minutes": 60, "leverage": 5}},
                ValidationError,
            ),
            ({"params": {"min_amount_vs_revenue": -1, "hold_minutes": 60}}, ValidationError),
        ],
    )
    def test_bad_configs_are_refused(
        self, change: dict[str, Any], error: type[Exception], tmp_path: Path
    ) -> None:
        raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) | change
        path = tmp_path / "s.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with pytest.raises(error):
            OrderWinDrift.from_yaml(path)


class TestEntries:
    def test_large_order_in_session_is_bought(self, strategy: OrderWinDrift) -> None:
        signals = strategy.on_event(order_win(at(11, 0)), StaticContext(at(11, 0)))
        assert [(s.side, s.product, s.closes, s.quantity) for s in signals] == [
            ("buy", "intraday", False, None)
        ]
        assert signals[0].reason == "order_win_drift: order win #1"

    @pytest.mark.parametrize(
        ("event", "now", "why"),
        [
            (order_win(at(11, 0), ratio=0.05), at(11, 0), "too small for the company"),
            (order_win(at(11, 0), ratio=None), at(11, 0), "size unknown"),
            (order_win(at(15, 5)), at(15, 5), "after the entry cutoff"),
            (order_win(at(11, 0)), at(11, 45), "reached us too late"),
        ],
    )
    def test_skipped(
        self, strategy: OrderWinDrift, event: MarketEvent, now: datetime, why: str
    ) -> None:
        assert strategy.on_event(event, StaticContext(now)) == [], why

    def test_after_hours_filing_is_for_the_next_open(self, strategy: OrderWinDrift) -> None:
        assert len(strategy.on_event(order_win(at(18, 0)), StaticContext(at(18, 0)))) == 1

    def test_other_events_and_held_stocks(self, strategy: OrderWinDrift) -> None:
        other = MarketEvent(2, 7, KEY, "RESULTS", at(11, 0), {"amount_vs_revenue": 1.0})
        assert strategy.on_event(other, StaticContext(at(11, 0))) == []
        held = StaticContext(at(11, 0), {KEY: position(at(10, 0))})
        assert strategy.on_event(order_win(at(11, 0)), held) == []


def position(opened: datetime, reason: str = "order_win_drift: order win #1") -> Position:
    return Position(KEY, 10, "intraday", opened, 100.0, reason)


def bar(start: datetime) -> BarEvent:
    return BarEvent(KEY, start, 100, 101, 99, 100, 1000)


class TestExits:
    def test_exit_after_the_holding_time(self, strategy: OrderWinDrift) -> None:
        held = {KEY: position(at(11, 0))}
        early = bar(at(11, 30))
        assert strategy.on_bar(early, StaticContext(early.closed_at, held)) == []
        due = bar(at(11, 59))
        (signal,) = strategy.on_bar(due, StaticContext(due.closed_at, held))
        assert (signal.side, signal.quantity, signal.closes) == ("sell", 10, True)
        assert "held long enough" in signal.reason

    def test_square_off_before_the_close(self, strategy: OrderWinDrift) -> None:
        late = bar(at(15, 14))
        (signal,) = strategy.on_bar(
            late, StaticContext(late.closed_at, {KEY: position(at(14, 50))})
        )
        assert "square-off" in signal.reason

    def test_positions_of_other_strategies_are_left_alone(self, strategy: OrderWinDrift) -> None:
        theirs = {KEY: position(at(9, 30), reason="mean_reversion: entry")}
        assert strategy.on_bar(bar(at(15, 20)), StaticContext(at(15, 21), theirs)) == []


# --- the same object, two runtimes ----------------------------------------------------


@dataclass
class Replay:
    """A backtest-style driver: the whole day, in time order, at once."""

    strategy: Strategy[Any]
    held: dict[str, Position] = field(default_factory=dict)

    def run(self, items: Iterable[MarketEvent | BarEvent]) -> list[Signal]:
        out: list[Signal] = []
        for item in sorted(items, key=moment):
            out += self.feed(item)
        return out

    def feed(self, item: MarketEvent | BarEvent) -> list[Signal]:
        now = moment(item)
        ctx = StaticContext(now, dict(self.held))
        signals = (
            self.strategy.on_event(item, ctx)
            if isinstance(item, MarketEvent)
            else self.strategy.on_bar(item, ctx)
        )
        for s in signals:  # instant fills at 100: enough to exercise the exits
            if s.closes:
                self.held.pop(s.instrument_key, None)
            else:
                self.held[s.instrument_key] = Position(
                    s.instrument_key, 10, s.product, now, 100.0, s.reason
                )
        return signals


def moment(item: MarketEvent | BarEvent) -> datetime:
    return item.available_at if isinstance(item, MarketEvent) else item.closed_at


def test_one_object_two_runtimes_same_decisions(strategy: OrderWinDrift) -> None:
    day = [order_win(at(10, 30))] + [bar(at(10, 30) + timedelta(minutes=m)) for m in range(90)]
    before = copy.deepcopy(strategy.__dict__)
    replayed = Replay(strategy).run(day)
    live = Replay(strategy)  # a live runtime receives the same items one at a time
    streamed = [s for item in day for s in live.feed(item)]
    assert replayed == streamed
    assert [s.side for s in replayed] == ["buy", "sell"]
    assert replayed[1].created_at - replayed[0].created_at == timedelta(minutes=60)
    assert strategy.__dict__ == before  # no hidden state: a restart changes nothing


def test_orders_on_their_way_are_not_repeated(strategy: OrderWinDrift) -> None:
    buying = StaticContext(at(11, 0), working={KEY: 993})
    assert strategy.on_event(order_win(at(11, 0)), buying) == []
    selling = StaticContext(at(12, 1), {KEY: position(at(11, 0))}, working={KEY: -10})
    assert strategy.on_bar(bar(at(12, 0)), selling) == []
