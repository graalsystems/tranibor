# SPDX-License-Identifier: Apache-2.0
"""The runner's loopback listeners for a hosted step (ADR 0080): model, MCP and CONNECT.

`Loopback` serves the three on `127.0.0.1` for the step's duration. The container beside the runner
shares the pod's network namespace and reaches the platform only through them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

Record = Callable[[str, dict[str, Any]], Awaitable[None]]


class Loopback:
    """The three listeners, started together and stopped together. `model=None`: no model listener, for a run
    on its person's subscription, whose vendor CLI reaches its provider itself, through the CONNECT tunnel
    (ADR 0227 §5)."""

    def __init__(
        self,
        model: Any | None,
        mcp: Any,
        connect: Any,
        *,
        host: str = "127.0.0.1",
        ports: tuple[int, int, int] = (4001, 4002, 4003),
    ) -> None:
        self.model, self.mcp, self.connect = model, mcp, connect
        self.host, self.ports = host, ports
        self._servers: list[Any] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._tunnels: asyncio.Server | None = None

    async def __aenter__(self) -> Loopback:
        import uvicorn

        listeners = [(self.mcp.app(), self.ports[1])]
        if self.model is not None:
            listeners.insert(0, (self.model.app(), self.ports[0]))
        for app, port in listeners:
            server = uvicorn.Server(
                uvicorn.Config(
                    app, host=self.host, port=port, log_level="warning", lifespan="off", access_log=False
                )
            )
            # The runner's own SIGTERM handling stays the runner's: the listeners do not capture it.
            server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]
            self._servers.append(server)
            self._tasks.append(asyncio.create_task(server.serve()))
        self._tunnels = await asyncio.start_server(self.connect.handle, self.host, self.ports[2])
        while not all(server.started for server in self._servers):
            if any(task.done() for task in self._tasks):
                await self.__aexit__(None, None, None)
                ports = [port for _, port in listeners]
                raise OSError(f"a loopback listener could not start on {self.host}:{ports}")
            await asyncio.sleep(0.01)
        return self

    async def __aexit__(self, *_: object) -> None:
        for server in self._servers:
            server.should_exit = True
        for task in self._tasks:
            with contextlib.suppress(Exception):
                await task
        if self._tunnels is not None:
            self._tunnels.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._tunnels.wait_closed(), 5)
        await self.connect.flush()
        if self.model is not None:
            await self.model.aclose()
        await self.mcp.aclose()
