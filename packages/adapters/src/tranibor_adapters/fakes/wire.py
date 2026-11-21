# SPDX-License-Identifier: Apache-2.0
"""A simulated HTTP transport that remembers everything that went out on the wire.

The adapters' tests drive a real adapter against it, and so do the tests of an extension's connector
types (ADR 0181, round D: the `software-delivery` plugin's `ci` and `cd`): it is shipped with the fakes, so
that a plugin's tests use the same one. It was `packages/adapters/tests/_transport.py`."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

Reply = tuple[int, Any] | httpx.Response | Exception


@dataclass
class Wire:
    """`routes`: (method, path) -> reply, or a list of replies consumed in order.

    The path is compared without the base: `/repos/x/y`. An absent route answers 404, like a
    server that does not know the resource, and that is often what the test wants.
    """

    routes: dict[tuple[str, str], Reply | list[Reply]] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._reply))

    def _reply(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        reply = self.routes.get(key)
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if reply is None:
            return httpx.Response(404, json={"message": "Not Found"})
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        status, content = reply
        if content is None:
            return httpx.Response(status)
        if isinstance(content, str):
            return httpx.Response(status, text=content)
        return httpx.Response(status, json=content)

    def body(self, index: int = -1) -> Any:
        return json.loads(self.requests[index].content)

    def sent(self, method: str | None = None) -> list[tuple[str, str]]:
        return [(r.method, r.url.path) for r in self.requests if method is None or r.method == method]


def no_wait(monkeypatch: Any, module: Any) -> list[float]:
    """Replaces the module's `asyncio.sleep` with a recorder: backoffs can be read, they do not last."""
    waits: list[float] = []

    async def _sleep(delay: float) -> None:
        waits.append(delay)

    monkeypatch.setattr(module.asyncio, "sleep", _sleep)
    return waits


def handler(fn: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(fn))
