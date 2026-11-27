"""S87-10: the provider profiles of the chart, `charts/tranibor/values/{gke,eks,aks}.yaml`.

Each profile is layered on the all-in-one profile, as `docs/deployment.md` and `tools/cluster-proof/proof.sh`
install it (`-f values/all-in-one.yaml -f values/<provider>.yaml -f my-site.yaml`). What these tests prove is
the RENDER: that it is accepted by kubeconform (strict, every object against a schema, the Gateway API's from
a pinned CRD catalogue), that a profile naming a RuntimeClass puts it on the runner and on the agent's own
container of every agent pod (the warm pool's template, and a `k8s_job` Job built by the executor from the
value the chart hands the workers), and that no profile turns the agent's own container off. They do not
prove that a GKE or EKS cluster runs it: none did (`docs/support/clusters.yaml`); AKS did (S87-13).
"""

from __future__ import annotations

import functools
import pathlib
import shutil
import subprocess
import types
from typing import Any

import pytest
import yaml
from tranibor_adapters.executor.k8s_job import KubernetesJobExecutor
from tranibor_contracts.policy import SandboxPolicy
from tranibor_core.domain import AgentContainer, StageJobSpec
from tranibor_orchestrator.activities.execution import runtime_class
from tranibor_orchestrator.config import OrchestratorSettings

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
VALUES = CHART / "values"
BASE = VALUES / "all-in-one.yaml"
SITE = ("--set", "global.bootstrap.org=acme", "--set", "global.bootstrap.admins=root@acme.example")
OPERATOR_API = "extensions.agents.x-k8s.io/v1beta1/SandboxTemplate"
#: The RuntimeClass each profile names for the agent pods; `""` is the cluster's default, said explicitly.
PROFILES = {"gke": "gvisor", "eks": "", "aks": "kata-vm-isolation"}
#: The CRD catalogue kubeconform reads the Gateway API's HTTPRoute from, pinned to one commit.
CRD_CATALOGUE = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/b50f3fbc45169a14b6d2d3f767b32fd96ef4a3fa/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)
VARIABLE = "TRANIBOR_RUNNER_RUNTIME_CLASS"
ISOLATION = "TRANIBOR_RUNNER_AGENT_ISOLATION"
DIGEST = "sha256:" + "0" * 64

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


@functools.cache
def _rendered(profile: str, *extra: str) -> str:
    command = ["helm", "template", "tranibor", str(CHART), "-n", "tranibor"]
    command += ["-f", str(BASE), "-f", str(VALUES / f"{profile}.yaml"), *SITE, *extra]
    # The `--api-versions` of a cluster with the agent-sandbox operator, for the `agent_sandbox` renders.
    command += ["--api-versions", OPERATOR_API]
    done = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    assert done.returncode == 0, done.stderr
    return done.stdout


def _docs(profile: str, *extra: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all(_rendered(profile, *extra)) if d]


def _of(documents: list[dict[str, Any]], kind: str) -> dict[str, dict[str, Any]]:
    return {d["metadata"]["name"]: d for d in documents if d["kind"] == kind}


def _env(deployment: dict[str, Any]) -> dict[str, str]:
    return {
        e["name"]: e.get("value", "")
        for c in deployment["spec"]["template"]["spec"]["containers"]
        for e in c.get("env", [])
    }


