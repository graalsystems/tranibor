# SPDX-License-Identifier: Apache-2.0
"""S87-06: Tekton's agent stage carries the agent's own container, on a cluster with Tekton Pipelines.

S87-05 built it and proved its shape in unit tests: the PipelineRun carries its Task inline, and the
agent stage's Task an `agent` sidecar (the launcher, uid 1001, read-only root, no secret) beside the
runner's step. What only Tekton's controller can say, run here:

- that it starts the sidecar and the runner's step reaches its launcher on the pod's loopback (Tekton
  waits for sidecars to be ready by default, `await-sidecar-readiness`; with `enable-kubernetes-sidecar`
  the sidecar is a native one);
- that the agent's side, started in the sidecar, runs in the Task's workspace (Tekton mounts it in
  sidecars at `$(workspaces.source.path)`) and shares the exchange directory both ways;
- that it finds the run token, which the runner's step holds from the run's Secret, in no `/proc` entry
  it can read;
- that the PipelineRun ends when the step ends: Tekton stops the sidecar;
- that the agent stage's pod carries `k8s_job`'s run-pod labels (Tekton gives it the embedded Task's
  `metadata.labels`), so a run-pod NetworkPolicy selects it, and that the egress gate (S80-07f) is its first
  step and ends before the runner's step starts (#2278). The gate runs on a cluster whose CNI enforces
  NetworkPolicies only, said by `TRANIBOR_TEKTON_EGRESS_GATE=1` (kind's default `kindnet` does not: the
  gate would exit 115 at its deadline). The namespace then gets a NetworkPolicy refusing every egress of the
  pods the run-pod label selects, and the gate passes only if that policy holds the pod.

The PipelineRun is the executor's own (`TektonExecutor._pipeline_run`); only the runner step's command is
replaced by the probe's runner side (`tests/live/aca_two_containers.py`, the same probe as on Container
Apps), and the node selector the runner nodes carry in production is dropped. Run on a cluster with
Tekton Pipelines and the runner image:

    TRANIBOR_CLUSTER_CONTEXT=kind-s87-tekton TRANIBOR_CLUSTER_ALLOW_SHARED=kind-s87-tekton \\
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
from tranibor_core.egress_gate import GATE_CONTAINER
from tranibor_core.publishing import PUBLISHING

from .conftest import kubectl_shared

pytestmark = pytest.mark.cluster

ROOT = pathlib.Path(__file__).resolve().parents[2]
IMAGE = os.environ.get("TRANIBOR_TEKTON_RUNNER_IMAGE", PUBLISHING.image_name("runner") + ":0.29.4")
PYTHON = "/app/.venv/bin/python"
#: The egress gate in the agent stage (#2278): on a cluster whose CNI enforces NetworkPolicies only.
EGRESS_GATE = os.environ.get("TRANIBOR_TEKTON_EGRESS_GATE", "") == "1"
#: The RuntimeClass of the run's pod (`gvisor` for the second run of S87-06); empty: the cluster's default.
RUNTIME_CLASS = os.environ.get("TRANIBOR_TEKTON_RUNTIME_CLASS", "")


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
    from tranibor_core.egress_gate import EgressGate

    probe = _probe()
    spec = StageJobSpec(
        run_id=run_id,
        project_slug="demo",
        namespace=namespace,
        runner_image=IMAGE,
        api_url="http://tranibor-api:8000",
        run_token="unused: read from the run's Secret",
        agent_isolation="container",
        timeout_minutes=10,
        runtime_class=RUNTIME_CLASS or None,
    )
    gate = EgressGate(deadline_s=60) if EGRESS_GATE else None
    executor = TektonExecutor(SimpleNamespace(), storage="1Gi", egress_gate=gate)  # type: ignore[arg-type]
    run = executor._pipeline_run(spec, f"run-{run_id}")
    template = run["spec"]["taskRunTemplate"]["podTemplate"]
    template.pop("nodeSelector", None)
    template.pop("tolerations", None)
    [stage] = [t for t in run["spec"]["pipelineSpec"]["tasks"] if t["name"] == "agent-stage"]
    *gated, step = stage["taskSpec"]["steps"]
    assert [s["name"] for s in gated] == ([GATE_CONTAINER] if EGRESS_GATE else []), "the gate goes first"
    # The runner writes the Task's two results (`--write-result-url`, `--write-status`); the probe in its
    # place does too, or the PipelineRun fails on results it declares and nobody wrote.
    step["command"] = [
        "/bin/sh",
        "-c",
        f'{PYTHON} -c "$S87_RUNNER_SIDE"; code=$?; : > $(results.result-url.path); '
        "echo probed > $(results.status.path); exit $code",
    ]
    step.pop("args", None)
    checks = json.dumps({str(code): verdict for code, verdict in probe.CHECKS.items()})
    token = next(e for e in step["env"] if e["name"] == "TRANIBOR_RUN_TOKEN")
    step["env"] = [
        *step["env"],
        {"name": "S87_MARKER", "valueFrom": token["valueFrom"]},
        {"name": "S87_RUNNER_SIDE", "value": probe.RUNNER_SIDE},
        {"name": "S87_AGENT_SIDE", "value": probe.AGENT_SIDE},
        {"name": "S87_CHECKS", "value": checks},
        {"name": "S87_AGENT_CWD", "value": "$(workspaces.source.path)"},
    ]
    return run


def _run_pod_deny_all(namespace: str) -> dict[str, Any]:
    """A run-pod policy of the platform's kind (#2278): every egress refused to the pods it selects by the
    run-pod label, as `docs/deployment.md` tells a platform to select them. The probe needs none: the
    runner's step reaches the agent's launcher on the pod's loopback."""
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "s87-run-pod-deny-egress", "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": {"tranibor/sandbox": "true"}},
            "policyTypes": ["Egress"],
            "egress": [],
        },
    }


