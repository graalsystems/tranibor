"""S87-05: Tekton's PipelineRun carries its Pipeline and the agent stage's Task inline, and under `container`
the agent stage carries the agent's own container as a sidecar, the one `k8s_job` starts.

Not proved here: that Tekton's controller starts the sidecar before the runner's step, mounts the workspace
in it, and stops it after the last step, with or without `enable-kubernetes-sidecar` (S87-06, a cluster).
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from tranibor_adapters.errors import UpstreamError
from tranibor_adapters.executor import egress_gate, separate_agent
from tranibor_adapters.executor.k8s_job import RUNNER_POD_LABELS, KubernetesJobExecutor
from tranibor_adapters.executor.tekton import TEKTON_API, TektonExecutor
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_contracts import ExecutorKind
from tranibor_core.agent_isolation import AGENT_ISOLATION_ENV, SEPARATE_AGENT
from tranibor_core.domain import ExecRef, StageJobSpec
from tranibor_core.egress_gate import GATE_CONTAINER, REPORT_ENV, EgressGate

RUNNER = "registry.example/tranibor-runner:1.0"


def _spec(**changes: Any) -> StageJobSpec:
    base: dict[str, Any] = {
        "run_id": "r-1",
        "project_slug": "demo",
        "namespace": "proj-demo-runners",
        "runner_image": RUNNER,
        "api_url": "http://tranibor-api:8000",
        "run_token": "token",
        "gateway_key": "gk",
        "env": {"HTTP_PROXY": "http://egress:3128"},
        "runtime_class": "gvisor",
    }
    return StageJobSpec(**{**base, **changes})


def _run(**changes: Any) -> dict[str, Any]:
    return TektonExecutor(client=object())._pipeline_run(_spec(**changes), "run-r-1")  # type: ignore[arg-type]


def _stage(run: dict[str, Any]) -> dict[str, Any]:
    [stage] = [t for t in run["spec"]["pipelineSpec"]["tasks"] if t["name"] == "agent-stage"]
    task: dict[str, Any] = stage["taskSpec"]
    return task


def _step(task: dict[str, Any]) -> dict[str, Any]:
    [step] = task["steps"]
    assert step["name"] == "run"
    return step


def test_the_pipeline_and_the_agent_stage_are_inline_never_a_namespace_copy() -> None:
    run = _run()
    assert "pipelineRef" not in run["spec"], "no Pipeline of the namespace is run"
    tasks = run["spec"]["pipelineSpec"]["tasks"]
    assert [t["name"] for t in tasks] == ["agent-stage"], "no repository: no clone"
    assert "taskRef" not in tasks[0] and "taskSpec" in tasks[0], "no Task of the namespace is run"
    results = {r["name"]: r["value"] for r in run["spec"]["pipelineSpec"]["results"]}
    assert results == {
        "result-url": "$(tasks.agent-stage.results.result-url)",
        "status": "$(tasks.agent-stage.results.status)",
    }


def test_a_project_with_a_repository_clones_it_first_with_the_catalogue_task() -> None:
    executor = TektonExecutor(client=object())  # type: ignore[arg-type]
    spec = executor._pipeline_spec(_spec(), "run-r-1", cloned=True)
    clone, stage = spec["tasks"]
    assert clone["name"] == "clone" and clone["taskRef"] == {"name": "git-clone"}
    assert {p["name"]: p["value"] for p in clone["params"]}["url"] == "$(params.repo-url)"
    assert stage["runAfter"] == ["clone"] and "taskSpec" in stage


@pytest.mark.parametrize("isolation", ["process", "container"])
def test_the_run_pod_takes_the_deployments_runtime_class(isolation: str) -> None:
    """`global.runner.runtimeClass` (gVisor, Kata): every task's pod, the agent's sidecar included, runs
    under it (S87-06); without one the pod takes the cluster's default."""
    template = _run(agent_isolation=isolation)["spec"]["taskRunTemplate"]["podTemplate"]
    assert template["runtimeClassName"] == "gvisor"
    template = _run(agent_isolation=isolation, runtime_class=None)["spec"]["taskRunTemplate"]["podTemplate"]
    assert "runtimeClassName" not in template


