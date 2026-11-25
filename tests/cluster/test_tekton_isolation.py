# SPDX-License-Identifier: Apache-2.0
"""S86-06: Tekton's agent stage carries the agent's own container, on a cluster with Tekton Pipelines.

S86-05 built it and proved its shape in unit tests: the PipelineRun carries its Task inline, and the
agent stage's Task an `agent` sidecar (the launcher, uid 1001, read-only root, no secret) beside the
runner's step. What only Tekton's controller can say, run here:

- that it starts the sidecar and the runner's step reaches its launcher on the pod's loopback (Tekton
  waits for sidecars to be ready by default, `await-sidecar-readiness`; with `enable-kubernetes-sidecar`
  the sidecar is a native one);
- that the agent's side, started in the sidecar, runs in the Task's workspace (Tekton mounts it in
  sidecars at `$(workspaces.source.path)`) and shares the exchange directory both ways;
- that it finds the run token, which the runner's step holds from the run's Secret, in no `/proc` entry
  it can read;
- that the PipelineRun ends when the step ends: Tekton stops the sidecar.

The PipelineRun is the executor's own (`TektonExecutor._pipeline_run`); only the runner step's command is
replaced by the probe's runner side (`tests/live/aca_two_containers.py`, the same probe as on Container
Apps), and the node selector the runner nodes carry in production is dropped. Run on a cluster with
Tekton Pipelines and the runner image:

    TRANIBOR_CLUSTER_CONTEXT=kind-s86-tekton TRANIBOR_CLUSTER_ALLOW_SHARED=kind-s86-tekton \\
        uv run pytest tests/cluster/test_tekton_isolation.py -m cluster -s
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import secrets
import time
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from .conftest import kubectl_shared

pytestmark = pytest.mark.cluster

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("TRANIBOR_TEKTON_RUNNER_IMAGE", "ghcr.io/graalsystems/tranibor-runner:0.29.4")
PYTHON = "/app/.venv/bin/python"


def _probe() -> ModuleType:
    path = ROOT / "tests" / "live" / "aca_two_containers.py"
    spec = importlib.util.spec_from_file_location("two_containers_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tekton_installed() -> None:
    found = kubectl_shared("get", "crd", "pipelineruns.tekton.dev", "-o", "name", check=False)
    if "pipelineruns.tekton.dev" not in found:
        pytest.skip("Tekton Pipelines is not installed on this cluster")


def _pipeline_run(namespace: str, run_id: str) -> dict[str, Any]:
    from tranibor_adapters.executor.tekton import TektonExecutor
    from tranibor_core.domain import StageJobSpec

    probe = _probe()
    spec = StageJobSpec(
        run_id=run_id,
        project_slug="demo",
        namespace=namespace,
        runner_image=IMAGE,
        api_url="http://choregos-api:8000",
        run_token="unused: read from the run's Secret",
        agent_isolation="container",
        timeout_minutes=10,
    )
    run = TektonExecutor(SimpleNamespace(), storage="1Gi")._pipeline_run(spec, f"run-{run_id}")  # type: ignore[arg-type]
    template = run["spec"]["taskRunTemplate"]["podTemplate"]
    template.pop("nodeSelector", None)
    template.pop("tolerations", None)
    [stage] = [t for t in run["spec"]["pipelineSpec"]["tasks"] if t["name"] == "agent-stage"]
    [step] = stage["taskSpec"]["steps"]
    # The runner writes the Task's two results (`--write-result-url`, `--write-status`); the probe in its
    # place does too, or the PipelineRun fails on results it declares and nobody wrote.
    step["command"] = [
        "/bin/sh",
        "-c",
        f'{PYTHON} -c "$S86_RUNNER_SIDE"; code=$?; : > $(results.result-url.path); '
        "echo probed > $(results.status.path); exit $code",
    ]
    step.pop("args", None)
    checks = json.dumps({str(code): verdict for code, verdict in probe.CHECKS.items()})
    token = next(e for e in step["env"] if e["name"] == "TRANIBOR_RUN_TOKEN")
    step["env"] = [
        *step["env"],
        {"name": "S86_MARKER", "valueFrom": token["valueFrom"]},
        {"name": "S86_RUNNER_SIDE", "value": probe.RUNNER_SIDE},
        {"name": "S86_AGENT_SIDE", "value": probe.AGENT_SIDE},
        {"name": "S86_CHECKS", "value": checks},
        {"name": "S86_AGENT_CWD", "value": "$(workspaces.source.path)"},
    ]
    return run


@pytest.fixture(scope="module")
def outcome() -> dict[str, Any]:
    _tekton_installed()
    namespace = f"s86-tekton-{secrets.token_hex(3)}"
    run_id = secrets.token_hex(4)
    token = f"s86-run-token-{secrets.token_hex(16)}"
    kubectl_shared("create", "namespace", namespace)
    try:
        kubectl_shared("create", "serviceaccount", "choregos-runner", "-n", namespace)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"run-{run_id}", "namespace": namespace},
            "stringData": {"token": token},
        }
        kubectl_shared("apply", "-f", "-", input_text=json.dumps(secret))
        kubectl_shared("apply", "-f", "-", input_text=json.dumps(_pipeline_run(namespace, run_id)))
        deadline, condition = time.monotonic() + 600, dict[str, Any]()
        while time.monotonic() < deadline:
            raw = kubectl_shared(
                "-n", namespace, "get", "pipelinerun", f"run-{run_id}", "-o", "json", check=False
            )
            conditions = (json.loads(raw) if raw.strip() else {}).get("status", {}).get("conditions") or []
            condition = conditions[0] if conditions else {}
            if condition.get("status") in {"True", "False"}:
                break
            time.sleep(3)
        pods = json.loads(
            kubectl_shared(
                "-n", namespace, "get", "pods", "-l", f"tekton.dev/pipelineRun=run-{run_id}", "-o", "json"
            )
        )["items"]
        [pod] = [p for p in pods if p["metadata"]["labels"].get("tekton.dev/pipelineTask") == "agent-stage"]
        logs = kubectl_shared("-n", namespace, "logs", pod["metadata"]["name"], "-c", "step-run", check=False)
        lines = [line for line in logs.splitlines() if line.startswith("S86-07 ")]
        flags = kubectl_shared(
            "-n",
            "tekton-pipelines",
            "get",
            "configmap",
            "feature-flags",
            "-o",
            "jsonpath={.data}",
            check=False,
        )
        print(f"\nTekton feature flags: {flags}\ncondition: {condition}\nrunner's report: {lines}")
        return {
            "condition": condition,
            "report": json.loads(lines[-1].removeprefix("S86-07 ")) if lines else {},
            "pod": pod,
        }
    finally:
        kubectl_shared("delete", "namespace", namespace, "--wait=false", "--ignore-not-found", check=False)


@pytest.mark.capability("tekton", "separate_agent")
def test_the_pipeline_run_ends_when_the_runners_step_ends(outcome: dict[str, Any]) -> None:
    assert outcome["condition"].get("status") == "True", outcome["condition"]


@pytest.mark.capability("tekton", "separate_agent")
def test_the_agent_runs_in_its_own_sidecar_as_another_user(outcome: dict[str, Any]) -> None:
    report = outcome["report"]
    assert report.get("exit") == 0, report
    assert report["runner_uid"] == 1000
    assert report["agent"]["uid"] == 1001, "the sidecar's own uid, not the runner's"
    sidecars = [c["name"] for c in outcome["pod"]["spec"]["containers"] if c["name"].startswith("sidecar-")]
    sidecars += [
        c["name"]
        for c in outcome["pod"]["spec"].get("initContainers", [])
        if c["name"].startswith("sidecar-")
    ]
    assert sidecars == ["sidecar-agent"], sidecars


@pytest.mark.capability("tekton", "separate_agent")
def test_the_agent_finds_the_run_token_in_no_process_it_can_read(outcome: dict[str, Any]) -> None:
    report = outcome["report"]
    assert report["agent"]["marker_found_in"] == [], report


@pytest.mark.capability("tekton", "separate_agent")
def test_the_workspace_and_the_exchange_are_shared_both_ways(outcome: dict[str, Any]) -> None:
    report = outcome["report"]
    assert report["agent"]["read_from_runner"] == "written by the runner's container", report
    assert report["agent_exit"] == 0, "the agent's side ran in the Task's workspace"
