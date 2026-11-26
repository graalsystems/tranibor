"""ADR 0226 (R9b): the names a project's manifests share with the chart and the executors take `tranibor`.

Provisioning writes, in every runners' namespace, the runners' service account, the executor's Role and
its binding to the orchestrator, and the egress proxy. Each name gets its twin beside the former one, for
the window (0.30.x and 0.31.x): a namespace provisioned before the upgrade has only the former names, and
what runs there (a Job, a suspended sandbox, the chart's default `runner.egressProxy`) still names them.
The platform's namespace, which the runners reach and whose orchestrator account the Role is bound to,
is the deployment's (`TRANIBOR_PLATFORM_NAMESPACE`), no longer a constant.
"""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from tranibor_contracts import ProjectConfig
from tranibor_contracts.delivery import ProjectDelivery, RepoConfig
from tranibor_core import load_preset
from tranibor_orchestrator.gitops import (
    EGRESS_NAME,
    EGRESS_PORT,
    FORMER_EGRESS_NAME,
    PLATFORM_NAMESPACE,
    render_project_manifests,
)

RUNNERS = "proj-billing-runners"


def _config() -> ProjectConfig:
    return ProjectConfig(
        slug="billing",
        org="northwind",
        delivery=ProjectDelivery(repo=RepoConfig(url="https://github.com/northwind/billing.git")),
    )


def _objects(**options: Any) -> dict[tuple[str, str], dict[str, Any]]:
    files = render_project_manifests("billing", _config(), load_preset("team"), **options)
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for text in files.values()
        for doc in yaml.safe_load_all(text)
        if doc and "kind" in doc and "metadata" in doc
    }


@pytest.mark.parametrize("account", ["tranibor-runner", "choregos-runner"])
def test_both_runner_accounts_are_provisioned_without_a_token(account: str) -> None:
    """The executors name `tranibor-runner` where the namespace has it, else the former one."""
    sa = _objects()[("ServiceAccount", account)]
    assert sa["metadata"]["namespace"] == RUNNERS
    assert sa["automountServiceAccountToken"] is False


def test_the_executor_may_ask_which_runner_account_the_namespace_has() -> None:
    role = _objects()[("Role", "choregos-executor")]
    rules = [r for r in role["rules"] if "serviceaccounts" in r["resources"]]
    assert rules == [{"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["get"]}], (
        "read only, and only service accounts"
    )


@pytest.mark.parametrize("namespace", [PLATFORM_NAMESPACE, "tranibor"])
def test_the_role_is_bound_to_both_orchestrator_accounts_of_the_platform_namespace(namespace: str) -> None:
    binding = _objects(platform_namespace=namespace)[("RoleBinding", "choregos-executor")]
    assert binding["subjects"] == [
        {"kind": "ServiceAccount", "name": "tranibor-orchestrator", "namespace": namespace},
        {"kind": "ServiceAccount", "name": "choregos-orchestrator", "namespace": namespace},
    ]


def test_the_runners_reach_the_api_in_the_platform_namespace_the_deployment_names() -> None:
    def api_namespaces(objects: dict[tuple[str, str], dict[str, Any]]) -> set[str]:
        policy = objects[("NetworkPolicy", "allow-platform-egress")]
        return {
            to["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"]
            for rule in policy["spec"]["egress"]
            if {"protocol": "TCP", "port": 8000} in rule.get("ports", [])
            for to in rule["to"]
            if "namespaceSelector" in to
        }

    assert api_namespaces(_objects()) == {PLATFORM_NAMESPACE}
    assert api_namespaces(_objects(platform_namespace="tranibor")) == {"tranibor"}


def test_the_proxy_answers_under_both_names() -> None:
    objects = _objects()
    deployment = objects[("Deployment", EGRESS_NAME)]
    assert ("Deployment", FORMER_EGRESS_NAME) not in objects
    pods = deployment["spec"]["template"]["metadata"]["labels"]
    for name in (EGRESS_NAME, FORMER_EGRESS_NAME):
        service = objects[("Service", name)]
        assert service["spec"]["selector"].items() <= pods.items(), name
        assert service["spec"]["ports"][0]["port"] == EGRESS_PORT
    # The pods keep the former value of their name for the window, as the chart's do by default.
    assert pods["app.kubernetes.io/name"] == FORMER_EGRESS_NAME


def test_the_runners_reach_the_proxy_under_either_value_of_its_name() -> None:
    policy = _objects()[("NetworkPolicy", "allow-platform-egress")]
    selectors = [
        to["podSelector"]
        for rule in policy["spec"]["egress"]
        if {"protocol": "TCP", "port": EGRESS_PORT} in rule.get("ports", [])
        for to in rule["to"]
        if "podSelector" in to
    ]
    assert selectors == [
        {
            "matchExpressions": [
                {
                    "key": "app.kubernetes.io/name",
                    "operator": "In",
                    "values": [EGRESS_NAME, FORMER_EGRESS_NAME],
                }
            ]
        }
    ]


def test_the_platform_namespace_is_a_setting_the_chart_gives(monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_orchestrator.config import OrchestratorSettings

    assert OrchestratorSettings().platform_namespace == PLATFORM_NAMESPACE
    monkeypatch.setenv("TRANIBOR_PLATFORM_NAMESPACE", "tranibor")
    assert OrchestratorSettings().platform_namespace == "tranibor"
