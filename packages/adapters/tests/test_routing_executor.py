"""S40-03c: a sandbox deployment runs a step with its own image on a second executor (ADR 0072)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from tranibor_adapters import registry
from tranibor_adapters.executor.routing import RoutingExecutor
from tranibor_adapters.fakes.executor import FakeExecutor
from tranibor_adapters.registry import second_executor_kind, with_custom_image_executor
from tranibor_contracts import ExecutorKind
from tranibor_core.agent_isolation import SEPARATE_AGENT
from tranibor_core.domain import ExecStatus, StageJobSpec

POOL = "registry.example/choregos-runner:1.0"


class _Sandbox(FakeExecutor):
    kind = ExecutorKind.AGENT_SANDBOX


class _Jobs(FakeExecutor):
    kind = ExecutorKind.K8S_JOB


def _spec(run_id: str, image: str) -> StageJobSpec:
    return StageJobSpec(
        run_id=run_id,
        project_slug="demo",
        namespace="choregos",
        runner_image=image,
        api_url="http://choregos-api:8000",
        run_token="token",
    )


def _routing() -> tuple[RoutingExecutor, _Sandbox, _Jobs]:
    sandbox = _Sandbox(capabilities=frozenset({"queue", "suspend", "resume", "renew"}))
    jobs = _Jobs(capabilities=frozenset({"queue"}))
    return RoutingExecutor(sandbox, jobs, POOL), sandbox, jobs


async def test_a_pool_image_step_goes_to_the_sandbox_and_a_custom_image_to_jobs() -> None:
    routing, sandbox, jobs = _routing()
    pool_ref = await routing.start(_spec("r-pool", POOL))
    default_ref = await routing.start(_spec("r-default", POOL))
    custom_ref = await routing.start(_spec("r-custom", "registry.example/agent-tools:3"))
    assert [s.run_id for s in sandbox.started] == ["r-pool", "r-default"]
    assert [s.run_id for s in jobs.started] == ["r-custom"]
    assert str(pool_ref.kind) == "agent_sandbox" and str(custom_ref.kind) == "k8s_job"
    assert str(default_ref.kind) == "agent_sandbox"


async def test_what_follows_start_reaches_the_executor_that_started_the_run() -> None:
    routing, sandbox, jobs = _routing()
    custom_ref = await routing.start(_spec("r-custom", "registry.example/agent-tools:3"))
    pool_ref = await routing.start(_spec("r-pool", POOL))
    jobs.statuses["r-custom"] = ExecStatus(state="succeeded")
    sandbox.statuses["r-pool"] = ExecStatus(state="running")
    assert (await routing.status(custom_ref)).state == "succeeded"
    assert (await routing.status(pool_ref)).state == "running"
    assert [line async for line in routing.logs(custom_ref)] == jobs.log_lines
    await routing.cancel(custom_ref)
    assert jobs.cancelled and not sandbox.cancelled


async def test_capabilities_are_the_delegate_s() -> None:
    routing, _, _ = _routing()
    custom_ref = await routing.start(_spec("r-custom", "registry.example/agent-tools:3"))
    pool_ref = await routing.start(_spec("r-pool", POOL))
    assert routing.capabilities == {"queue", "suspend", "resume", "renew"}
    assert routing.capabilities_for(pool_ref) == {"queue", "suspend", "resume", "renew"}
    assert routing.capabilities_for(custom_ref) == {"queue"}


async def test_a_run_on_the_secondary_is_never_suspended() -> None:
    routing, sandbox, jobs = _routing()
    jobs_ref = await routing.start(_spec("r-custom", "registry.example/agent-tools:3"))
    sandbox_ref = await routing.start(_spec("r-pool", POOL))
    until = datetime(2026, 10, 17, tzinfo=UTC)
    assert await routing.suspend(sandbox_ref, until) is True
    assert sandbox.suspended and not jobs.suspended
    jobs.suspend = None  # type: ignore[assignment,method-assign]  # a k8s_job has no `suspend`
    assert await routing.suspend(jobs_ref, until) is False


def test_the_wiring_wraps_only_a_sandbox_with_a_known_pool_and_a_named_second_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox = _Sandbox()
    monkeypatch.delenv("TRANIBOR_CUSTOM_IMAGE_EXECUTOR", raising=False)
    monkeypatch.setenv("TRANIBOR_RUNNER_IMAGE", POOL)
    assert with_custom_image_executor(sandbox) is sandbox, "no second executor named: the refusal stays"
    monkeypatch.setenv("TRANIBOR_CUSTOM_IMAGE_EXECUTOR", "k8s_job")
    monkeypatch.setenv("TRANIBOR_FAKES", "1")  # `build` then answers with a fake: no cluster needed here
    wrapped = with_custom_image_executor(sandbox)
    assert isinstance(wrapped, RoutingExecutor) and wrapped.pool_image == POOL
    assert isinstance(wrapped.secondary, FakeExecutor)
    jobs = _Jobs()
    assert with_custom_image_executor(jobs) is jobs, "only agent_sandbox and tekton are wrapped"
    monkeypatch.delenv("TRANIBOR_RUNNER_IMAGE")
    assert with_custom_image_executor(sandbox) is sandbox, "an unknown pool image: an override cannot be told"


# S86-02: a deployment on `tekton` sends an agent step Tekton cannot isolate to the second executor ─────


class _Tekton(FakeExecutor):
    kind = ExecutorKind.TEKTON


def _container(run_id: str, image: str = POOL, isolation: str = "container") -> StageJobSpec:
    return _spec(run_id, image).model_copy(update={"agent_isolation": isolation})


def _wrapped_tekton(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Tekton, _Jobs]:
    """`with_custom_image_executor` around a Tekton that does not declare `separate_agent`, as the chart
    wires it (`TRANIBOR_CUSTOM_IMAGE_EXECUTOR=k8s_job`, no pool image: Tekton has none)."""
    tekton = _Tekton(capabilities=frozenset())
    jobs = _Jobs(capabilities=frozenset({"queue", SEPARATE_AGENT}))
    monkeypatch.setenv("TRANIBOR_CUSTOM_IMAGE_EXECUTOR", "k8s_job")
    monkeypatch.delenv("TRANIBOR_RUNNER_IMAGE", raising=False)
    built: list[tuple[str, str]] = []

    def build(kind: str, type_name: str, config: dict[str, Any] | None = None) -> Any:
        built.append((kind, type_name))
        return jobs

    monkeypatch.setattr(registry, "build", build)
    wrapped = with_custom_image_executor(tekton)
    assert built == [("runtime", "k8s_job")]
    return wrapped, tekton, jobs


async def test_a_container_run_on_a_tekton_primary_lands_on_k8s_job(monkeypatch: pytest.MonkeyPatch) -> None:
    wrapped, tekton, jobs = _wrapped_tekton(monkeypatch)
    ref = await wrapped.start(_container("r-agent"))
    assert str(ref.kind) == "k8s_job"
    assert [s.run_id for s in jobs.started] == ["r-agent"] and tekton.started == []
    # What follows `start` reaches the executor that started the run.
    jobs.statuses["r-agent"] = ExecStatus(state="running")
    assert (await wrapped.status(ref)).state == "running"


async def test_on_tekton_a_job_step_with_its_own_image_and_a_process_run_stay_on_tekton(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tekton runs any image: only the agent's isolation routes, never the step's image. Under `container`
    a job step goes too: the runner waits for the launcher in every run, a job's included."""
    wrapped, tekton, jobs = _wrapped_tekton(monkeypatch)
    await wrapped.start(_container("r-job", "registry.example/agent-tools:3", isolation="process"))
    await wrapped.start(_container("r-process", isolation="process"))
    assert [s.run_id for s in tekton.started] == ["r-job", "r-process"] and jobs.started == []
    await wrapped.start(_container("r-job-isolated", "registry.example/agent-tools:3"))
    assert [s.run_id for s in jobs.started] == ["r-job-isolated"]


def test_tekton_is_wrapped_without_a_pool_image_and_left_alone_without_a_second_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrapped, _, _ = _wrapped_tekton(monkeypatch)
    assert isinstance(wrapped, RoutingExecutor) and wrapped.pool_image is None
    assert second_executor_kind("tekton") == "k8s_job"
    monkeypatch.delenv("TRANIBOR_CUSTOM_IMAGE_EXECUTOR")
    tekton = _Tekton(capabilities=frozenset())
    assert with_custom_image_executor(tekton) is tekton, "no second executor named: the refusal stays"
    assert second_executor_kind("tekton") is None
