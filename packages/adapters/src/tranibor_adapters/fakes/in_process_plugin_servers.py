# SPDX-License-Identifier: Apache-2.0
"""A plugin's server run inside the API's process, on its own fake, for a demonstration (ADR 0218).

A plugin's server is an MCP server image the platform runs as a Kubernetes Deployment (ADR 0096). On a
laptop there is no cluster, and the in-memory Kubernetes API of the fakes runs nothing. When the
deployment says `TRANIBOR_DEMO_PLUGIN_SERVERS=<file>` (fakes on, `TRANIBOR_ENV` `dev` or `test`, refused
elsewhere: `tranibor_core.production.refuse_demo_plugin_servers`), the server of a plugin that declares
its `fake` is instead run from the plugin's OWN Python package, imported by the API, behind an
`httpx.ASGITransport`: no container, no Docker socket, no network listener. The broker, discovery, the
operation policies and the approvals are the product's own; only where the server runs changes.

The file says where each server's package is and how to build its ASGI application:

    servers:
      - plugin: servicenow
        server: servicenow
        path: /work/tranibor-agents/plugins/servicenow/server/src   # put on sys.path (omit: installed)
        app: choregos_servicenow.app:build_app                       # module:callable, called with settings
        settings: choregos_servicenow.config:Settings.from_env       # module:callable(env) -> settings
        fake_env: {SERVICENOW_FAKE_SEED: /work/seed.json}            # beyond the plugin's `fake.env`

`app(settings(env))` builds the application (`app(env)` when `settings` is absent) with `env` made of the
declaration's `fake.env`, the file's `fake_env` and the platform's token (`TRANIBOR_SERVER_TOKEN`, which
the application checks as it does behind a Service). The core never ships nor imports a plugin: the file
names code the operator put on the machine.

The application's ASGI lifespan runs in a task of its own, for the life of the process (`stop` ends it).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
import pathlib
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import yaml
from tranibor_core.production import (
    DEMO_PLUGIN_SERVERS_VARIABLE,
    ENVIRONMENT_VARIABLE,
    FAKES_VARIABLE,
    refuse_demo_plugin_servers,
)

from ..errors import ConfigurationError
from ..plugin_servers import INSTANCE_ENV, TOKEN_ENV

#: The domain the in-process servers answer on: a name no resolver serves, so a mistake never leaves the
#: process. The first label is the server's object name.
HOST_SUFFIX = ".in-process.invalid"
_ENV = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_REF = re.compile(r"^[A-Za-z_][\w.]*:[A-Za-z_][\w.]*$")
_KEYS = {"plugin", "server", "path", "app", "settings", "fake_env"}
_OPTIONAL = "`path`, `settings`, `fake_env`"
STARTUP_TIMEOUT_S = 30.0


def url_of(object_name: str, path: str) -> str:
    """Where the server of `object_name` answers MCP: its name under `HOST_SUFFIX`."""
    return f"http://{object_name}{HOST_SUFFIX}{path}"


@dataclass(frozen=True)
class InProcessEntry:
    """One server's package, as the operator's file says it."""

    plugin: str
    server: str
    app: str
    settings: str | None = None
    path: str | None = None
    fake_env: dict[str, str] = field(default_factory=dict)


def _refuse(where: str, why: str) -> ConfigurationError:
    return ConfigurationError(f"{DEMO_PLUGIN_SERVERS_VARIABLE} {where}: {why}")


def check_env_name(name: str, where: str) -> None:
    if not _ENV.match(name) or name.startswith(("TRANIBOR_", "CHOREGOS_")):  # compat-ok
        raise _refuse(
            where, f"`{name}` is not a variable a server may receive (upper case, not `TRANIBOR_*`)"
        )


def load_registry(path: str | os.PathLike[str]) -> dict[tuple[str, str], InProcessEntry]:
    """The operator's file read; refused outside development under the fakes, whatever the caller did."""
    refuse_demo_plugin_servers(
        environment=os.environ.get(ENVIRONMENT_VARIABLE, "dev"), fakes=os.environ.get(FAKES_VARIABLE)
    )
    where = str(path)
    try:
        document = yaml.safe_load(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _refuse(where, f"cannot be read ({exc.__class__.__name__})") from exc
    listed = document.get("servers") if isinstance(document, dict) else None
    if not isinstance(listed, list) or not listed:
        raise _refuse(where, "holds `servers: [{plugin, server, app, ...}]`")
    found: dict[tuple[str, str], InProcessEntry] = {}
    for raw in listed:
        if not isinstance(raw, dict) or set(raw) - _KEYS or not {"plugin", "server", "app"} <= set(raw):
            raise _refuse(where, f"a server holds `plugin`, `server`, `app` and optionally {_OPTIONAL}")
        for key in ("app", "settings"):
            if raw.get(key) is not None and not _REF.match(str(raw[key])):
                raise _refuse(where, f"`{key}: {raw[key]}` is not `module:callable`")
        env = {str(k): str(v) for k, v in (raw.get("fake_env") or {}).items()}
        for name in env:
            check_env_name(name, where)
        entry = InProcessEntry(
            str(raw["plugin"]),
            str(raw["server"]),
            str(raw["app"]),
            str(raw["settings"]) if raw.get("settings") else None,
            str(raw["path"]) if raw.get("path") else None,
            env,
        )
        if (entry.plugin, entry.server) in found:
            raise _refuse(where, f"`{entry.plugin}/{entry.server}` is listed twice")
        found[(entry.plugin, entry.server)] = entry
    return found


def _resolve(ref: str) -> Callable[..., Any]:
    module, _, attribute = ref.partition(":")
    target: Any = importlib.import_module(module)
    for part in attribute.split("."):
        target = getattr(target, part)
    if not callable(target):
        raise ConfigurationError(f"`{ref}` is not callable")
    return target  # type: ignore[no-any-return]


class _Running:
    """An application and the task that holds its ASGI lifespan open."""

    def __init__(self, app: Any, env: Mapping[str, str], loop: asyncio.AbstractEventLoop) -> None:
        self.app, self.env, self.loop = app, dict(env), loop
        self.transport = httpx.ASGITransport(app=app)
        self._inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._started = asyncio.Event()
        self._stopped = asyncio.Event()
        self._failure: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._lifespan())
        await self._inbox.put({"type": "lifespan.startup"})
        await asyncio.wait_for(self._started.wait(), STARTUP_TIMEOUT_S)
        if self._failure is not None:
            raise ConfigurationError(f"the server's application failed to start: {self._failure}")

    async def _lifespan(self) -> None:
        async def send(message: dict[str, Any]) -> None:
            kind = message["type"]
            if kind == "lifespan.startup.failed":
                self._failure = str(message.get("message") or "startup failed")
                self._started.set()
            elif kind == "lifespan.startup.complete":
                self._started.set()
            elif kind in ("lifespan.shutdown.complete", "lifespan.shutdown.failed"):
                self._stopped.set()

        try:
            await self.app({"type": "lifespan", "asgi": {"version": "3.0"}}, self._inbox.get, send)
        except Exception:  # an application without lifespan support raises: it needs none
            pass
        finally:
            self._started.set()
            self._stopped.set()

    async def stop(self) -> None:
        if self._task is None or self._task.done():
            return
        await self._inbox.put({"type": "lifespan.shutdown"})
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopped.wait(), STARTUP_TIMEOUT_S)
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._task


