"""The policies the code writes open the resolver the chart's own policies open (#2277).

S87-16 made the chart's DNS egress a setting (`networkPolicies.dns`). The policies the platform writes
itself (a plugin server's, by the API; a project namespace's, by the workers' provisioning) hard-coded
`kube-system` UDP/TCP 53. The chart now renders the same setting into a ConfigMap, `tranibor-dns-policy`,
which the API and every worker read as `TRANIBOR_DNS_POLICY`; the code builds its rule from it
(`tranibor_core.dns_policy`).

What is proved here: for each shape of the setting, the value the processes read is the setting the
chart's policies were rendered from, and the code's builder turns it into exactly the rule each of the
chart's policies holds.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tranibor_core.dns_policy import DnsPolicy, network_policy_rule

from .test_cluster_assumptions import DNS_POLICIES, EVERYTHING, OPENSHIFT_DNS, _dns_rules, _documents, _render

CONFIG_MAP = "tranibor-dns-policy"
#: The processes that write a NetworkPolicy: the API (plugin servers) and the workers (provisioning).
WRITERS = {
    "tranibor-api",
    "tranibor-orchestrator-orchestrator",
    "tranibor-orchestrator-executor",
    "tranibor-orchestrator-tracker",
    "tranibor-orchestrator-memory",
}
#: The one chart policy whose default ports are UDP 53 alone (the platform's own pods).
UDP_ONLY = "tranibor-allow-platform"

SHAPES = {
    "default": (),
    "openshift": (
        "networkPolicies.dns.namespace=openshift-dns",
        f"networkPolicies.dns.ports={json.dumps(OPENSHIFT_DNS)}",
    ),
    "node-local cache and resolver pods": (
        'networkPolicies.dns.cidrs=["169.254.20.10/32"]',
        'networkPolicies.dns.podLabels={"k8s-app":"kube-dns"}',
    ),
    "the cache alone": ('networkPolicies.dns.namespace=""', 'networkPolicies.dns.cidrs=["169.254.20.10/32"]'),
}


def _setting(documents: list[dict[str, Any]]) -> str:
    [config_map] = [d for d in documents if d["kind"] == "ConfigMap" and d["metadata"]["name"] == CONFIG_MAP]
    return str(config_map["data"]["policy.json"])


def _readers(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Each Deployment's `TRANIBOR_DNS_POLICY`, where it has one."""
    found: dict[str, Any] = {}
    for doc in documents:
        if doc["kind"] != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            for variable in container.get("env") or []:
                if variable["name"] == "TRANIBOR_DNS_POLICY":
                    found[doc["metadata"]["name"]] = variable
    return found


@pytest.mark.parametrize("shape", list(SHAPES))
def test_the_code_reads_the_setting_and_builds_the_rule_the_chart_renders(shape: str) -> None:
    documents = _documents(*EVERYTHING, *SHAPES[shape])
    readers = _readers(documents)
    assert set(readers) == WRITERS
    for name, variable in readers.items():
        assert variable == {
            "name": "TRANIBOR_DNS_POLICY",
            "valueFrom": {"configMapKeyRef": {"name": CONFIG_MAP, "key": "policy.json"}},
        }, name
    setting = DnsPolicy.model_validate_json(_setting(documents))
    rules = _dns_rules(documents)
    assert set(rules) == DNS_POLICIES
    for name, (rule,) in rules.items():
        assert network_policy_rule(setting, tcp=name != UDP_ONLY) == rule, name


def test_the_setting_reaches_the_code_when_the_chart_writes_no_policy() -> None:
    """The code writes its policies whatever `networkPolicies.enabled` says."""
    documents = _documents("networkPolicies.enabled=false", *SHAPES["openshift"])
    assert not _dns_rules(documents)
    assert DnsPolicy.model_validate_json(_setting(documents)).namespace == "openshift-dns"
    assert set(_readers(documents)) == WRITERS


def test_extra_env_cannot_give_the_code_another_resolver() -> None:
    """One value: a second one through `global.extraEnv` is refused, not silently preferred."""
    refused = _render('global.extraEnv=[{"name":"TRANIBOR_DNS_POLICY","value":"{}"}]')
    assert refused.returncode != 0 and "TRANIBOR_DNS_POLICY" in refused.stderr
