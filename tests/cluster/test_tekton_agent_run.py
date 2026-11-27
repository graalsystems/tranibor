# SPDX-License-Identifier: Apache-2.0
"""S87-06: one agent run through the runner's own command, in a Tekton agent stage on a cluster.

`test_tekton_isolation.py` replaces the runner's step by a probe. Here the step runs the runner's CLI
itself (`tranibor-runner run`, the step's own arguments, its own environment: the run's token from the
run's Secret, `agent_isolation=container`, the launcher's port, the Task's workspace), with one test
double, the platform's internal API in memory (`assets/tekton_agent_run.py`). The agent is the runner
tests' scripted ACP agent, which the runner has the launcher of the `agent` sidecar start, as it would a
harness. What only a cluster with Tekton says:

- the run journals `agent.isolation` with `mode: container` and the launcher on the pod's loopback;
- the agent ran in the sidecar: the file it wrote in the Task's workspace belongs to the sidecar's uid
  (1001), not the runner's (1000);
- the run ends `done`, the runner writes the Task's results, and the PipelineRun ends `Succeeded` when the
  step ends (Tekton stops the sidecar).

With `TRANIBOR_TEKTON_RUNTIME_CLASS=gvisor` the pod runs under that RuntimeClass, and the test says so.
Run where `test_tekton_isolation.py` runs (the CI's `hosted-cluster` job runs both under gVisor):

    TRANIBOR_CLUSTER_CONTEXT=kind-<cluster> TRANIBOR_TEKTON_RUNNER_IMAGE=<the runner image, loaded> \\
        uv run pytest tests/cluster/test_tekton_agent_run.py -m cluster -s
"""

from __future__ import annotations

import json
import pathlib
import secrets
import time
from types import SimpleNamespace
from typing import Any

import pytest

from .conftest import kubectl_shared
from .test_tekton_isolation import IMAGE, PYTHON, RUNTIME_CLASS, _place_on_runner_nodes, _tekton_installed

pytestmark = pytest.mark.cluster

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = pathlib.Path(__file__).parent / "assets" / "tekton_agent_run.py"
FAKE_AGENT = ROOT / "packages" / "runner" / "tests" / "fixtures" / "fake_agent.py"


def _pipeline_run(namespace: str, run_id: str) -> dict[str, Any]:
    from tranibor_adapters.executor.tekton import AGENT_STAGE, TektonExecutor
    from tranibor_core.domain import StageJobSpec

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
    run = TektonExecutor(SimpleNamespace(), storage="1Gi")._pipeline_run(spec, f"run-{run_id}")  # type: ignore[arg-type]
    _place_on_runner_nodes(run["spec"]["taskRunTemplate"]["podTemplate"])
    [stage] = [t for t in run["spec"]["pipelineSpec"]["tasks"] if t["name"] == AGENT_STAGE]
    step = next(s for s in stage["taskSpec"]["steps"] if s["name"] == "run")
    # The step's own arguments (`run --write-result-url … --write-status …`) go to the runner's CLI; only the
    # program that receives them changes, to put the internal API in memory before the CLI starts.
    assert step["command"] == ["tranibor-runner"] and step["args"][:1] == ["run"], step
    step["command"] = ["/bin/sh", "-c", f'exec {PYTHON} -c "$S87_AGENT_RUN" "$@"', "tranibor-runner"]
    step["env"] = [
        *step["env"],
        {"name": "S87_AGENT_RUN", "value": SCRIPT.read_text(encoding="utf-8")},
        {"name": "S87_FAKE_AGENT", "value": FAKE_AGENT.read_text(encoding="utf-8")},
    ]
    return run


def _lines(logs: str, kind: str) -> list[Any]:
    prefix = f"S87-06 {kind} "
    return [json.loads(line.removeprefix(prefix)) for line in logs.splitlines() if line.startswith(prefix)]


@pytest.fixture(scope="module")
def outcome() -> dict[str, Any]:
    _tekton_installed()
    namespace = f"s87-agent-run-{secrets.token_hex(3)}"
    run_id = secrets.token_hex(4)
    kubectl_shared("create", "namespace", namespace)
    try:
        kubectl_shared("create", "serviceaccount", "tranibor-runner", "-n", namespace)
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": f"run-{run_id}", "namespace": namespace},
            "stringData": {"token": f"s87-run-token-{secrets.token_hex(16)}"},
        }
        kubectl_shared("apply", "-f", "-", input_text=json.dumps(secret))
        kubectl_shared("apply", "-f", "-", input_text=json.dumps(_pipeline_run(namespace, run_id)))
        deadline, run = time.monotonic() + 600, dict[str, Any]()
        while time.monotonic() < deadline:
            raw = kubectl_shared(
                "-n", namespace, "get", "pipelinerun", f"run-{run_id}", "-o", "json", check=False
            )
            run = json.loads(raw) if raw.strip() else {}
            conditions = run.get("status", {}).get("conditions") or [{}]
            if conditions[0].get("status") in {"True", "False"}:
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
            pytest.fail(
                f"no agent-stage pod ran ({run.get('status')}); the namespace's events:\n{events[-2000:]}"
            )
        [pod] = stages
        logs = kubectl_shared("-n", namespace, "logs", pod["metadata"]["name"], "-c", "step-run", check=False)
        print(
            f"\nruntimeClass: {pod['spec'].get('runtimeClassName')}"
            f"\nPipelineRun status: {json.dumps(run.get('status', {}).get('conditions'))}"
            f"\nstep-run's log (tail):\n{logs[-4000:]}"
        )
        return {
            "condition": (run.get("status", {}).get("conditions") or [{}])[0],
            "results": {r["name"]: r["value"] for r in run.get("status", {}).get("results") or []},
            "events": _lines(logs, "event"),
            "result": (_lines(logs, "result") or [{}])[-1],
            "written": (_lines(logs, "written") or [{}])[-1],
            "pod": pod,
        }
    finally:
        kubectl_shared("delete", "namespace", namespace, "--wait=false", "--ignore-not-found", check=False)


@pytest.mark.capability("tekton", "separate_agent")
def test_the_run_journals_that_its_agent_runs_in_a_container(outcome: dict[str, Any]) -> None:
    isolation = [e["payload"] for e in outcome["events"] if e["type"] == "agent.isolation"]
    assert isolation == [{"mode": "container", "launcher": "127.0.0.1:8766"}], outcome["events"][:5]


@pytest.mark.capability("tekton", "separate_agent")
def test_the_agent_ran_in_the_sidecar_and_wrote_in_the_tasks_workspace(outcome: dict[str, Any]) -> None:
    written = outcome["written"]
    assert written.get("runner_uid") == 1000, written
    assert written.get("uid") == 1001, f"the agent's file is not the sidecar's: {written}"


@pytest.mark.capability("tekton", "separate_agent")
def test_the_run_ends_done_and_the_pipeline_run_succeeds(outcome: dict[str, Any]) -> None:
    assert outcome["result"].get("status") == "done", outcome["result"]
    assert outcome["condition"].get("status") == "True", outcome["condition"]
    assert outcome["results"].get("status") == "done", outcome["results"]


def test_the_pod_runs_under_the_runtime_class_asked(outcome: dict[str, Any]) -> None:
    assert outcome["pod"]["spec"].get("runtimeClassName") == (RUNTIME_CLASS or None)