@pytest.fixture(scope="module")
def outcome() -> dict[str, Any]:
    _tekton_installed()
    namespace = f"s87-tekton-{secrets.token_hex(3)}"
    run_id = secrets.token_hex(4)
    token = f"s87-run-token-{secrets.token_hex(16)}"
    kubectl_shared("create", "namespace", namespace)
    try:
        kubectl_shared("create", "serviceaccount", "tranibor-runner", "-n", namespace)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"run-{run_id}", "namespace": namespace},
            "stringData": {"token": token},
        }
        kubectl_shared("apply", "-f", "-", input_text=json.dumps(secret))
        if EGRESS_GATE:
            kubectl_shared("apply", "-f", "-", input_text=json.dumps(_run_pod_deny_all(namespace)))
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
        stages = [p for p in pods if p["metadata"]["labels"].get("tekton.dev/pipelineTask") == "agent-stage"]
        if not stages:
            events = kubectl_shared("-n", namespace, "get", "events", "--sort-by=.lastTimestamp", check=False)
            pytest.fail(f"no agent-stage pod ran ({condition}); the namespace's events:\n{events[-2000:]}")
        [pod] = stages
        logs = kubectl_shared("-n", namespace, "logs", pod["metadata"]["name"], "-c", "step-run", check=False)
        lines = [line for line in logs.splitlines() if line.startswith("S87-07 ")]
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
        print(
            f"\nTekton feature flags: {flags}\nruntimeClass: {pod['spec'].get('runtimeClassName')}"
            f"\ncondition: {condition}\nrunner's report: {lines}"
        )
        # The steps' own start and end, as Tekton's entrypoint records them: every step's container starts
        # with the pod and waits for the step before it, so the pod's container statuses do not say when a
        # step began.
        task_runs = json.loads(
            kubectl_shared(
                "-n",
                namespace,
                "get",
                "taskruns",
                "-l",
                f"tekton.dev/pipelineRun=run-{run_id},tekton.dev/pipelineTask=agent-stage",
                "-o",
                "json",
            )
        )["items"]
        return {
            "condition": condition,
            "steps": {s["name"]: s for t in task_runs for s in t.get("status", {}).get("steps") or []},
            "report": json.loads(lines[-1].removeprefix("S87-07 ")) if lines else {},
            "pod": pod,
        }
    finally:
        kubectl_shared("delete", "namespace", namespace, "--wait=false", "--ignore-not-found", check=False)


@pytest.mark.capability("tekton", "separate_agent")
def test_the_pipeline_run_ends_when_the_runners_step_ends(outcome: dict[str, Any]) -> None:
    assert outcome["condition"].get("status") == "True", outcome["condition"]


def test_the_run_pod_runs_under_the_runtime_class_asked(outcome: dict[str, Any]) -> None:
    """`gvisor` in the CI's `hosted-cluster` job: the executor names it on the PipelineRun's pod template."""
    assert outcome["pod"]["spec"].get("runtimeClassName") == (RUNTIME_CLASS or None)


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


@pytest.mark.capability("tekton", "separate_agent")
def test_the_agent_stages_pod_carries_the_run_pod_labels(outcome: dict[str, Any]) -> None:
    """#2278: the labels the run-pod NetworkPolicies select, as on `k8s_job`'s run pods."""
    from tranibor_adapters.executor.k8s_job import RUNNER_POD_LABELS

    labels = outcome["pod"]["metadata"]["labels"]
    assert {key: labels.get(key) for key in RUNNER_POD_LABELS} == RUNNER_POD_LABELS


@pytest.mark.capability("tekton", "separate_agent")
def test_the_egress_gate_ends_before_the_runners_step_starts(outcome: dict[str, Any]) -> None:
    """#2278: the gate saw the run-pod policy refuse a direct connection out, then the runner's step began."""
    if not EGRESS_GATE:
        pytest.skip("TRANIBOR_TEKTON_EGRESS_GATE=1 names a cluster whose CNI enforces NetworkPolicies")
    steps = outcome["steps"]
    assert list(steps)[:2] == [GATE_CONTAINER, "run"], list(steps)
    gate = steps[GATE_CONTAINER].get("terminated") or {}
    runner = steps["run"].get("terminated") or {}
    assert gate.get("exitCode") == 0, gate
    assert gate["finishedAt"] <= runner["startedAt"], (gate, runner)
