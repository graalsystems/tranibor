"""S86-05: Tekton's PipelineRun carries its Pipeline and the agent stage's Task inline, and under `container`
the agent stage carries the agent's own container as a sidecar, the one `k8s_job` starts.

Not proved here: that Tekton's controller starts the sidecar before the runner's step, mounts the workspace
in it, and stops it after the last step, with or without `enable-kubernetes-sidecar` (S86-06, a cluster).
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from tranibor_adapters.executor import separate_agent
from tranibor_adapters.executor.k8s_job import KubernetesJobExecutor
from tranibor_adapters.executor.tekton import TEKTON_API, TektonExecutor
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_core.agent_isolation import AGENT_ISOLATION_ENV, SEPARATE_AGENT
from tranibor_core.domain import StageJobSpec

RUNNER = "registry.example/choregos-runner:1.0"


def _spec(**changes: Any) -> StageJobSpec:
    base: dict[str, Any] = {
        "run_id": "r-1",
        "project_slug": "demo",
        "namespace": "proj-demo-runners",
        "runner_image": RUNNER,
        "api_url": "http://choregos-api:8000",
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


def test_under_process_the_agent_stage_has_no_sidecar_and_the_runner_holds_the_run() -> None:
    task = _stage(_run())
    assert "sidecars" not in task and "volumes" not in task
    step = _step(task)
    assert step["image"] == RUNNER and step["command"] == ["choregos-runner"]
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
    assert "choregos-agent" not in yaml.safe_dump(run["spec"]["pipelineSpec"])
