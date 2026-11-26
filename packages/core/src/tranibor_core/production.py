# SPDX-License-Identifier: Apache-2.0
"""What production is, and the fakes it refuses (S80-03).

Production is `TRANIBOR_ENV` set to `staging`, `prod` or `personal` (the chart's `global.environment`
for the first two; `personal` is one person's own installation, ADR 0228): the same notion that refuses
a PostgreSQL superuser, the development sign-in and the demonstration connectors there. Nothing else says
"production": no second variable, no guess from a URL, and no other module spells the set
(`tests/architecture/test_production_is_named_once.py`).

`TRANIBOR_FAKES` (ADR 0005) switches the whole platform to its in-memory fakes. That is what the
tests, the end-to-end suite and `make demo` run on, and it is a bench's switch only: on, it relaxes the
network egress control to `warn` (`tranibor_core.egress.default_mode`) and hands the API an in-memory
secret store when no key is configured (`tranibor_api.secret_store`). Every process that reads it, the
API, the outposts' door, the orchestrator's workers and the runner, calls `refuse_fakes_in_production`
before it serves anything, and stops there with the setting named.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: The posture of one person's own installation (ADR 0228): production, with rules of its own on top.
PERSONAL_ENVIRONMENT = "personal"
#: The `TRANIBOR_ENV` values that are production. The one place that spells them.
PRODUCTION_ENVIRONMENTS = frozenset({"staging", "prod", PERSONAL_ENVIRONMENT})
#: The variable that turns the fakes on, and the one that names the environment.
FAKES_VARIABLE = "TRANIBOR_FAKES"
ENVIRONMENT_VARIABLE = "TRANIBOR_ENV"
#: What pydantic reads as true for a boolean setting: the fakes are on for any of them.
_TRUE = frozenset({"1", "true", "yes", "on", "y", "t"})


class FakesInProductionError(RuntimeError):
    """A process was asked to start with the fakes on in a production environment."""


def is_production(environment: str | None) -> bool:
    return (environment or "").strip().lower() in PRODUCTION_ENVIRONMENTS


def is_personal(environment: str | None) -> bool:
    """Whether `TRANIBOR_ENV` names a personal installation (ADR 0228), which is also production."""
    return (environment or "").strip().lower() == PERSONAL_ENVIRONMENT


def fakes_on(value: str | bool | None) -> bool:
    """Whether a `TRANIBOR_FAKES` value turns the fakes on, read as the settings read it."""
    if isinstance(value, bool):
        return value
    return (value or "").strip().lower() in _TRUE


def refuse_fakes_in_production(*, environment: str | None, fakes: str | bool | None, process: str) -> None:
    """Stop `process` when the fakes are on in production, naming both settings and how to fix it."""
    if not (fakes_on(fakes) and is_production(environment)):
        return
    raise FakesInProductionError(
        f"{FAKES_VARIABLE} is on and {ENVIRONMENT_VARIABLE}={(environment or '').strip()}: the {process} "
        "refuses to start. The fakes (ADR 0005) relax the network egress control to `warn` and replace "
        "the secret store with an in-memory one; they are for dev, test and the demo only. Unset "
        f"{FAKES_VARIABLE} (the chart's `global.fakes: false`), or run this process outside staging and prod."
    )


def refuse_fakes_in_this_environment(process: str, environ: Mapping[str, str] | None = None) -> None:
    """`refuse_fakes_in_production`, read from a process environment (`os.environ` by default)."""
    values = os.environ if environ is None else environ
    refuse_fakes_in_production(
        environment=values.get(ENVIRONMENT_VARIABLE), fakes=values.get(FAKES_VARIABLE), process=process
    )


#: The variable that opts the API into running the orchestrator's workers inside its own process (ADR 0212).
DEMO_INLINE_WORKER_VARIABLE = "TRANIBOR_DEMO_INLINE_WORKER"
#: The only environment the inline demo worker serves in: `TRANIBOR_ENV` is `dev` (its default) or `test`.
DEMO_INLINE_WORKER_ENVIRONMENTS = frozenset({"dev", "test"})


class DemoInlineWorkerRefusedError(RuntimeError):
    """The API was asked to run the inline demo worker where it must not run."""


def refuse_demo_inline_worker(*, environment: str | None, fakes: str | bool | None) -> None:
    """Stop the API when `TRANIBOR_DEMO_INLINE_WORKER` is on outside development under the fakes.

    The inline worker is a presentation aid (ADR 0212): one process, an in-memory Temporal test server,
    no durability. It needs the fakes (so that no agent, tracker or repository is real) and a `dev` or
    `test` environment; anything else is refused, naming the setting to change.
    """
    env = (environment or "").strip().lower()
    if env not in DEMO_INLINE_WORKER_ENVIRONMENTS:
        raise DemoInlineWorkerRefusedError(
            f"{DEMO_INLINE_WORKER_VARIABLE} is on and {ENVIRONMENT_VARIABLE}={env or '(unset)'}: the API "
            "refuses to start. The inline demo worker (ADR 0212) has no durability and is for a "
            f"development machine only: unset {DEMO_INLINE_WORKER_VARIABLE}, or run with "
            f"{ENVIRONMENT_VARIABLE}=dev."
        )
    if not fakes_on(fakes):
        raise DemoInlineWorkerRefusedError(
            f"{DEMO_INLINE_WORKER_VARIABLE} is on and {FAKES_VARIABLE} is off: the API refuses to start. "
            "The inline demo worker plays a simulated agent against fake adapters (ADR 0005, ADR 0212); "
            f"set {FAKES_VARIABLE}=1, or unset {DEMO_INLINE_WORKER_VARIABLE} and run a real worker."
        )


#: The variable that names the file of tools the demonstration MCP server serves (ADR 0213).
DEMO_MCP_TOOLS_VARIABLE = "TRANIBOR_DEMO_MCP_TOOLS"


class DemoMcpToolsRefusedError(RuntimeError):
    """The demonstration MCP server was asked for where it must not run."""


def refuse_demo_mcp_tools(*, environment: str | None, fakes: str | bool | None) -> None:
    """Stop a process when `TRANIBOR_DEMO_MCP_TOOLS` is set outside development under the fakes.

    The demonstration MCP server (ADR 0213) answers a canned text to anyone who asks and checks no
    credential: it is the fakes' MCP server with tools declared from a file. It needs the fakes (so that
    nothing real is reached) and a `dev` or `test` environment; anything else is refused, naming the
    setting to change.
    """
    env = (environment or "").strip().lower()
    if env not in DEMO_INLINE_WORKER_ENVIRONMENTS:
        raise DemoMcpToolsRefusedError(
            f"{DEMO_MCP_TOOLS_VARIABLE} is set and {ENVIRONMENT_VARIABLE}={env or '(unset)'}: refused. The "
            "demonstration MCP server (ADR 0213) checks no credential and is for a development machine "
            f"only: unset {DEMO_MCP_TOOLS_VARIABLE}, or run with {ENVIRONMENT_VARIABLE}=dev."
        )
    if not fakes_on(fakes):
        raise DemoMcpToolsRefusedError(
            f"{DEMO_MCP_TOOLS_VARIABLE} is set and {FAKES_VARIABLE} is off: refused. The demonstration MCP "
            f"server is the fakes' (ADR 0005, ADR 0213); set {FAKES_VARIABLE}=1, or unset "
            f"{DEMO_MCP_TOOLS_VARIABLE} and connect a real MCP server."
        )


#: The variable that lets the API take in a demonstration history (ADR 0216).
DEMO_HISTORY_IMPORT_VARIABLE = "TRANIBOR_DEMO_HISTORY_IMPORT"


class DemoHistoryImportRefusedError(RuntimeError):
    """The API was asked to take in a demonstration history where it must not."""


def demo_history_import_refusal(*, environment: str | None, enabled: str | bool | None) -> str | None:
    """Why this instance takes no demonstration history, or `None` when it does (ADR 0216).

    The import writes runs, costs and decisions that nothing measured, so it is an explicit opt-in
    (`TRANIBOR_DEMO_HISTORY_IMPORT`, never implied by the fakes or by a role) and it is never on in
    production, whatever the variable says: `staging` and `prod` refuse it, as they refuse the fakes.
    A deployed demonstration instance runs with `TRANIBOR_ENV=dev` (its default) or any name that is not
    production.
    """
    env = (environment or "").strip().lower()
    if is_production(env):
        return (
            f"a demonstration history cannot be imported where {ENVIRONMENT_VARIABLE}={env}: it writes "
            "runs and costs that nothing measured, so staging and prod refuse it. Use a demonstration "
            f"instance ({ENVIRONMENT_VARIABLE}=dev)."
        )
    if not fakes_on(enabled):
        return (
            "this instance does not import a demonstration history: it is an explicit opt-in. Set "
            f"{DEMO_HISTORY_IMPORT_VARIABLE}=1 on a demonstration instance (ADR 0216)."
        )
    return None


def refuse_demo_history_import(*, environment: str | None, enabled: str | bool | None) -> None:
    """Stop a process that has `TRANIBOR_DEMO_HISTORY_IMPORT` on in production, naming the setting."""
    if fakes_on(enabled) and is_production(environment):
        raise DemoHistoryImportRefusedError(
            f"{DEMO_HISTORY_IMPORT_VARIABLE} is on and {ENVIRONMENT_VARIABLE}={(environment or '').strip()}: "
            "the API refuses to start. A demonstration history (ADR 0216) writes runs and costs that "
            f"nothing measured and is for a demonstration instance only: unset "
            f"{DEMO_HISTORY_IMPORT_VARIABLE}, or run with {ENVIRONMENT_VARIABLE}=dev."
        )


#: The variable that names the file of plugin servers the API runs in its own process (ADR 0218).
DEMO_PLUGIN_SERVERS_VARIABLE = "TRANIBOR_DEMO_PLUGIN_SERVERS"


class DemoPluginServersRefusedError(RuntimeError):
    """In-process plugin servers were asked for where they must not run."""


def refuse_demo_plugin_servers(*, environment: str | None, fakes: str | bool | None) -> None:
    """Stop a process when `TRANIBOR_DEMO_PLUGIN_SERVERS` is set outside development under the fakes.

    A plugin server run in-process (ADR 0218) is the plugin's own code, on its own fake instance, inside
    the API's process: no container, no signature check on an image, no network listener. It needs the
    fakes (so that nothing real is reached) and a `dev` or `test` environment; anything else is refused,
    naming the setting to change.
    """
    env = (environment or "").strip().lower()
    if env not in DEMO_INLINE_WORKER_ENVIRONMENTS:
        raise DemoPluginServersRefusedError(
            f"{DEMO_PLUGIN_SERVERS_VARIABLE} is set and {ENVIRONMENT_VARIABLE}={env or '(unset)'}: refused. "
            "A plugin server run in-process (ADR 0218) skips the image checks of a deployment and is for a "
            f"development machine only: unset {DEMO_PLUGIN_SERVERS_VARIABLE}, or run with "
            f"{ENVIRONMENT_VARIABLE}=dev."
        )
    if not fakes_on(fakes):
        raise DemoPluginServersRefusedError(
            f"{DEMO_PLUGIN_SERVERS_VARIABLE} is set and {FAKES_VARIABLE} is off: refused. A plugin server "
            f"in-process runs on its fake instance (ADR 0005, ADR 0218); set {FAKES_VARIABLE}=1, or unset "
            f"{DEMO_PLUGIN_SERVERS_VARIABLE} and run the server on a cluster."
        )
