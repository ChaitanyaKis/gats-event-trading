"""Upstox order API (T8.1). DOCUMENTED 2026-10-04, NOT PROBED and not to be
probed by Claude: a probe of this API is a real order.

From upstox.com/developer/api-documentation:

- **Place (V3):** ``POST https://api-hft.upstox.com/v3/order/place`` with
  ``Authorization: Bearer <access token>`` and a JSON body: ``quantity``,
  ``product`` (``I`` intraday, ``D`` delivery, ``MTF``), ``validity``
  (``DAY``, ``IOC``), ``price``, ``tag`` (optional), ``instrument_token``,
  ``order_type`` (``MARKET``, ``LIMIT``, ``SL``, ``SL-M``),
  ``transaction_type`` (``BUY``, ``SELL``), ``disclosed_quantity``,
  ``trigger_price``, ``is_amo``, ``slice``. Reply: ``{"status": "success",
  "data": {"order_ids": [...]}, "metadata": {"latency": 30}}``.
- **Cancel (V3):** ``DELETE https://api-hft.upstox.com/v3/order/cancel
  ?order_id=...``; reply ``{"status": "success", "data": {"order_id": ..}}``.
- **Order details:** ``GET https://api.upstox.com/v2/order/details
  ?order_id=...``; ``data`` has ``status`` (the example shows ``complete``;
  the full list is in an appendix not read here), ``filled_quantity``,
  ``pending_quantity``, ``average_price``, ``tag``, ``status_message``.
- **Positions:** ``GET https://api.upstox.com/v2/portfolio/
  short-term-positions``; each has ``instrument_token``, ``product``,
  ``quantity``, ``average_price``.

The docs name error ``UDAPI1154`` for a request from an IP that is not the
registered static one (a HUMAN step before any live order).

GATS sends only LIMIT orders, never MARKET, never sliced, never after-market
(``is_amo`` false): one request is one order with a price it cannot exceed.
The access token is the broker's daily OAuth token (the Analytics Token
cannot trade); it travels only in the ``Authorization`` header.
"""

from __future__ import annotations

import json
from typing import Any

from gats.broker.base import (
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    BrokerState,
    OutcomeUnknown,
)
from gats.db import repo
from gats.ingest import Services
from gats.net import Fetched, FetchError

SOURCE = "UPSTOX"
ORDER_KIND = "upstox_order"
_PRODUCT = {"intraday": "I", "delivery": "D"}


class TokenMissing(RuntimeError):
    """Orders need the broker's access token (a HUMAN step)."""


def place_body(order: BrokerOrder) -> dict[str, Any]:
    """The JSON body of one protected limit order."""
    if order.quantity <= 0 or order.limit_price <= 0:
        raise ValueError(f"not an order: {order}")
    return {
        "quantity": order.quantity,
        "product": _PRODUCT[order.product],
        "validity": "DAY",
        "price": round(order.limit_price, 2),
        "tag": order.client_id,
        "instrument_token": order.instrument_key,
        "order_type": "LIMIT",
        "transaction_type": order.side.upper(),
        "disclosed_quantity": 0,
        "trigger_price": 0,
        "is_amo": False,
        "slice": False,
    }


def _data(payload: bytes, what: str) -> Any:
    try:
        body = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BrokerError(f"{what}: reply is not JSON ({exc})") from exc
    if not isinstance(body, dict) or body.get("status") != "success":
        errors = body.get("errors") if isinstance(body, dict) else None
        raise BrokerError(f"{what}: refused: {str(errors or body)[:300]}")
    return body.get("data")


def parse_place(payload: bytes) -> str:
    """The order id of a placed order (exactly one: orders are not sliced)."""
    data = _data(payload, "place order")
    ids = data.get("order_ids") if isinstance(data, dict) else None
    if not isinstance(ids, list) or len(ids) != 1 or not ids[0]:
        raise BrokerError(f"place order: expected one order id, got {ids!r}")
    return str(ids[0])


def parse_order(payload: bytes) -> BrokerState:
    data = _data(payload, "order details")
    if not isinstance(data, dict) or not data.get("order_id"):
        raise BrokerError("order details: no order in the reply")
    average = data.get("average_price")
    return BrokerState(
        order_id=str(data["order_id"]),
        status=str(data.get("status") or ""),
        filled_quantity=int(data.get("filled_quantity") or 0),
        pending_quantity=int(data.get("pending_quantity") or 0),
        average_price=float(average) if average else None,
        tag=data.get("tag"),
        message=data.get("status_message"),
    )


