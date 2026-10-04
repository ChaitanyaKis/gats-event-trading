"""What the order layer needs from a broker, whichever broker it is."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from gats.strategy.base import Product, Side


class BrokerError(RuntimeError):
    """The broker refused, or its reply could not be read. The order was
    NOT accepted (as far as the reply says)."""


class OutcomeUnknown(RuntimeError):
    """The request was sent and no answer came back: the order may or may
    not exist at the broker. It must never be sent again blindly."""


@dataclass(frozen=True)
class BrokerOrder:
    """One order to place. Always a limit order: the limit is the market
    protection, so a gap or a thin book cannot fill it at any price."""

    client_id: str  # our idempotency key; travels as the broker's tag
    instrument_key: str
    side: Side
    product: Product
    quantity: int
    limit_price: float


@dataclass(frozen=True)
class BrokerState:
    """An order as the broker reports it."""

    order_id: str
    status: str  # the broker's own word
    filled_quantity: int
    pending_quantity: int
    average_price: float | None
    tag: str | None
    message: str | None


@dataclass(frozen=True)
class BrokerPosition:
    instrument_key: str
    product: str  # the broker's code
    quantity: int  # net, signed
    average_price: float


class Broker(Protocol):
    async def place(self, order: BrokerOrder) -> str:
        """Send the order once; the broker's order id."""
        ...

    async def cancel(self, order_id: str) -> None: ...

    async def order(self, order_id: str) -> BrokerState: ...

    async def positions(self) -> list[BrokerPosition]: ...
