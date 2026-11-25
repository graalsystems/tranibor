# SPDX-License-Identifier: Apache-2.0
"""The object store's port and errors, shared by every implementation (ADR 0049)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

#: Seconds a readiness probe waits: a store that does not answer by then is not ready.
CHECK_TIMEOUT = 5.0
#: Seconds before a temporary credential expires at which it is fetched again: a request signed with
#: it still has time to reach the store, and a slow identity endpoint has time to answer.
REFRESH_MARGIN = 300.0


class ObjectStoreError(RuntimeError):
    """The object store refused or could not be reached."""


class ObjectNotFoundError(ObjectStoreError):
    """No object under this key."""


@runtime_checkable
class ObjectStore(Protocol):
    """Content in, content out, by key. Keys are relative to the store's bucket and prefix."""

    def url_of(self, key: str) -> str:
        """The stable address of a key, recorded with the artifact (`s3://bucket/prefix/key`)."""
        ...

    async def put(self, key: str, data: bytes, media_type: str) -> str:
        """Writes the object (overwriting is harmless: keys are content-addressed); returns its URL."""
        ...

    async def get(self, key: str) -> bytes:
        """Reads the object; `ObjectNotFoundError` when it is absent."""
        ...

    async def delete(self, key: str) -> None:
        """Removes the object; idempotent: an absent object is not an error (a retried erasure, a purge
        that ran twice). `ObjectStoreError` when the store refuses or cannot be reached."""
        ...

    async def check(self) -> None:
        """A cheap probe that creates nothing: returns when the store answers for its bucket, raises
        `ObjectStoreError` (never carrying a key or a secret) when it does not."""
        ...


class Refreshing[T]:
    """A temporary credential, fetched once and again `margin` seconds before it expires.

    `fetch` returns the value and its expiry (epoch seconds; `None` for a value that does not
    expire). Concurrent callers wait for one fetch rather than each asking the identity endpoint."""

    def __init__(
        self,
        fetch: Callable[[], Awaitable[tuple[T, float | None]]],
        *,
        margin: float = REFRESH_MARGIN,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch
        self._margin = margin
        self._clock = clock
        self._value: T | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()
        #: How many times the identity endpoint was asked (tests read it).
        self.fetches = 0

    def _fresh(self) -> T | None:
        if self._value is not None and self._clock() < self._expires_at - self._margin:
            return self._value
        return None

    async def get(self) -> T:
        fresh = self._fresh()
        if fresh is not None:
            return fresh
        async with self._lock:
            fresh = self._fresh()
            if fresh is not None:
                return fresh
            value, expires_at = await self._fetch()
            self.fetches += 1
            self._value = value
            self._expires_at = float("inf") if expires_at is None else expires_at
            return value


async def read_token_file(path: str, what: str) -> str:
    """A projected token, read again at each fetch (the kubelet rotates it); never echoed in an error."""
    try:
        token = (await asyncio.to_thread(Path(path).read_text, encoding="utf-8")).strip()
    except OSError as exc:
        raise ObjectStoreError(f"{what}: cannot read the token file {path} ({exc.strerror})") from exc
    if not token:
        raise ObjectStoreError(f"{what}: the token file {path} is empty")
    return token


async def send(
    client: httpx.AsyncClient | None,
    method: str,
    url: str,
    *,
    what: str,
    timeout: float,
    content: bytes | None = None,
    headers: dict[str, str] | None = None,
    data: dict[str, str] | None = None,
) -> httpx.Response:
    """One HTTP call through the given client, or a short-lived one; a transport failure becomes an
    `ObjectStoreError` naming `what` was unreachable (never the request's headers)."""
    own = client or httpx.AsyncClient(timeout=timeout)
    try:
        return await own.request(method, url, content=content, headers=headers, data=data)
    except httpx.HTTPError as exc:
        raise ObjectStoreError(f"{what} unreachable: {exc}") from exc
    finally:
        if client is None:
            await own.aclose()


__all__ = [
    "CHECK_TIMEOUT",
    "REFRESH_MARGIN",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreError",
    "Refreshing",
    "read_token_file",
    "send",
]
