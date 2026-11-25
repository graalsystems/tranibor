"""ADR 0222: in staging and prod no executor starts a run whose agent would share the runner's container
unless the operator named the opt-out; the refusal names the setting, nothing reaches the backend, and an
accepted opt-out is logged. Dev, test and the fakes keep running as before."""

from __future__ import annotations

from typing import Any

import pytest
import structlog
from tranibor_adapters.errors import AgentIsolationRefusedError
from tranibor_adapters.executor.aca import AcaExecutor
from tranibor_adapters.executor.agent_sandbox import AgentSandboxExecutor
from tranibor_adapters.executor.k8s_job import KubernetesJobExecutor
from tranibor_adapters.executor.local_docker import LocalDockerExecutor
from tranibor_adapters.executor.tekton import TektonExecutor
from tranibor_adapters.fakes.executor import FakeExecutor
from tranibor_core.agent_isolation import AGENT_ISOLATION_UNAVAILABLE, NONE_ACCEPTED
from tranibor_core.domain import StageJobSpec

KINDS = ["k8s_job", "agent_sandbox", "local_docker", "tekton", "aca"]


class _Recording:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.scope = "/subscriptions/s/resourceGroups/g"
        self.resource_group = "g"
        self.base_url = "https://management.example"

    async def request(self, method: str, path: str, **_: Any) -> Any:
        self.calls.append((method, path))
        return None


class _Docker(LocalDockerExecutor):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, ...]] = []

    async def _run_raw(
        self, *args: str, timeout: float = 120.0, env: dict[str, str] | None = None
    ) -> tuple[int, bytes, str]:
        self.calls.append(args)
        return 1, b"", ""


def _executor(kind: str) -> tuple[Any, Any]:
    if kind == "local_docker":
        docker = _Docker()
        return docker, docker
    client = _Recording()
    built: dict[str, Any] = {
        "k8s_job": lambda: KubernetesJobExecutor(client=client),  # type: ignore[arg-type]
        "agent_sandbox": lambda: AgentSandboxExecutor(client=client),  # type: ignore[arg-type]
        "tekton": lambda: TektonExecutor(client=client),  # type: ignore[arg-type]
        "aca": lambda: AcaExecutor(client, environment_id="env"),  # type: ignore[arg-type]
    }
    return built[kind](), client


def _spec(environment: str, *, isolation: str = "process", accepted: bool = False) -> StageJobSpec:
    return StageJobSpec(
        run_id="r-1",
        project_slug="demo",
        namespace="choregos",
        runner_image="registry.example/choregos-runner:1.0",
        api_url="http://choregos-api:8000",
        run_token="token",
        agent_isolation=isolation,  # type: ignore[arg-type]
        environment=environment,
        agent_isolation_accepted=accepted,
    )


@pytest.mark.parametrize("environment", ["staging", "prod"])
@pytest.mark.parametrize("kind", KINDS)
async def test_every_executor_refuses_an_unisolated_agent_in_production(kind: str, environment: str) -> None:
    executor, recorder = _executor(kind)
    with pytest.raises(AgentIsolationRefusedError) as refused:
        await executor.start(_spec(environment))
    message = str(refused.value)
    assert refused.value.code == AGENT_ISOLATION_UNAVAILABLE
    assert f"`{kind}`" in message, "names the executor"
    assert f"TRANIBOR_AGENT_ISOLATION={NONE_ACCEPTED}" in message, "names the opt-out"
    assert "global.runner.agentIsolation" in message, "names the way to isolate"
    assert recorder.calls == [], "nothing reaches the backend"


@pytest.mark.parametrize("environment", ["dev", "test", ""])
@pytest.mark.parametrize("kind", ["k8s_job", "agent_sandbox", "tekton", "aca"])
async def test_outside_production_process_runs_as_before(kind: str, environment: str) -> None:
    executor, recorder = _executor(kind)
    await executor.start(_spec(environment))
    assert recorder.calls, "the run reached the backend"


@pytest.mark.parametrize("kind", ["k8s_job", "agent_sandbox", "tekton", "aca"])
async def test_the_named_opt_out_is_accepted_and_logged(kind: str) -> None:
    executor, recorder = _executor(kind)
    with structlog.testing.capture_logs() as logs:
        await executor.start(_spec("prod", accepted=True))
    assert recorder.calls, "the run reached the backend"
    (entry,) = [log for log in logs if log["event"] == "agent_isolation.none_accepted"]
    assert entry["log_level"] == "warning"
    assert entry["run_id"] == "r-1" and entry["executor"] == kind and entry["environment"] == "prod"
    assert entry["setting"] == f"TRANIBOR_AGENT_ISOLATION={NONE_ACCEPTED}"


async def test_local_docker_takes_the_opt_out_past_the_guard() -> None:
    docker = _Docker()
    with pytest.raises(Exception) as stopped:
        await docker.start(_spec("prod", accepted=True))
    assert not isinstance(stopped.value, AgentIsolationRefusedError)
    assert docker.calls, "past the guard, docker was asked"


@pytest.mark.parametrize("kind", ["k8s_job", "agent_sandbox", "local_docker"])
async def test_an_isolated_run_needs_no_opt_out_in_production(kind: str) -> None:
    """The isolating executors are not touched: `container` runs in prod with nothing to accept."""
    executor, _ = _executor(kind)
    if kind == "agent_sandbox":
        executor = AgentSandboxExecutor(client=_Recording(), agent_isolation="container")  # type: ignore[arg-type]
    with structlog.testing.capture_logs() as logs:
        try:
            await executor.start(_spec("prod", isolation="container"))
        except AgentIsolationRefusedError:  # pragma: no cover - the failure this test exists for
            raise
        except Exception:
            pass
    assert not [log for log in logs if log["event"] == "agent_isolation.none_accepted"]


async def test_the_fakes_are_not_judged() -> None:
    await FakeExecutor().start(_spec("prod"))


async def test_a_hosted_session_pod_with_no_environment_is_not_judged() -> None:
    """`serve-session` starts no agent of its own and leaves the field empty (ADR 0222)."""
    executor, recorder = _executor("k8s_job")
    await executor.start(_spec(""))
    assert recorder.calls
