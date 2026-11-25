# SPDX-License-Identifier: Apache-2.0
"""The executors' side of ADR 0146 §9 and ADR 0222: the refusal at `start`, and what each kind declares.

`refuse_unless_separate` is the first thing every executor's `start` does: under `container`, an
executor that does not declare `separate_agent` raises before it writes a Secret, a claim, a Job or a
container. `declared_capabilities` reads what an executor of a kind declares in this deployment from its
class, without building it: the API judges `/readiness` with it, and it holds no cluster credential.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import structlog
from tranibor_core.agent_isolation import (
    AGENT_ISOLATION_ACCEPT_ENV,
    NONE_ACCEPTED,
    agent_isolation_refusal,
    unisolated_agent_refusal,
)
from tranibor_core.domain import StageJobSpec
from tranibor_core.production import is_production

from ..errors import AgentIsolationRefusedError

log = structlog.get_logger(__name__)


def refuse_unless_separate(
    executor: str, capabilities: Iterable[str], spec: StageJobSpec, *, runs_an_agent: bool = True
) -> None:
    """Raises `AgentIsolationRefusedError` when the run asks for the agent's own container and the executor
    cannot start one, or (ADR 0222) when, in staging or prod, the agent would share the runner's container
    and the operator did not name the opt-out. An accepted opt-out is logged, once per run. `runs_an_agent`
    is False for the fake, which starts no runner: no agent shares its container with anything."""
    refusal = agent_isolation_refusal(spec.agent_isolation, executor, capabilities)
    if refusal is None and runs_an_agent:
        refusal = unisolated_agent_refusal(
            spec.environment, spec.agent_isolation, executor, accepted=spec.agent_isolation_accepted
        )
    if refusal is not None:
        raise AgentIsolationRefusedError(refusal)
    if (
        runs_an_agent
        and spec.agent_isolation == "process"
        and spec.agent_isolation_accepted
        and is_production(spec.environment)
    ):
        log.warning(
            "agent_isolation.none_accepted",
            run_id=spec.run_id,
            project=spec.project_slug,
            executor=executor,
            environment=spec.environment,
            setting=f"{AGENT_ISOLATION_ACCEPT_ENV}={NONE_ACCEPTED}",
            detail="the agent runs in the runner's container and can read its credentials (ADR 0222)",
        )


def declared_capabilities(kind: str, *, agent_isolation: str) -> frozenset[str]:
    """What an executor of `kind` declares in a deployment whose setting is `agent_isolation`, read from
    its class. The warm pool's depends on its template, which the chart renders from the same setting.
    An unknown kind declares nothing."""
    from .aca import AcaExecutor
    from .agent_sandbox import AgentSandboxExecutor
    from .k8s_job import KubernetesJobExecutor
    from .local_docker import LocalDockerExecutor
    from .tekton import TektonExecutor

    if kind == "agent_sandbox":
        return AgentSandboxExecutor.pool_capabilities(agent_isolation)
    classes: dict[str, Any] = {
        "k8s_job": KubernetesJobExecutor,
        "local_docker": LocalDockerExecutor,
        "tekton": TektonExecutor,
        "aca": AcaExecutor,
    }
    found = classes.get(kind)
    return frozenset(found.capabilities) if found is not None else frozenset()
