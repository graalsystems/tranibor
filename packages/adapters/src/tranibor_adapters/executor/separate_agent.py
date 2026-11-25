# SPDX-License-Identifier: Apache-2.0
"""The agent's own container in a run's pod (ADR 0146 §1-§4, §12; S65-04h).

Under `container` the agent of an ACP step runs beside the runner, never as its child: a native sidecar
(an init container with `restartPolicy: Always`, Kubernetes 1.29 or later) that starts the launcher
(`choregos-runner launcher`) and nothing else. The runner connects to it over the pod's loopback and
asks it for the harness and the project's checks. The same shape as the warm pool's template
(`sandbox.yaml`), built here for `k8s_job` and, as a sidecar of the agent stage's Task, for `tekton`
(S86-05, `tekton_sidecar`):

- another uid (1001) in the runner's group (1000, the pod's `fsGroup`), a read-only root file system,
  every capability dropped; no Secret, no `envFrom`, no variable but `TMPDIR` and `HOME`;
- two volumes shared with the runner, and only two: the working tree (`/workspace`) and the exchange
  directory (`/exchange`, an `emptyDir`), where the runner writes the tool client and the agent its
  home and its scripts; the agent's `/tmp` is its own `emptyDir`;
- after the egress gate, which stays the pod's FIRST init container (S80-07f): init containers start in
  order, so nothing of the agent's runs until the gate has seen the pod's egress policy in force.

The runner's container keeps every credential and gets requests of its own (§12): it runs Python, git
and the loopback listeners, not the harness nor the test suites, which the agent's container now runs
with the requests and limits the single container had.
"""

from __future__ import annotations

from typing import Any

from tranibor_core.agent_isolation import AGENT_ISOLATION_ENV

#: The launcher's port on the pod's loopback (`launcher.DEFAULT_PORT`, the chart's `launcherPort`).
LAUNCHER_PORT = 8766
#: The agent's uid: not the runner's (1000), in the runner's group, which the pod's `fsGroup` gives both.
AGENT_UID = 1001
RUNNER_GID = 1000
#: The launcher's container. Beside a hosted agent (ADR 0080), whose container is `agent`, it is
#: `launcher`: there it starts the project's checks only.
AGENT_CONTAINER = "agent"
LAUNCHER_BESIDE_HOSTED = "launcher"
EXCHANGE = "/exchange"
EXCHANGE_VOLUME = "exchange"
AGENT_TMP_VOLUME = "agent-tmp"
#: The interpreter of the runner's image (its venv is not on `PATH`, #713): the startup probe's.
PYTHON = "/app/.venv/bin/python"

#: What the runner's own container requests and may use under `container` (ADR 0146 §12), measured on
#: 2026-11-19 against this repository (4,869 files, a 121 MiB pack): the runner's modules imported, 78 MiB
#: resident; `git fetch --depth 50`, the runner's fetch, 146 MiB at its peak; `checkout`, `add -A`, `diff`
#: and `commit` of 400 changed files under 70 MiB, each under a second of CPU. A full clone of the same
#: repository peaks at 700 MiB: the limit leaves room for a repository several times larger, and for the
#: compression proxy the runner starts when a policy asks for one (ADR 0055, not measured).
RUNNER_REQUESTS = {"cpu": "250m", "memory": "512Mi"}
RUNNER_LIMITS = {"cpu": "1", "memory": "2Gi"}


def runner_env() -> dict[str, str]:
    """What the runner's container is told: where the agent is, and that its temporary files (the agent's
    `$HOME`, the tool client) go to the surface both containers see."""
    return {
        AGENT_ISOLATION_ENV: "container",
        "TRANIBOR_RUNNER_LAUNCHER_PORT": str(LAUNCHER_PORT),
        "TMPDIR": EXCHANGE,
    }


def launcher_container(
    image: str, *, name: str, requests: dict[str, str], limits: dict[str, str]
) -> dict[str, Any]:
    """The agent's container: the launcher, as a native sidecar, holding nothing of the run's."""
    return {
        "name": name,
        "image": image,
        "restartPolicy": "Always",
        "args": ["launcher", "--port", str(LAUNCHER_PORT)],
        "env": [{"name": "TMPDIR", "value": EXCHANGE}, {"name": "HOME", "value": EXCHANGE}],
        # An exec probe: the kubelet's TCP probe connects from the node, where the pod's loopback is
        # not reachable. The runner starts once the launcher listens.
        "startupProbe": {
            "exec": {
                "command": [
                    PYTHON,
                    "-c",
                    f"import socket; socket.create_connection(('127.0.0.1', {LAUNCHER_PORT}), 1)",
                ]
            },
            "periodSeconds": 1,
            "failureThreshold": 60,
        },
        "resources": {"requests": dict(requests), "limits": dict(limits)},
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": AGENT_UID,
            "runAsGroup": RUNNER_GID,
            "allowPrivilegeEscalation": False,
            "readOnlyRootFilesystem": True,
            "capabilities": {"drop": ["ALL"]},
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumeMounts": [
            {"name": "workspace", "mountPath": "/workspace"},
            {"name": EXCHANGE_VOLUME, "mountPath": EXCHANGE},
            {"name": AGENT_TMP_VOLUME, "mountPath": "/tmp"},  # noqa: S108 - the agent's own emptyDir
        ],
    }


