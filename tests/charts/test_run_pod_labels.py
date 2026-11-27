"""Finding #2082: every pod an agent runs in carries the labels its network policies select.

The warm pool's pods (`agent_sandbox`, the chart's `SandboxTemplate`) carry `choregos/sandbox: "true"`,
the label an operator's run-pod policy selects (for example a `choregos-sandbox-egress`: a Cilium refusal
of everything but the API, the model gateway, the egress proxy and the memory). The `k8s_job`
executor's pods did not, so nothing but the namespace's baseline applied to them: from their agent's
container the dev Temporal server, its Redis and the web search answered without credentials.

The two pod shapes are built here the way they really are, the chart's rendered `SandboxTemplate` and
the Job the executor posts, and these tests fail when they part again:

- a Job's pod carries every label of the warm pool's pod, and the warm pool's pod every label of a
  Job's pod but the run's own identifier;
- every NetworkPolicy the chart renders selects both shapes or neither;
- the run-pod selector a platform is told to use (`tranibor/sandbox: "true"`, docs/deployment.md), and
  the former one an operator's policy written before 0.30.0 holds (`choregos/sandbox: "true"`), select
  both (ADR 0226, R9b: the label is written under both keys for the window).
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml
from tranibor_adapters.executor.k8s_job import RUNNER_POD_LABELS, KubernetesJobExecutor
from tranibor_adapters.executor.tekton import AGENT_STAGE, TektonExecutor
from tranibor_core.domain import StageJobSpec

from .conftest import OPERATOR_API
from .test_single_node_agent_network import _selects

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
#: What a platform that holds agent pods by a policy of its own selects (for example an operator's
#: `choregos-sandbox-egress` and `choregos-sandbox-assignment`).
PLATFORM_RUN_POD_SELECTORS = (
    {"matchLabels": {"tranibor/sandbox": "true"}},
    {"matchLabels": {"choregos/sandbox": "true"}},
)
#: The labels a Job's pod carries that belong to ONE run, not to the family.
PER_RUN = {"tranibor/run-id", "choregos/run-id"}

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _rendered(*overrides: str) -> list[dict[str, Any]]:
    # The operator's API named here: a module-scoped render runs before the conftest's autouse fixture.
    command = ["helm", "template", "choregos", str(CHART), "-n", "choregos", "--api-versions", OPERATOR_API]
    for override in overrides:
        command += ["--set", override]
    out = subprocess.run(command, capture_output=True, text=True, check=True, timeout=120).stdout
    return [d for d in yaml.safe_load_all(out) if d]


@pytest.fixture(scope="module")
def rendered() -> list[dict[str, Any]]:
    """The chart on the warm pool, with its own NetworkPolicies and the assignment policy."""
    return _rendered(
        "tranibor-orchestrator.runner.executor=agent_sandbox",
        "tranibor-orchestrator.runner.namespacePattern=choregos",
        "global.rbac.jobRunner=true",
        "networkPolicies.enabled=true",
    )


@pytest.fixture(scope="module")
def sandbox_pod(rendered: list[dict[str, Any]]) -> dict[str, str]:
    template = next(d for d in rendered if d["kind"] == "SandboxTemplate")
    labels: dict[str, str] = template["spec"]["podTemplate"]["metadata"]["labels"]
    return labels


class _RecordingClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, Any]] = []

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if method == "POST":
            self.posted.append((path, kwargs.get("json")))
        return None  # nothing exists yet: the Job and its Secret are created


@pytest.fixture
async def job_pod() -> dict[str, str]:
    """The labels of the pod the `k8s_job` executor really asks for."""
    client = _RecordingClient()
    spec = StageJobSpec(
        run_id="r-1",
        project_slug="demo",
        namespace="choregos",
        runner_image="harbor.example/choregos/tranibor-runner:0.1.0",
        api_url="http://choregos-api:8000",
        run_token="token",
    )
    await KubernetesJobExecutor(client=client).start(spec)  # type: ignore[arg-type]
    job = next(body for path, body in client.posted if path.endswith("/jobs"))
    labels: dict[str, str] = job["spec"]["template"]["metadata"]["labels"]
    return labels


async def test_a_job_s_pod_carries_every_label_of_the_warm_pool_s_pod(
    sandbox_pod: dict[str, str], job_pod: dict[str, str]
) -> None:
    missing = {k: v for k, v in sandbox_pod.items() if job_pod.get(k) != v}
    assert not missing, f"a k8s_job pod lacks the warm pool's {missing}: a policy on those misses it"


async def test_the_warm_pool_s_pod_carries_every_family_label_of_a_job_s_pod(
    sandbox_pod: dict[str, str], job_pod: dict[str, str]
) -> None:
    missing = {k: v for k, v in job_pod.items() if k not in PER_RUN and sandbox_pod.get(k) != v}
    assert not missing, f"the warm pool's pod lacks a k8s_job pod's {missing}"
    assert {k: v for k, v in job_pod.items() if k not in PER_RUN} == RUNNER_POD_LABELS


async def test_every_chart_policy_selects_both_pod_shapes_or_neither(
    rendered: list[dict[str, Any]], sandbox_pod: dict[str, str], job_pod: dict[str, str]
) -> None:
    policies = [d for d in rendered if d["kind"] == "NetworkPolicy"]
    names = {p["metadata"]["name"] for p in policies}
    assert {"tranibor-runner-assignment", "tranibor-runner-egress"} <= names, names
    apart = [
        policy["metadata"]["name"]
        for policy in policies
        if _selects(selector := policy["spec"].get("podSelector"), sandbox_pod) != _selects(selector, job_pod)
    ]
    assert not apart, f"policies that hold one executor's agent pods and not the other's: {apart}"


@pytest.fixture
def tekton_agent_pod() -> dict[str, str]:
    """The labels of the agent stage's pod of the PipelineRun the `tekton` executor asks for (#2278).

    As Tekton gives them: the PipelineRun's, then its own, then the embedded Task's `metadata.labels`
    for a key not set yet (`combineTaskRunAndTaskSpecLabels`); the pod gets its TaskRun's."""
    spec = StageJobSpec(
        run_id="r-1",
        project_slug="demo",
        namespace="tranibor",
        runner_image="harbor.example/tranibor/tranibor-runner:0.1.0",
        api_url="http://tranibor-api:8000",
        run_token="token",
    )
    run = TektonExecutor(client=object())._pipeline_run(spec, "run-r-1")  # type: ignore[arg-type]
    [stage] = [t for t in run["spec"]["pipelineSpec"]["tasks"] if t["name"] == AGENT_STAGE]
    labels: dict[str, str] = {
        **run["metadata"]["labels"],
        "tekton.dev/pipelineRun": "run-r-1",
        "tekton.dev/pipelineTask": AGENT_STAGE,
    }
    for key, value in stage["taskSpec"].get("metadata", {}).get("labels", {}).items():
        labels.setdefault(key, value)
    return {
        **labels,
        "tekton.dev/taskRun": "run-r-1-agent-stage",
        "app.kubernetes.io/managed-by": "tekton-pipelines",
    }


