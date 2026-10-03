"""The strategy interface shared by backtest, paper and live (T6.2).

Strategies are pure: ``on_event`` and ``on_bar`` map their inputs (and a
read-only :class:`Context`) to signals, with no I/O and no hidden state.
Positions, entry times and the clock come from the runtime through the
context. That is what lets one object run unchanged in a backtest replay,
in paper trading and live, and what makes a restart mid-session or a
replay of a past day give the same decisions.

Parameters live in YAML (``configs/strategies/``) and are versioned by
their content, so every signal can name exactly which rules produced it.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar, Generic, Literal, Protocol, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict

from gats.timeutil import ensure_aware

Side = Literal["buy", "sell"]
Product = Literal["intraday", "delivery"]


@dataclass(frozen=True)
class MarketEvent:
    """A filing the strategy may act on, as it was known at ``available_at``."""

    event_id: int
    security_id: int
    instrument_key: str
    event_type: str
    available_at: datetime  # UTC; nothing earlier may act on it
    facts: Mapping[str, Any] = field(default_factory=dict)  # extracted facts, features


@dataclass(frozen=True)
class BarEvent:
    """A one-minute bar, known only once it has closed."""

    instrument_key: str
    start: datetime  # UTC
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def closed_at(self) -> datetime:
        return self.start + timedelta(minutes=1)


@dataclass(frozen=True)
class Position:
    instrument_key: str
    quantity: int  # positive long, negative short
    product: Product
    opened_at: datetime
    entry_price: float
    reason: str  # the opening signal's reason


@dataclass(frozen=True)
class Signal:
    """What the strategy wants; the engine decides how, risk decides whether."""

    instrument_key: str
    side: Side
    product: Product
    reason: str
    created_at: datetime
    limit_price: float | None = None  # None: marketable, inside the engine's protection band
    quantity: int | None = None  # None: the position sizer decides
    closes: bool = False  # True: reduces an existing position


class Context(Protocol):
    """What a runtime shows a strategy: read-only."""

    @property
    def now(self) -> datetime: ...

    def position(self, instrument_key: str) -> Position | None: ...

    def positions(self) -> list[Position]: ...

    def pending(self, instrument_key: str) -> int:
        """Shares in working orders: positive to buy, negative to sell. A
        stateless strategy needs this to avoid repeating an order that has
        been sent but not filled yet."""
        ...


class StrategyParams(BaseModel):
    """Base for a strategy's parameters: strict, immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


P = TypeVar("P", bound=StrategyParams)


class Strategy(ABC, Generic[P]):
    """Subclasses set ``name`` and ``params_model`` and implement the hooks."""

    name: ClassVar[str]
    params_model: ClassVar[type[StrategyParams]]

    def __init__(self, params: P, version: str) -> None:
        self.params = params
        self.version = version

    @classmethod
    def from_yaml(cls, path: Path) -> Strategy[P]:
        """Load ``{strategy, version, params}``; the version string names the
        strategy, its declared version and a hash of the parameters."""
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if raw.get("strategy") != cls.name:
            raise ValueError(f"{path}: strategy {raw.get('strategy')!r} is not {cls.name!r}")
        params = cls.params_model.model_validate(raw.get("params") or {})
        return cls(params, version_of(cls.name, str(raw.get("version", "v0")), params))  # type: ignore[arg-type]

    @abstractmethod
    def on_event(self, event: MarketEvent, ctx: Context) -> list[Signal]:
        """React to a filing that has just become available."""

    def on_bar(self, bar: BarEvent, ctx: Context) -> list[Signal]:
        """React to a closed bar (default: nothing)."""
        return []


def version_of(name: str, declared: str, params: StrategyParams) -> str:
    canonical = json.dumps(params.model_dump(mode="json"), sort_keys=True)
    return f"{name}:{declared}+{hashlib.sha256(canonical.encode()).hexdigest()[:8]}"


@dataclass
class StaticContext:
    """A plain context, for tests and simple replays."""

    clock: datetime
    held: dict[str, Position] = field(default_factory=dict)
    working: dict[str, int] = field(default_factory=dict)

    @property
    def now(self) -> datetime:
        return ensure_aware(self.clock)

    def position(self, instrument_key: str) -> Position | None:
        return self.held.get(instrument_key)

    def positions(self) -> list[Position]:
        return list(self.held.values())

    def pending(self, instrument_key: str) -> int:
        return self.working.get(instrument_key, 0)