class InProcessPluginServers:
    """The servers of an operator's file, started when first asked for and answering through a transport."""

    def __init__(self, registry: Mapping[tuple[str, str], InProcessEntry]) -> None:
        self.registry = dict(registry)
        self._running: dict[str, _Running] = {}
        self._lock = asyncio.Lock()

    def entry(self, plugin: str, server: str) -> InProcessEntry | None:
        return self.registry.get((plugin, server))

    async def ensure(
        self, object_name: str, entry: InProcessEntry, declared_env: Mapping[str, str], token: str
    ) -> None:
        """`object_name` running with `declared_env` (the declaration's `fake.env`), the file's `fake_env`
        and `token`; a replay with the same environment is a no-op, a changed one restarts it."""
        env = {**declared_env, **entry.fake_env, TOKEN_ENV: token}
        if INSTANCE_ENV in declared_env:  # the platform's, set last like the token
            env[INSTANCE_ENV] = declared_env[INSTANCE_ENV]
        loop = asyncio.get_running_loop()
        async with self._lock:
            current = self._running.get(object_name)
            if current is not None and current.env == env and current.loop is loop:
                return
            if current is not None:
                await self._stop(object_name)
            self._running[object_name] = await self._start(entry, env, loop)

    async def _start(
        self, entry: InProcessEntry, env: dict[str, str], loop: asyncio.AbstractEventLoop
    ) -> _Running:
        if entry.path and entry.path not in sys.path:
            sys.path.insert(0, entry.path)
        try:
            build = _resolve(entry.app)
            settings = _resolve(entry.settings)(env) if entry.settings else env
            app = build(settings)
        except (ImportError, AttributeError, ValueError, TypeError) as exc:
            raise ConfigurationError(
                f"the server {entry.plugin}/{entry.server} cannot be built from `{entry.app}`: {exc} "
                f"(is its package importable? `path` in the {DEMO_PLUGIN_SERVERS_VARIABLE} file)"
            ) from exc
        running = _Running(app, env, loop)
        await running.start()
        return running

    async def _stop(self, object_name: str) -> None:
        running = self._running.pop(object_name, None)
        if running is not None and running.loop is asyncio.get_running_loop():
            await running.stop()

    async def stop(self, object_name: str | None = None) -> None:
        """One server stopped, or every one (the API's shutdown, a test's teardown)."""
        async with self._lock:
            for name in [object_name] if object_name else list(self._running):
                await self._stop(name)

    def transport(self, otherwise: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncBaseTransport:
        """What the broker's calls go through: a call to `<object>.in-process.invalid` reaches that
        server's application; any other goes to `otherwise`."""
        return _Dispatch(self, otherwise)


class _Dispatch(httpx.AsyncBaseTransport):
    def __init__(self, servers: InProcessPluginServers, otherwise: httpx.AsyncBaseTransport | None) -> None:
        self.servers, self.otherwise = servers, otherwise

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        if host.endswith(HOST_SUFFIX):
            running = self.servers._running.get(host.removesuffix(HOST_SUFFIX))
            if running is None:
                return httpx.Response(503, json={"error": "the in-process server is not running"})
            return await running.transport.handle_async_request(request)
        if self.otherwise is None:
            return httpx.Response(502, json={"error": "no transport for this host"})
        return await self.otherwise.handle_async_request(request)


__all__ = [
    "HOST_SUFFIX",
    "InProcessEntry",
    "InProcessPluginServers",
    "check_env_name",
    "load_registry",
    "url_of",
]