def add_to_pod(
    pod_spec: dict[str, Any],
    image: str,
    *,
    name: str,
    agent_requests: dict[str, str],
    agent_limits: dict[str, str],
    runner_requests: dict[str, str] | None = None,
    runner_limits: dict[str, str] | None = None,
) -> None:
    """Puts the agent's container among the pod's init containers (the egress gate, added after, goes
    before it), shares the exchange directory with the runner, tells the runner, and gives the runner's
    container its own requests and limits."""
    pod_spec.setdefault("initContainers", []).append(
        launcher_container(image, name=name, requests=agent_requests, limits=agent_limits)
    )
    pod_spec.setdefault("volumes", []).extend(_shared_volumes())
    runner = next(c for c in pod_spec["containers"] if c["name"] == "runner")
    _tell_runner(runner, "resources", runner_requests, runner_limits)
    # The cluster's services are not announced to the agent's container as variables.
    pod_spec["enableServiceLinks"] = False


def _shared_volumes() -> list[dict[str, Any]]:
    return [{"name": EXCHANGE_VOLUME, "emptyDir": {}}, {"name": AGENT_TMP_VOLUME, "emptyDir": {}}]


def _tell_runner(
    runner: dict[str, Any],
    resources: str,
    requests: dict[str, str] | None,
    limits: dict[str, str] | None,
) -> None:
    """Where the agent is (`runner_env`), the exchange directory mounted, and the runner's own requests and
    limits under `resources` (a container's `resources`, a Tekton step's `computeResources`)."""
    told = runner_env()
    runner["env"] = [e for e in runner.get("env", []) if e.get("name") not in told] + [
        {"name": key, "value": value} for key, value in told.items()
    ]
    runner.setdefault("volumeMounts", []).append({"name": EXCHANGE_VOLUME, "mountPath": EXCHANGE})
    runner[resources] = {
        "requests": dict(requests or RUNNER_REQUESTS),
        "limits": dict(limits or RUNNER_LIMITS),
    }


def tekton_sidecar(image: str, *, requests: dict[str, str], limits: dict[str, str]) -> dict[str, Any]:
    """The agent's container as a sidecar of a Tekton Task (S86-05): `launcher_container`, the same image,
    uid, read-only root, dropped capabilities, environment and probe, with three differences Tekton
    imposes and nothing else:

    - no `restartPolicy`: a Task's sidecar is a pod container, where Kubernetes refuses the field. With
      Tekton's `enable-kubernetes-sidecar` flag (Kubernetes 1.29+) Tekton turns it into a native sidecar
      itself; without it, Tekton's controller waits for it to be ready before the first step (its
      default, `await-sidecar-readiness`) and stops it after the last one;
    - `computeResources` for `resources` (Tekton v1's name);
    - no `/workspace` mount: Tekton mounts the Task's workspace in every step and sidecar at
      `$(workspaces.source.path)`, the path the runner is told, so both see the tree at the same place.
      The pod has no volume called `workspace`, and the workspace's own volume is named by Tekton."""
    container = launcher_container(image, name=AGENT_CONTAINER, requests=requests, limits=limits)
    del container["restartPolicy"]
    container["computeResources"] = container.pop("resources")
    container["volumeMounts"] = [m for m in container["volumeMounts"] if m["name"] != "workspace"]
    return container


def add_to_task(
    task_spec: dict[str, Any],
    image: str,
    *,
    agent_requests: dict[str, str],
    agent_limits: dict[str, str],
    runner_requests: dict[str, str] | None = None,
    runner_limits: dict[str, str] | None = None,
) -> None:
    """`add_to_pod` for a Tekton Task (S86-05): the agent's container among the Task's sidecars, the
    exchange directory and the agent's `/tmp` among its volumes, and the runner's step (`run`) told where
    the launcher is, with its own requests and limits."""
    task_spec.setdefault("sidecars", []).append(
        tekton_sidecar(image, requests=agent_requests, limits=agent_limits)
    )
    task_spec.setdefault("volumes", []).extend(_shared_volumes())
    runner = next(s for s in task_spec["steps"] if s["name"] == "run")
    _tell_runner(runner, "computeResources", runner_requests, runner_limits)
