"""Polite HTTP client for exchange websites.

- At most one request per ``min_interval_s`` per host.
- Exponential backoff with jitter on 408/429/5xx and network errors,
  honouring ``Retry-After``.
- Optional cookie warm-up (NSE only serves its JSON API to sessions that
  first loaded the homepage). A 401/403 triggers one re-warm and retry.
- 404 and other client errors are returned, not raised, so callers decide
  (a missing daily file on a holiday is normal, not an error).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import httpx

from gats.logging_setup import kv
from gats.timeutil import utcnow

log = logging.getLogger(__name__)

_RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_WARMUP_STATUSES = frozenset({401, 403})

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Fetched:
    url: str
    status: int
    content: bytes
    content_type: str | None
    fetched_at: datetime
    elapsed_ms: int
    too_large: bool = False  # body exceeded ``max_bytes``; ``content`` is empty

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300 and not self.too_large


class FetchError(RuntimeError):
    """Transport failure or retryable status that persisted after all retries."""

    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(f"{message} ({url})")
        self.url = url
        self.status = status


class _HostThrottle:
    def __init__(self, min_interval_s: float, per_host: Mapping[str, float] | None = None) -> None:
        self._min_interval = min_interval_s
        self._per_host = dict(per_host or {})
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}

    async def wait(self, host: str, sleep: Sleep) -> None:
        lock = self._locks.setdefault(host, asyncio.Lock())
        interval = self._per_host.get(host, self._min_interval)
        async with lock:
            last = self._last.get(host)
            if last is not None:
                remaining = interval - (time.monotonic() - last)
                if remaining > 0:
                    await sleep(remaining)
            self._last[host] = time.monotonic()


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - utcnow()).total_seconds())


async def _read_capped(response: httpx.Response, max_bytes: int | None) -> tuple[bytes, bool]:
    """Read the body, giving up early if it exceeds ``max_bytes``."""
    if max_bytes is not None:
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > max_bytes:
            return b"", True
    body = bytearray()
    async for chunk in response.aiter_bytes():
        body.extend(chunk)
        if max_bytes is not None and len(body) > max_bytes:
            return b"", True
    return bytes(body), False


class PoliteClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_s: float = 20.0,
        min_interval_s: float = 1.0,
        host_min_interval_s: Mapping[str, float] | None = None,
        max_retries: int = 4,
        backoff_base_s: float = 2.0,
        backoff_max_s: float = 60.0,
        sleep: Sleep = asyncio.sleep,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": user_agent,
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._throttle = _HostThrottle(min_interval_s, host_min_interval_s)
        self._max_retries = max_retries
        self._backoff_base = backoff_base_s
        self._backoff_max = backoff_max_s
        self._sleep = sleep
        self._warmed: set[str] = set()

    async def __aenter__(self) -> PoliteClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _backoff(self, attempt: int) -> float:
        delay = min(self._backoff_max, self._backoff_base * (2**attempt))
        return float(delay * random.uniform(0.5, 1.0))

    async def warmup(self, url: str, *, force: bool = False) -> None:
        """Load ``url`` to obtain session cookies. Cached unless ``force``."""
        if url in self._warmed and not force:
            return
        try:
            await self._request(url, params=None, headers={"Accept": "text/html"})
        except FetchError as exc:
            log.warning("warmup failed %s", kv(url=url, error=str(exc)))
            return
        self._warmed.add(url)

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        warmup_url: str | None = None,
        max_bytes: int | None = None,
    ) -> Fetched:
        """GET with retries. With ``max_bytes``, the download is abandoned as
        soon as the declared or received size exceeds it (``too_large``)."""
        if warmup_url:
            await self.warmup(warmup_url)
        fetched = await self._request(url, params=params, headers=headers, max_bytes=max_bytes)
        if warmup_url and fetched.status in _WARMUP_STATUSES:
            log.info("session rejected, re-warming %s", kv(url=url, status=fetched.status))
            await self.warmup(warmup_url, force=True)
            fetched = await self._request(url, params=params, headers=headers, max_bytes=max_bytes)
        return fetched

    async def _request(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        max_bytes: int | None = None,
    ) -> Fetched:
        host = urlsplit(url).netloc
        last_error: str = "no attempt made"
        last_status: int | None = None
        for attempt in range(self._max_retries + 1):
            await self._throttle.wait(host, self._sleep)
            started = time.monotonic()
            fetched_at = utcnow()
            try:
                async with self._client.stream(
                    "GET", url, params=params, headers=headers
                ) as response:
                    if response.status_code in _RETRY_STATUSES:
                        last_error = f"HTTP {response.status_code}"
                        last_status = response.status_code
                        delay = _retry_after_seconds(response) or self._backoff(attempt)
                        delay = min(delay, self._backoff_max)
                    else:
                        body, too_large = await _read_capped(response, max_bytes)
                        return Fetched(
                            url=str(response.url),
                            status=response.status_code,
                            content=body,
                            content_type=response.headers.get("Content-Type"),
                            fetched_at=fetched_at,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            too_large=too_large,
                        )
            except httpx.TransportError as exc:
                last_error, last_status = f"{type(exc).__name__}: {exc}", None
                delay = self._backoff(attempt)
            if attempt < self._max_retries:
                log.warning(
                    "retrying %s",
                    kv(url=url, attempt=attempt + 1, error=last_error, delay_s=round(delay, 1)),
                )
                await self._sleep(delay)
        raise FetchError(url, last_error, status=last_status)