def parse_positions(payload: bytes) -> list[BrokerPosition]:
    data = _data(payload, "positions")
    if not isinstance(data, list):
        raise BrokerError("positions: expected a list")
    out = []
    for row in data:
        if not isinstance(row, dict) or not row.get("instrument_token"):
            raise BrokerError(f"positions: unexpected row {str(row)[:120]}")
        out.append(
            BrokerPosition(
                instrument_key=str(row["instrument_token"]),
                product=str(row.get("product") or ""),
                quantity=int(row.get("quantity") or 0),
                average_price=float(row.get("average_price") or 0.0),
            )
        )
    return out


class UpstoxBroker:
    """The real thing. Constructed only by the live runtime, after the gate."""

    def __init__(self, svc: Services) -> None:
        self._svc = svc

    def _headers(self) -> dict[str, str]:
        token = self._svc.settings.upstox_access_token
        if token is None or not token.get_secret_value().strip():
            raise TokenMissing(
                "GATS_UPSTOX_ACCESS_TOKEN is not set: the human adds the broker's access token "
                "to .env (docs/RUNBOOK_LIVE.md)."
            )
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token.get_secret_value().strip()}",
        }

    def _keep(self, got: Fetched, what: str) -> None:
        """Raw first: every reply about an order is stored as received."""
        with self._svc.engine.begin() as conn:
            repo.save_raw(
                conn, self._svc.store, got.content, kind=ORDER_KIND, source=SOURCE, url=got.url,
                content_type=got.content_type, fetched_at=got.fetched_at,
                meta={"what": what, "http_status": got.status},
            )  # fmt: skip

    async def place(self, order: BrokerOrder) -> str:
        s = self._svc.settings
        url = f"{s.upstox_order_base.rstrip('/')}/v3/order/place"
        try:
            # Sent once. A retry of a request that got through is a second order.
            got = await self._svc.client.post_json(
                url, place_body(order), headers=self._headers(), retries=0
            )
        except FetchError as exc:
            raise OutcomeUnknown(f"place {order.client_id}: {exc}") from exc
        self._keep(got, f"place {order.client_id}")
        if got.status >= 500:
            raise OutcomeUnknown(f"place {order.client_id}: HTTP {got.status}")
        if not got.ok:
            raise BrokerError(f"place {order.client_id}: HTTP {got.status}: {got.content[:300]!r}")
        return parse_place(got.content)

    async def cancel(self, order_id: str) -> None:
        s = self._svc.settings
        url = f"{s.upstox_order_base.rstrip('/')}/v3/order/cancel"
        try:
            got = await self._svc.client.delete(
                url, params={"order_id": order_id}, headers=self._headers(), retries=0
            )
        except FetchError as exc:
            raise OutcomeUnknown(f"cancel {order_id}: {exc}") from exc
        self._keep(got, f"cancel {order_id}")
        if not got.ok:
            raise BrokerError(f"cancel {order_id}: HTTP {got.status}: {got.content[:300]!r}")
        _data(got.content, "cancel order")

    async def order(self, order_id: str) -> BrokerState:
        s = self._svc.settings
        url = f"{s.upstox_api_base.rstrip('/')}/v2/order/details"
        try:
            got = await self._svc.client.get(
                url, params={"order_id": order_id}, headers=self._headers()
            )
        except FetchError as exc:
            raise BrokerError(f"order details {order_id}: {exc}") from exc
        self._keep(got, f"details {order_id}")
        if not got.ok:
            raise BrokerError(f"order details {order_id}: HTTP {got.status}")
        return parse_order(got.content)

    async def positions(self) -> list[BrokerPosition]:
        s = self._svc.settings
        url = f"{s.upstox_api_base.rstrip('/')}/v2/portfolio/short-term-positions"
        try:
            got = await self._svc.client.get(url, headers=self._headers())
        except FetchError as exc:
            raise BrokerError(f"positions: {exc}") from exc
        self._keep(got, "positions")
        if not got.ok:
            raise BrokerError(f"positions: HTTP {got.status}")
        return parse_positions(got.content)
