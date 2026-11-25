# SPDX-License-Identifier: Apache-2.0
"""Where the agent of a step runs (ADR 0146 §9): the deployment's setting, the executors' capability, and
the one comparison that refuses a run an executor cannot isolate.

`process`: the agent is a child of the runner, in the runner's container (the historical shape).
`container`: the agent runs in a container of its own in the run's pod, started there by the launcher,
and holds none of the run's credentials. One setting for the whole deployment,
`TRANIBOR_RUNNER_AGENT_ISOLATION` (the chart's `global.runner.agentIsolation`): the API reads it for
`/readiness`, the workers for every run they start, the warm pool's template for its pods.

An executor that can start the agent's container declares `separate_agent` among its capabilities. Under
`container`, an executor without it refuses the run at `start`, before anything is sent to its backend,
and `/readiness` says the same sentence before any run: both call `agent_isolation_refusal`. Never a
quieter mode: a run asked to isolate its agent is never run with the agent beside the runner (ADR 0101).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Literal

from .production import ENVIRONMENT_VARIABLE, is_production

AgentIsolation = Literal["process", "container"]

#: The deployment's setting, read by the API, the workers and the runner alike.
AGENT_ISOLATION_ENV = "TRANIBOR_RUNNER_AGENT_ISOLATION"
#: The values it takes; the first is the default.
AGENT_ISOLATION_MODES: tuple[AgentIsolation, ...] = ("process", "container")
#: The capability of an executor that starts the agent in a container of its own (ADR 0146 §9).
SEPARATE_AGENT = "separate_agent"
#: The error code of a run refused for it, as `hosted_unsupported_cluster` names its own refusal.
AGENT_ISOLATION_UNAVAILABLE = "agent_isolation_unavailable"


#: The named, explicit opt-out (ADR 0222): in staging and prod an agent that runs beside the runner's
#: credentials (`process`) is refused unless this variable holds `none-accepted`. The only value; a typo
#: is refused at start, never read as an acceptance.
AGENT_ISOLATION_ACCEPT_ENV = "TRANIBOR_AGENT_ISOLATION"
NONE_ACCEPTED = "none-accepted"


def agent_isolation_none_accepted(value: str | None) -> bool:
    """Whether a `TRANIBOR_AGENT_ISOLATION` value is the opt-out. Empty is not; any other word is refused."""
    text = (value or "").strip()
    if not text:
        return False
    if text == NONE_ACCEPTED:
        return True
    raise ValueError(
        f"{AGENT_ISOLATION_ACCEPT_ENV}={text!r}: the only value is {NONE_ACCEPTED!r} (ADR 0222); "
        "leave it unset to keep the agent isolated"
    )


def unisolated_agent_refusal(
    environment: str | None, asked: str, executor: str, *, accepted: bool
) -> str | None:
    """Why a run whose agent would share the runner's container is refused, or None.

    Only in staging and prod (`is_production`), only under `process`, and not when the operator named the
    opt-out (ADR 0222). An empty `environment` is not judged: a pod that starts no agent of its own
    (a hosted session) leaves it empty. One comparison for the executor, the runner and `/readiness`."""
    if accepted or asked != "process" or not is_production(environment):
        return None
    return (
        f"{AGENT_ISOLATION_UNAVAILABLE}: {ENVIRONMENT_VARIABLE}={(environment or '').strip()} and the agent "
        f"would run in the runner's own container ({AGENT_ISOLATION_ENV}=process, executor `{executor}`), "
        "where it can read the run token, the git token and the egress report key (ADR 0146). Isolate it "
        "(`global.runner.agentIsolation: container` on `k8s_job`, `local_docker`, `agent_sandbox` "
        "or `tekton`), or "
        f"accept the risk by name with {AGENT_ISOLATION_ACCEPT_ENV}={NONE_ACCEPTED} "
        "(the chart's `global.runner.acceptUnisolatedAgent: true`), which every run then logs (ADR 0222)"
    )


def deployment_agent_isolation(environ: Mapping[str, str]) -> AgentIsolation:
    """The deployment's setting; `process` when it says nothing. An unknown value is refused, never
    read as `process`: a typo would otherwise turn the isolation off."""
    value = (environ.get(AGENT_ISOLATION_ENV) or "").strip() or AGENT_ISOLATION_MODES[0]
    if value in ("process", "container"):
        return value  # type: ignore[return-value]  # narrowed by the test, which mypy does not see
    raise ValueError(
        f"{AGENT_ISOLATION_ENV}={value!r}: expected one of {', '.join(AGENT_ISOLATION_MODES)} (ADR 0146)"
    )


#: Why an executor that will not start the agent's container does not (S65-04l), added to its refusal
#: so that the operator reads the reason, not only the missing word.
WHY_NOT_SEPARATE: dict[str, str] = {
    "aca": (
        "a Container Apps job runs one container per execution here, and whether a job stops a second "
        "container when the runner's ends was never run"
    ),
}


def agent_isolation_refusal(asked: str, executor: str, capabilities: Iterable[str]) -> str | None:
    """Why `executor` cannot run a step under `asked`, or None when it can.

    ONE comparison for the executor (at `start`) and for the API (`/readiness`, before any run), so they
    cannot disagree, as `runtime_class_refusal` is for the warm pool's RuntimeClass."""
    if asked != "container" or SEPARATE_AGENT in frozenset(capabilities):
        return None
    why = WHY_NOT_SEPARATE.get(executor)
    return (
        f"{AGENT_ISOLATION_UNAVAILABLE}: the deployment runs every agent in a container of its own "
        f"({AGENT_ISOLATION_ENV}=container, ADR 0146) and the `{executor}` executor cannot start one "
        f"(it does not declare `{SEPARATE_AGENT}`{': ' + why if why else ''}); run the project on an "
        "executor that does (`k8s_job`, `local_docker`, `agent_sandbox`, `tekton`), or set "
        "`global.runner.agentIsolation: process` (in staging and prod that also needs "
        f"{AGENT_ISOLATION_ACCEPT_ENV}={NONE_ACCEPTED}, ADR 0222)"
    )
