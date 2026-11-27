"""A project namespace's policies open the cluster's resolver the deployment names (#2277).

Provisioning writes two policies that open DNS: the runners' `allow-platform-egress` and the egress
proxy's `tranibor-egress-out`. Both hard-coded `kube-system` UDP 53; they now follow the chart's
`networkPolicies.dns`, read by the workers as `TRANIBOR_DNS_POLICY`. Unset, they render what they always
rendered.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import yaml
from tranibor_contracts import ProjectConfig
from tranibor_contracts.delivery import ProjectDelivery, RepoConfig
from tranibor_core import load_preset
from tranibor_core.dns_policy import UPSTREAM, DnsPolicy
from tranibor_orchestrator.gitops import render_project_manifests

OPENSHIFT_JSON = json.dumps(
    {
        "namespace": "openshift-dns",
        "podLabels": {},
        "cidrs": ["169.254.20.10/32"],
        "ports": [{"port": 5353, "protocol": "UDP"}, {"port": 5353, "protocol": "TCP"}],
    }
)
#: The two policies of a project namespace that open DNS (the proxy's needs a domain to allow).
DNS_POLICIES = ("allow-platform-egress", "tranibor-egress-out")


def _config() -> ProjectConfig:
    return ProjectConfig(
        slug="billing",
        org="northwind",
        delivery=ProjectDelivery(repo=RepoConfig(url="https://github.com/northwind/billing.git")),
    )


def _policies(**options: Any) -> dict[str, dict[str, Any]]:
    files = render_project_manifests(
        "billing", _config(), load_preset("team"), org_allow_domains=["api.github.com"], **options
    )
    return {
        doc["metadata"]["name"]: doc
        for text in files.values()
        for doc in yaml.safe_load_all(text)
        if doc and doc.get("kind") == "NetworkPolicy"
    }


def _dns_rule(policy: dict[str, Any]) -> dict[str, Any]:
    [rule] = [r for r in policy["spec"]["egress"] if any(p["port"] in (53, 5353) for p in r["ports"])]
    return rule


def test_unset_both_policies_open_kube_system_udp_53_as_before() -> None:
    policies = _policies()
    for name in DNS_POLICIES:
        assert _dns_rule(policies[name]) == {
            "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
            "ports": [{"protocol": "UDP", "port": 53}],
        }, name
    assert _policies() == _policies(dns=UPSTREAM)


def test_openshift_dns_on_5353_and_a_node_local_cache_come_out_of_the_setting() -> None:
    policies = _policies(dns=DnsPolicy.model_validate_json(OPENSHIFT_JSON))
    for name in DNS_POLICIES:
        assert _dns_rule(policies[name]) == {
            "to": [
                {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}},
                {"ipBlock": {"cidr": "169.254.20.10/32"}},
            ],
            "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}],
        }, name
    left = [name for name, policy in policies.items() if "kube-system" in json.dumps(policy)]
    assert not left, f"policies still opening kube-system: {left}"


def test_the_workers_read_the_setting_the_chart_gives(monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_orchestrator.config import OrchestratorSettings

    assert OrchestratorSettings().dns_policy == UPSTREAM
    monkeypatch.setenv("TRANIBOR_DNS_POLICY", OPENSHIFT_JSON)
    read = OrchestratorSettings().dns_policy
    assert read.namespace == "openshift-dns" and read.cidrs == ("169.254.20.10/32",)
    assert [(p.port, p.protocol) for p in read.ports] == [(5353, "UDP"), (5353, "TCP")]
    monkeypatch.setenv("TRANIBOR_DNS_POLICY", '{"namespace": ""}')
    with pytest.raises(ValueError, match="neither a namespace nor cidrs"):
        OrchestratorSettings()