async def test_every_chart_policy_selects_a_tekton_agent_pod_as_a_job_s_pod(
    rendered: list[dict[str, Any]], job_pod: dict[str, str], tekton_agent_pod: dict[str, str]
) -> None:
    """#2278: the agent stage's pod of a PipelineRun is held by the policies that hold a Job's pod."""
    apart = [
        policy["metadata"]["name"]
        for policy in rendered
        if policy["kind"] == "NetworkPolicy"
        and _selects(selector := policy["spec"].get("podSelector"), job_pod)
        != _selects(selector, tekton_agent_pod)
    ]
    assert not apart, f"policies that hold a Job's agent pod and not a PipelineRun's: {apart}"
    for selector in PLATFORM_RUN_POD_SELECTORS:
        assert _selects(selector, tekton_agent_pod), f"a tekton agent pod is outside {selector}"


@pytest.mark.parametrize("selector", PLATFORM_RUN_POD_SELECTORS, ids=["tranibor", "choregos"])
async def test_the_platform_s_run_pod_selector_selects_both_pod_shapes(
    selector: dict[str, Any], sandbox_pod: dict[str, str], job_pod: dict[str, str]
) -> None:
    assert _selects(selector, sandbox_pod)
    assert _selects(selector, job_pod), "a k8s_job pod is outside the run-pod policy"


async def test_a_job_s_pod_carries_its_run_under_both_keys(job_pod: dict[str, str]) -> None:
    assert job_pod["tranibor/run-id"] == job_pod["choregos/run-id"] == "r-1"
