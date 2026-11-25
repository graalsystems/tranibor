"""An executor announces its capabilities, and cannot announce one it does not have.

This is the **seam** through which a more capable sandbox will come in: Agent Substrate,
a snapshot runtime, or something else. It is laid down now, while a single executor fills a
single capability, so that on that day nothing else moves — the orchestrator will ask what
the runtime can do instead of assuming it.

A capability announced and absent is worse than absent: the caller relies on it.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from tranibor_adapters import available, build
from tranibor_adapters.base import EXECUTOR_CAPABILITIES

pytestmark = pytest.mark.conformance

#: The method each capability obliges to implement. `queue` imposes none: it plays out in
#: `start` and `status`, and its behaviour is checked by the tests of the executor
#: concerned (ceiling reached, queue in arrival order).
#: `snapshot` imposes none either since ADR 0053: it is a property of `start` (every run gets a
#: sandbox made from the template, never a used one), proved by the executor's own tests.
#: `separate_agent` neither (ADR 0146 §9): a property of `start` too, the agent's own container in the
#: run's pod; an executor without it refuses a run under `container` (`test_agent_isolation_refusal.py`).
EXPECTED_METHOD = {"renew": "renew", "suspend": "suspend", "resume": "resume"}

# Building an executor outside a cluster: without `token`, the Kubernetes client goes and reads
# the service-account token mounted in a pod, and that file does not exist here.
KUBE = {"kubernetes": {"base_url": "https://exemple.invalid", "token": "hors-cluster", "verify": False}}
CONFIGS = {
    "k8s_job": KUBE,
    "tekton": KUBE,
    "local_docker": {},
    "aca": {"subscription_id": "s", "resource_group": "g", "environment_id": "e"},
    "agent_sandbox": KUBE,
    "fake": {},
}


def _executors() -> list[str]:
    return sorted(available("runtime"))


def test_the_vocabulary_stays_closed() -> None:
    """A capability invented on the fly means nothing to the caller."""
    assert (
        frozenset({"queue", "renew", "suspend", "resume", "snapshot", "separate_agent"})
        == EXECUTOR_CAPABILITIES
    )


@pytest.mark.parametrize("name", _executors())
def test_each_executor_declares_its_capabilities(name: str) -> None:
    executor = build("runtime", name, CONFIGS.get(name, {}))
    capabilities = getattr(executor, "capabilities", None)
    assert isinstance(capabilities, frozenset), f"{name} announces nothing: a caller would have to guess"
    inconnues = capabilities - EXECUTOR_CAPABILITIES
    assert not inconnues, f"{name} announces capabilities outside the vocabulary: {sorted(inconnues)}"


@pytest.mark.parametrize("name", _executors())
def test_what_is_announced_is_implemented(name: str) -> None:
    executor = build("runtime", name, CONFIGS.get(name, {}))
    for capability in sorted(getattr(executor, "capabilities", frozenset())):
        methode = EXPECTED_METHOD.get(capability)
        if methode is None:
            continue
        fonction = getattr(executor, methode, None)
        assert callable(fonction), f"{name} announces `{capability}` without `{methode}()`"
        assert inspect.iscoroutinefunction(fonction), f"{name}.{methode}() must be asynchronous"


def test_only_agent_sandbox_starts_every_run_from_a_clean_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """A FACT, not a rule. Until S35-02 it read "no executor can take a snapshot yet", and it
    turned red the day `agent_sandbox` announced `snapshot`: its purpose (ADR 0013, Decision 4).

    What `snapshot` means is now ADR 0016's and ADR 0053's: every run gets a sandbox made from the
    template and never used before, which is what makes a warm pool defensible again. Freezing a
    RUNNING sandbox and restoring it is still offered by no executor. The day this list changes,
    re-read those three records before adding a name.
    """
    # The real executors, not the fakes another test may have switched on.
    monkeypatch.delenv("TRANIBOR_FAKES", raising=False)
    with_snapshot = [
        name
        for name in _executors()
        if "snapshot" in getattr(build("runtime", name, CONFIGS.get(name, {})), "capabilities", frozenset())
    ]
    assert with_snapshot == ["agent_sandbox"], (
        f"{with_snapshot} announce `snapshot`: re-read docs/adr/0013, 0016 and 0053 "
        "(warm pool of never-used sandboxes; no restore of a running one)"
    )


async def test_tekton_declares_the_agents_own_container_and_starts_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S86-05: the agent stage's Task carries the agent's container as a sidecar (ADR 0146 §9). Tekton
    declares `separate_agent`, has no reason left in `WHY_NOT_SEPARATE`, and a run under `container`
    reaches its backend with the sidecar in the PipelineRun it sends."""
    from tranibor_core.agent_isolation import SEPARATE_AGENT, WHY_NOT_SEPARATE, agent_isolation_refusal
    from tranibor_core.domain import StageJobSpec

    monkeypatch.delenv("TRANIBOR_FAKES", raising=False)
    executor = build("runtime", "tekton", CONFIGS["tekton"])
    assert SEPARATE_AGENT in executor.capabilities
    assert "tekton" not in WHY_NOT_SEPARATE
    assert agent_isolation_refusal("container", "tekton", executor.capabilities) is None
    sent: list[tuple[str, str, Any]] = []

    async def record(method: str, path: str, **kwargs: Any) -> Any:
        sent.append((method, path, kwargs.get("json")))
        return None

    monkeypatch.setattr(executor.client, "request", record)
    spec = StageJobSpec(
        run_id="r-1",
        project_slug="demo",
        namespace="ns",
        runner_image="registry.example/runner:1",
        api_url="http://api",
        run_token="token",
        agent_isolation="container",
    )
    await executor.start(spec)
    [run] = [body for method, path, body in sent if method == "POST" and path.endswith("/pipelineruns")]
    [stage] = run["spec"]["pipelineSpec"]["tasks"]
    assert [s["name"] for s in stage["taskSpec"]["sidecars"]] == ["agent"]


