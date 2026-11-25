# SPDX-License-Identifier: Apache-2.0
"""S87-07: what a Container Apps job does with two containers, on a real subscription.

ADR 0226 §2 lets `aca` start the agent's own container only if a two-container job gives the shape
the other executors give (ADR 0146): the runner and the agent's launcher share loopback and an
`EmptyDir`, the agent's container cannot read the runner's secrets, and the execution ends when the
runner ends. Microsoft's documentation allows several containers in a job and shares an `EmptyDir`
between the containers of one replica; it says nothing of loopback, of the process namespace, of the
uid (no `securityContext`) or of when an execution with two containers ends. These tests run it.

Both containers run the public runner image: the runner's side is `aca_two_containers.RUNNER_SIDE`
(the runner image's Python and its launcher client), the agent's side is `choregos-runner launcher`,
the program an isolated agent's container runs everywhere else. The runner holds a marker as a job
secret, as it holds the run token.

Prerequisites: those of `test_aca_live.py` (`docs/runbooks/aca-live.md`). Each test prints one line
`S87-07 …` with what it observed; the runner's own report (`S87-07 {json}`) is in the job's console
log (Log Analytics, `ContainerAppConsoleLogs_CL`).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from tranibor_adapters.executor.aca import AzureArmClient
from tranibor_core.publishing import PUBLISHING

from . import aca_two_containers as probe
from .conftest import require

pytestmark = pytest.mark.live

IMAGE = PUBLISHING.image_name("runner") + ":0.29.4"
LAUNCHER = "/app/.venv/bin/choregos-runner"
PYTHON = "/app/.venv/bin/python"
#: The agent's container when it ends itself once the runner says it is done (ADR 0226 §2, S87-08).
LAUNCHER_UNTIL_DONE = (
    f"{LAUNCHER} launcher & pid=$!; until [ -f /exchange/.runner-done ]; do sleep 1; done; kill $pid; exit 0"
)


@pytest.fixture
async def arm() -> AsyncIterator[tuple[AzureArmClient, str]]:
    env = require(
        "TRANIBOR_LIVE_AZURE_SUBSCRIPTION",
        "TRANIBOR_LIVE_AZURE_RG",
        "TRANIBOR_LIVE_AZURE_ENV_ID",
        "TRANIBOR_LIVE_AZURE_TOKEN",
    )
    async with httpx.AsyncClient(timeout=60.0) as client:
        yield (
            AzureArmClient(
                subscription_id=env["TRANIBOR_LIVE_AZURE_SUBSCRIPTION"],
                resource_group=env["TRANIBOR_LIVE_AZURE_RG"],
                token=env["TRANIBOR_LIVE_AZURE_TOKEN"],
                client=client,
            ),
            env["TRANIBOR_LIVE_AZURE_ENV_ID"],
        )


def definition(
    environment_id: str, *, agent_command: list[str], marker: str, timeout_s: int
) -> dict[str, Any]:
    exchange = [{"volumeName": "exchange", "mountPath": "/exchange"}]
    checks = json.dumps({str(code): verdict for code, verdict in probe.CHECKS.items()})
    return {
        "location": "westeurope",
        "tags": {"choregos-live": "s87-07"},
        "properties": {
            "environmentId": environment_id,
            "configuration": {
                "triggerType": "Manual",
                "replicaTimeout": timeout_s,
                "replicaRetryLimit": 0,
                "manualTriggerConfig": {"parallelism": 1, "replicaCompletionCount": 1},
                "secrets": [{"name": "marker", "value": marker}],
            },
            "template": {
                "containers": [
                    {
                        "name": "runner",
                        "image": IMAGE,
                        "command": [PYTHON, "-c", probe.RUNNER_SIDE],
                        "env": [
                            {"name": "S87_MARKER", "secretRef": "marker"},
                            {"name": "S87_AGENT_SIDE", "value": probe.AGENT_SIDE},
                            {"name": "S87_CHECKS", "value": checks},
                        ],
                        "resources": {"cpu": 0.5, "memory": "1Gi"},
                        "volumeMounts": exchange,
                    },
                    {
                        "name": "agent",
                        "image": IMAGE,
                        "command": agent_command,
                        "resources": {"cpu": 0.5, "memory": "1Gi"},
                        "volumeMounts": exchange,
                    },
                ],
                "volumes": [{"name": "exchange", "storageType": "EmptyDir"}],
            },
        },
    }


async def run_job(
    arm: AzureArmClient, environment_id: str, *, agent_command: list[str], timeout_s: int
) -> str:
    """Create, start and follow a two-container job to its terminal state; delete it after."""
    name = f"s87-{uuid.uuid4().hex[:10]}"
    path = f"{arm.scope}/providers/Microsoft.App/jobs/{name}"
    marker = f"s87-marker-{uuid.uuid4().hex}"
    await arm.request(
        "PUT",
        path,
        json=definition(environment_id, agent_command=agent_command, marker=marker, timeout_s=timeout_s),
    )
    try:
        await arm.request("POST", f"{path}/start")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s + 300
        status = "pending"
        while loop.time() < deadline:
            executions = (await arm.request("GET", f"{path}/executions") or {}).get("value", [])
            if executions:
                status = str((executions[0].get("properties") or {}).get("status", "")).lower()
                if status in {"succeeded", "failed", "stopped", "degraded"}:
                    break
            await asyncio.sleep(10)
        print(f"S87-07 job={name} agent={agent_command[-1][:40]!r} status={status}")
        return status
    finally:
        await arm.request("DELETE", path)


async def test_the_shape_holds_when_the_agent_ends_with_the_runner(arm: tuple[AzureArmClient, str]) -> None:
    """Loopback, the `EmptyDir` both ways and the marker out of the agent's reach: the runner's side
    exits 0 only if all hold, and the agent's container ends itself once the runner is done, so the
    execution is `Succeeded` exactly when the shape holds."""
    client, environment_id = arm
    status = await run_job(
        client, environment_id, agent_command=["/bin/sh", "-c", LAUNCHER_UNTIL_DONE], timeout_s=600
    )
    assert status == "succeeded", (
        f"the two-container job ended {status}: read the runner's `S87-07` line in the console log; "
        f"its exit code is one of {probe.CHECKS}"
    )


async def test_what_the_execution_does_when_the_agent_outlives_the_runner(
    arm: tuple[AzureArmClient, str],
) -> None:
    """The launcher alone serves forever. Whether the execution ends when the runner's container ends
    is ACA's behaviour, measured here: a short replica timeout bounds the wait either way."""
    client, environment_id = arm
    status = await run_job(client, environment_id, agent_command=[LAUNCHER, "launcher"], timeout_s=240)
    assert status in {"succeeded", "failed"}, f"no terminal state observed: {status}"