def test_under_process_the_agent_stage_has_no_sidecar_and_the_runner_holds_the_run() -> None:
    task = _stage(_run())
    assert "sidecars" not in task and "volumes" not in task
    step = _step(task)
    assert step["image"] == RUNNER and step["command"] == ["tranibor-runner"]
    assert step["args"][:1] == ["run"] and "$(results.status.path)" in step["args"]
    env = {e["name"]: e for e in step["env"]}
    assert env["TRANIBOR_RUN_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "run-r-1", "key": "token"}
    assert env["TRANIBOR_WORKSPACE"]["value"] == "$(workspaces.source.path)"
    assert AGENT_ISOLATION_ENV not in env
    assert step["computeResources"]["limits"] == {"cpu": "2", "memory": "6Gi"}


def test_the_runner_step_gets_the_environment_k8s_jobs_runner_gets() -> None:
    """One builder for both (`runner_env_entries`): the Tekton step cannot drift from the Job's container."""
    spec = _spec()
    job = KubernetesJobExecutor(client=object())._job(spec, "run-r-1")  # type: ignore[arg-type]
    [runner] = job["spec"]["template"]["spec"]["containers"]
    step = _step(_stage(_run()))
    assert step["env"][:-1] == runner["env"]


def test_under_container_the_agent_stage_carries_the_agents_container() -> None:
    run = _run(agent_isolation="container")
    task = _stage(run)
    [agent] = task["sidecars"]
    expected = separate_agent.launcher_container(
        RUNNER,
        name="agent",
        requests={"cpu": "1", "memory": "2Gi"},
        limits={"cpu": "2", "memory": "6Gi"},
    )
    # The same container as `k8s_job`'s, but for what Tekton imposes.
    assert agent["image"] == RUNNER and agent["args"] == ["launcher", "--port", "8766"]
    assert agent["securityContext"] == expected["securityContext"]
    assert agent["securityContext"]["runAsUser"] == 1001
    assert agent["securityContext"]["readOnlyRootFilesystem"] is True
    assert agent["startupProbe"] == expected["startupProbe"]
    assert agent["env"] == [{"name": "TMPDIR", "value": "/exchange"}, {"name": "HOME", "value": "/exchange"}]
    assert agent["computeResources"] == expected["resources"]
    assert "restartPolicy" not in agent and "resources" not in agent
    text = yaml.safe_dump(agent)
    for forbidden in ("secretKeyRef", "secretRef", "envFrom", "TRANIBOR_RUN", "token"):
        assert forbidden not in text, forbidden
    # Tekton mounts the workspace itself, at the runner's path: only the exchange and the agent's /tmp here.
    assert {m["name"]: m["mountPath"] for m in agent["volumeMounts"]} == {
        "exchange": "/exchange",
        "agent-tmp": "/tmp",
    }
    assert task["volumes"] == [{"name": "exchange", "emptyDir": {}}, {"name": "agent-tmp", "emptyDir": {}}]
    # The runner's step reaches the launcher over loopback, shares /exchange, and keeps its own requests.
    step = _step(task)
    env = {e["name"]: e.get("value") for e in step["env"]}
    assert env[AGENT_ISOLATION_ENV] == "container"
    assert env["TRANIBOR_RUNNER_LAUNCHER_PORT"] == "8766" and env["TMPDIR"] == "/exchange"
    assert {"name": "exchange", "mountPath": "/exchange"} in step["volumeMounts"]
    assert step["computeResources"] == {
        "requests": separate_agent.RUNNER_REQUESTS,
        "limits": separate_agent.RUNNER_LIMITS,
    }
    assert run["spec"]["taskRunTemplate"]["podTemplate"]["enableServiceLinks"] is False


def test_a_code_agents_image_is_the_sidecars_and_the_runner_stays_the_platforms() -> None:
    task = _stage(_run(agent_isolation="container", agent_image="registry.example/agent@sha256:" + "b" * 64))
    [agent] = task["sidecars"]
    assert agent["image"].startswith("registry.example/agent@") and _step(task)["image"] == RUNNER


def test_a_command_other_than_run_gets_no_result_flags() -> None:
    step = _step(_stage(_run(runner_command=["serve-session"])))
    assert step["args"] == ["serve-session"]


async def test_a_container_run_reaches_the_cluster_with_the_sidecar() -> None:
    api = FakeKubernetesApi()
    executor = TektonExecutor(client=api)  # type: ignore[arg-type]
    assert SEPARATE_AGENT in executor.capabilities
    ref = await executor.start(_spec(agent_isolation="container"))
    created = api.pipelineruns[ref.name]
    assert [s["name"] for s in _stage(created)["sidecars"]] == ["agent"]
    assert ("POST", f"{TEKTON_API}/namespaces/proj-demo-runners/pipelineruns") in api.calls


@pytest.mark.parametrize("isolation", ["process", "container"])
def test_no_pipeline_is_named_in_the_configuration(isolation: str) -> None:
    """The `pipeline` knob is gone with the Pipeline it named: nothing but the run's spec says what runs."""
    run = _run(agent_isolation=isolation)
    assert "tranibor-agent" not in yaml.safe_dump(run["spec"]["pipelineSpec"])


class _PodLogsApi:
    """The API server's pods of one PipelineRun and their logs, per container (#2279).

    A label selector is matched as the API server matches it (every `key=value`), and a log asked of a
    container the pod does not have is refused with a 400, as the API server refuses it."""

    def __init__(self, pods: list[dict[str, Any]], logs: dict[tuple[str, str], str]) -> None:
        self.pods = pods
        self.logs = logs

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert method == "GET"
        params = kwargs.get("params") or {}
        if path.endswith("/pods"):
            wanted = dict(term.split("=", 1) for term in params["labelSelector"].split(","))
            return {"items": [p for p in self.pods if wanted.items() <= p["metadata"]["labels"].items()]}
        pod, container = path.split("/pods/", 1)[1].removesuffix("/log"), params["container"]
        if (pod, container) not in self.logs:
            raise UpstreamError(
                "kubernetes", f"container {container} is not valid for pod {pod}", status_code=400
            )
        return self.logs[(pod, container)]


def _task_pod(name: str, task: str, containers: list[str]) -> dict[str, Any]:
    return {
        "metadata": {
            "name": name,
            "labels": {"tekton.dev/pipelineRun": "run-r-1", "tekton.dev/pipelineTask": task},
        },
        "spec": {"containers": [{"name": c} for c in containers]},
    }


async def test_logs_read_each_tasks_own_step_container() -> None:
    """The clone's pod is read from the catalogue's `step-clone`, the agent stage's from `step-run` alone."""
    api = _PodLogsApi(
        # The agent stage's pod listed first, as the API may list it: the log keeps the pipeline's order.
        pods=[
            _task_pod("run-r-1-agent-stage-pod", "agent-stage", ["step-run", "sidecar-agent"]),
            _task_pod("run-r-1-clone-pod", "clone", ["prepare", "step-clone"]),
            {"metadata": {"name": "other-run-pod", "labels": {"tekton.dev/pipelineRun": "run-r-2"}}},
        ],
        logs={
            ("run-r-1-clone-pod", "step-clone"): "cloned main\n",
            ("run-r-1-agent-stage-pod", "step-run"): "runner started\nrunner done\n",
            ("run-r-1-agent-stage-pod", "sidecar-agent"): "the agent's own output\n",
        },
    )
    executor = TektonExecutor(client=api)  # type: ignore[arg-type]
    ref = ExecRef(kind=ExecutorKind.TEKTON, name="run-r-1", namespace="proj-demo-runners", run_id="r-1")
    assert [line async for line in executor.logs(ref)] == ["cloned main", "runner started", "runner done"]


async def test_logs_of_a_run_without_a_repository_are_the_runners() -> None:
    api = _PodLogsApi(
        pods=[_task_pod("run-r-1-agent-stage-pod", "agent-stage", ["step-run"])],
        logs={("run-r-1-agent-stage-pod", "step-run"): "runner done\n"},
    )
    executor = TektonExecutor(client=api)  # type: ignore[arg-type]
    ref = ExecRef(kind=ExecutorKind.TEKTON, name="run-r-1", namespace="proj-demo-runners", run_id="r-1")
    assert [line async for line in executor.logs(ref)] == ["runner done"]


def _tekton_pod_labels(run: dict[str, Any], task: str) -> dict[str, str]:
    """The labels Tekton gives a pipeline task's pod (#2278): the PipelineRun's labels, then its own, then
    the embedded Task's `metadata.labels` for a key not set yet (`combineTaskRunAndTaskSpecLabels`); the pod
    gets its TaskRun's (`pod.makeLabels`)."""
    labels: dict[str, str] = {
        **run["metadata"]["labels"],
        "tekton.dev/pipelineRun": run["metadata"]["name"],
        "tekton.dev/pipelineTask": task,
    }
    [pipeline_task] = [t for t in run["spec"]["pipelineSpec"]["tasks"] if t["name"] == task]
    for key, value in (pipeline_task.get("taskSpec", {}).get("metadata", {}).get("labels") or {}).items():
        labels.setdefault(key, value)
    return {**labels, "app.kubernetes.io/managed-by": "tekton-pipelines"}


@pytest.mark.parametrize("isolation", ["process", "container"])
def test_the_agent_stages_pod_carries_the_labels_of_k8s_jobs_run_pod(isolation: str) -> None:
    """The run-pod NetworkPolicies select `k8s_job`'s run pods by these labels (#2278, #2082)."""
    spec = _spec(agent_isolation=isolation)
    job = KubernetesJobExecutor(client=object())._job(spec, "run-r-1")  # type: ignore[arg-type]
    job_pod: dict[str, str] = job["spec"]["template"]["metadata"]["labels"]
    executor = TektonExecutor(client=object())  # type: ignore[arg-type]
    run = executor._pipeline_run(spec, "run-r-1")
    # A project with a repository: the clone task first.
    run["spec"]["pipelineSpec"] = executor._pipeline_spec(spec, "run-r-1", cloned=True)
    assert _stage(run)["metadata"]["labels"] == RUNNER_POD_LABELS
    stage_pod = _tekton_pod_labels(run, "agent-stage")
    assert {k: stage_pod.get(k) for k in job_pod} == job_pod
    # The clone's pod runs the catalogue's `git-clone`, nothing of the agent's, and reaches the git host:
    # it stays outside the run-pod policy.
    assert not RUNNER_POD_LABELS.keys() & _tekton_pod_labels(run, "clone").keys()


@pytest.mark.parametrize("isolation", ["process", "container"])
def test_the_egress_gate_is_the_agent_stages_first_step(isolation: str) -> None:
    """The gate (S80-07f) holds the runner's step, which alone asks the agent's launcher to run anything,
    until the pod's egress policy is in force (#2278). The same container as `k8s_job`'s init container."""
    gate = EgressGate(target="10.9.9.9:443", deadline_s=12)
    spec = _spec(agent_isolation=isolation)
    run = TektonExecutor(client=object(), egress_gate=gate)._pipeline_run(spec, "run-r-1")  # type: ignore[arg-type]
    task = _stage(run)
    first, runner = task["steps"]
    assert first["name"] == GATE_CONTAINER and runner["name"] == "run"
    job = KubernetesJobExecutor(client=object(), egress_gate=gate)._job(spec, "run-r-1")  # type: ignore[arg-type]
    init = job["spec"]["template"]["spec"]["initContainers"][0]
    assert {k: v for k, v in first.items() if k != "computeResources"} == {
        k: v for k, v in init.items() if k != "resources"
    }
    assert first["computeResources"] == init["resources"] and "resources" not in first
    assert egress_gate.runner_mount() in runner["volumeMounts"]
    assert {"name": REPORT_ENV, "value": "/egress-gate/report.json"} in runner["env"]
    assert egress_gate.volume() in task["volumes"]


def test_without_a_gate_the_agent_stage_has_the_runners_step_alone() -> None:
    task = _stage(_run())
    assert [s["name"] for s in task["steps"]] == ["run"] and "volumes" not in task


def test_the_deployment_turns_the_gate_on_for_tekton(monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_adapters.registry import _REGISTRY

    config = {"kubernetes": {"base_url": "https://k8s.example", "token": "t", "verify": False}}
    monkeypatch.delenv("TRANIBOR_RUNNER_EGRESS_GATE", raising=False)
    assert _REGISTRY[("runtime", "tekton")](config).egress_gate is None
    monkeypatch.setenv("TRANIBOR_RUNNER_EGRESS_GATE", "true")
    monkeypatch.setenv("TRANIBOR_RUNNER_EGRESS_GATE_DEADLINE", "20")
    assert _REGISTRY[("runtime", "tekton")](config).egress_gate == EgressGate(deadline_s=20)