async def test_aca_declares_the_agents_own_container_and_starts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """S86-08: a Container Apps job carries the agent's container beside the runner's (ADR 0146, ADR 0224
    §2): ACA declares `separate_agent`, has no reason left in `WHY_NOT_SEPARATE`, and a run under
    `container` reaches ARM with a job whose second container is the launcher, ending on the runner's
    done file, with no secret; the two share the working tree and the exchange directory."""
    from tranibor_core.agent_isolation import SEPARATE_AGENT, WHY_NOT_SEPARATE, agent_isolation_refusal
    from tranibor_core.domain import StageJobSpec

    monkeypatch.delenv("TRANIBOR_FAKES", raising=False)
    executor = build("runtime", "aca", CONFIGS["aca"])
    assert SEPARATE_AGENT in executor.capabilities
    assert "aca" not in WHY_NOT_SEPARATE
    assert agent_isolation_refusal("container", "aca", executor.capabilities) is None
    sent: list[tuple[str, str, Any]] = []

    async def record(method: str, path: str, **kwargs: Any) -> Any:
        sent.append((method, path, kwargs.get("json")))
        return None

    monkeypatch.setattr(executor.client, "request", record)
    spec = StageJobSpec(
        run_id="r-1",
        project_slug="demo",
        namespace="ns",
        runner_image="registry.example/runner:1",
        api_url="http://api",
        run_token="token",
        agent_isolation="container",
    )
    await executor.start(spec)
    [job] = [body for method, _, body in sent if method == "PUT"]
    runner, agent = job["properties"]["template"]["containers"]
    assert agent["name"] == "agent"
    assert agent["args"][:1] == ["launcher"] and "--until-file" in agent["args"]
    assert not [e for e in agent["env"] if "secretRef" in e], "the agent's container holds no secret"
    assert {e["name"] for e in runner["env"]} >= {
        "TRANIBOR_RUNNER_AGENT_ISOLATION",
        "TRANIBOR_RUNNER_DONE_FILE",
    }
    shared = {m["volumeName"] for m in runner["volumeMounts"]} & {
        m["volumeName"] for m in agent["volumeMounts"]
    }
    assert shared == {"workspace", "exchange"}