def _told(documents: list[dict[str, Any]], variable: str) -> dict[str, str]:
    """`variable` for each Deployment that carries it: the API and every worker."""
    return {
        name: env[variable]
        for name, d in _of(documents, "Deployment").items()
        if variable in (env := _env(d))
    }


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_the_profile_renders_and_kubeconform_accepts_every_object(profile: str) -> None:
    if shutil.which("kubeconform") is None:
        pytest.skip("kubeconform absent")
    # Strict, and without `-ignore-missing-schemas`: an object kubeconform has no schema for is an error,
    # not a silent skip (the GKE profile's HTTPRoutes are read from the pinned catalogue).
    command = ["kubeconform", "-strict", "-summary", "-schema-location", "default", "-schema-location"]
    done = subprocess.run(
        [*command, CRD_CATALOGUE, "-"],
        input=_rendered(profile),
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "Invalid: 0, Errors: 0, Skipped: 0" in done.stdout, done.stdout


@pytest.mark.parametrize(("profile", "expected"), sorted(PROFILES.items()))
def test_the_api_and_every_worker_are_told_the_profiles_runtime_class(profile: str, expected: str) -> None:
    told = _told(_docs(profile), VARIABLE)
    assert "tranibor-api" in told, "the API judges a policy's runtime against it (/readiness)"
    assert any(name.startswith("tranibor-orchestrator-") for name in told), told
    assert set(told.values()) == {expected}, told


@pytest.mark.parametrize(("profile", "expected"), sorted(PROFILES.items()))
def test_the_warm_pools_runner_and_agent_run_under_the_profiles_runtime_class(
    profile: str, expected: str
) -> None:
    documents = _docs(profile, "--set", "global.runner.executor=agent_sandbox")
    templates = _of(documents, "SandboxTemplate")
    assert templates, "the agent_sandbox executor renders the warm pool's template"
    for name, template in templates.items():
        pod = template["spec"]["podTemplate"]["spec"]
        assert pod.get("runtimeClassName", "") == expected, name
        # The runtime class is the POD's: the runner's container and the agent's own container both run in it.
        assert [c["name"] for c in pod["containers"]] == ["runner"], name
        agent = next(c for c in pod["initContainers"] if c["name"] == "agent")
        assert agent["restartPolicy"] == "Always", "the agent's own container, a native sidecar of the pod"


@pytest.mark.parametrize(("profile", "expected"), sorted(PROFILES.items()))
def test_a_k8s_job_pod_takes_the_runtime_class_the_chart_hands_the_workers(
    profile: str, expected: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the base profile's `k8s_job`, the agent pods are made by code, not by the chart: the worker reads
    the chart's value into its settings and the executor writes it on the Job's pod, around both of them."""
    executor_env = _env(_of(_docs(profile), "Deployment")["tranibor-orchestrator-executor"])
    assert executor_env["TRANIBOR_EXECUTOR_KIND"] == "k8s_job"
    monkeypatch.setenv(VARIABLE, executor_env[VARIABLE])
    settings = OrchestratorSettings(_env_file=None)  # type: ignore[call-arg]
    named = runtime_class(settings, types.SimpleNamespace(sandbox=SandboxPolicy()))
    spec = StageJobSpec(
        run_id="r-1",
        project_slug="demo",
        namespace="tranibor",
        runner_image="example/runner:1",
        api_url="http://tranibor-api:8000",
        run_token="token",
        runtime_class=named,
        agent_isolation=executor_env[ISOLATION],  # type: ignore[arg-type]
        agent_container=AgentContainer(image=f"example/agent@{DIGEST}"),
    )
    pod = KubernetesJobExecutor(client=object())._job(spec, "run-r-1")["spec"]["template"]["spec"]  # type: ignore[arg-type]
    assert pod.get("runtimeClassName", "") == expected
    assert [c["name"] for c in pod["containers"]] == ["runner"]
    assert "agent" in [c["name"] for c in pod["initContainers"]]


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_no_profile_turns_off_the_agents_own_container(profile: str) -> None:
    documents = _docs(profile)
    told = _told(documents, ISOLATION)
    assert "tranibor-api" in told and set(told.values()) == {"container"}, told
    assert not _told(documents, "TRANIBOR_AGENT_ISOLATION"), "no named opt-out (ADR 0222)"
    # Nor can one, by what it sets: a provider profile names a runtime, never the isolation, the executor,
    # the opt-out or the egress gate.
    values = yaml.safe_load((VALUES / f"{profile}.yaml").read_text(encoding="utf-8"))
    assert set(values["global"]) == {"runner"}
    assert set(values["global"]["runner"]) == {"runtimeClass"}
    assert "tranibor-orchestrator" not in values
    assert "enabled" not in values.get("networkPolicies", {})


def _dns_peers(documents: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The `to` of every egress rule of the chart's NetworkPolicies that opens port 53."""
    return [
        rule["to"]
        for policy in _of(documents, "NetworkPolicy").values()
        for rule in policy["spec"].get("egress", [])
        if any(p.get("port") == 53 for p in rule.get("ports", []))
    ]


def test_gke_opens_nodelocal_dnscache_beside_kube_dns() -> None:
    rules = _dns_peers(_docs("gke"))
    assert rules, "the chart's policies open DNS"
    for peers in rules:
        assert {"ipBlock": {"cidr": "169.254.20.10/32"}} in peers, peers
        assert {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}} in peers
    assert all({"ipBlock": {"cidr": "169.254.20.10/32"}} not in peers for peers in _dns_peers(_docs("aks")))


def test_gke_routes_the_api_and_console_on_a_gateway_and_keycloak_through_gke_ingress() -> None:
    documents = _docs("gke")
    assert {"tranibor-api", "tranibor-web"} <= set(_of(documents, "HTTPRoute"))
    ingresses = _of(documents, "Ingress")
    assert set(ingresses) == {"tranibor-keycloak"}, "two Ingresses for one host are two GKE load balancers"
    keycloak = ingresses["tranibor-keycloak"]
    assert keycloak["metadata"]["annotations"]["kubernetes.io/ingress.class"] == "gce"
    assert "ingressClassName" not in keycloak["spec"], "GKE's controller reads the annotation alone"


def test_eks_routes_through_one_alb_and_names_an_ebs_class_for_every_volume() -> None:
    documents = _docs("eks")
    ingresses = _of(documents, "Ingress")
    assert set(ingresses) == {"tranibor-api", "tranibor-web", "tranibor-keycloak"}
    for name, ingress in ingresses.items():
        assert ingress["spec"]["ingressClassName"] == "alb", name
        annotations = ingress["metadata"]["annotations"]
        assert annotations["alb.ingress.kubernetes.io/target-type"] == "ip", name
        assert annotations["alb.ingress.kubernetes.io/group.name"] == "tranibor", name
    claims = [d["spec"] for d in documents if d["kind"] == "PersistentVolumeClaim"]
    claims += [
        t["spec"]
        for d in documents
        if d["kind"] == "StatefulSet"
        for t in d["spec"].get("volumeClaimTemplates", [])
    ]
    assert claims and {c.get("storageClassName") for c in claims} == {"gp2"}, claims


def test_aks_routes_through_the_application_routing_class() -> None:
    ingresses = _of(_docs("aks"), "Ingress")
    assert set(ingresses) == {"tranibor-api", "tranibor-web", "tranibor-keycloak"}
    assert {i["spec"]["ingressClassName"] for i in ingresses.values()} == {
        "webapprouting.kubernetes.azure.com"
    }
