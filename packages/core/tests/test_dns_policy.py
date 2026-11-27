"""The cluster's resolver, one builder for the policies the platform writes (#2277).

The chart's `networkPolicies.dns` reaches the code as `TRANIBOR_DNS_POLICY`; the rule each writer builds
from it is the one the chart's `tranibor.dnsEgress` renders (`tests/charts/test_dns_policy_in_code.py`
compares the two). Unset, each writer's rule is the one it always wrote.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tranibor_core.dns_policy import UPSTREAM, DnsPolicy, cilium_rules, network_policy_rule

KUBE_SYSTEM = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}
OPENSHIFT = DnsPolicy.model_validate(
    {
        "namespace": "openshift-dns",
        "ports": [{"port": 5353, "protocol": "UDP"}, {"port": 5353, "protocol": "TCP"}],
        "cidrs": ["169.254.20.10/32"],
    }
)


def test_unset_each_writer_keeps_the_rule_it_always_wrote() -> None:
    assert network_policy_rule(UPSTREAM, tcp=True) == {
        "to": [KUBE_SYSTEM],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    }
    assert network_policy_rule(UPSTREAM, tcp=False) == {
        "to": [KUBE_SYSTEM],
        "ports": [{"protocol": "UDP", "port": 53}],
    }
    assert cilium_rules(UPSTREAM, ["a.example"]) == [
        {
            "toEndpoints": [
                {"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}
            ],
            "toPorts": [
                {"ports": [{"port": "53", "protocol": "ANY"}], "rules": {"dns": [{"matchName": "a.example"}]}}
            ],
        }
    ]


def test_the_chart_json_is_read_as_the_chart_writes_it() -> None:
    """The ConfigMap's value, keys as the chart has them (`podLabels`)."""
    read = DnsPolicy.model_validate_json(
        '{"cidrs":["169.254.20.10/32"],"namespace":"kube-system","podLabels":{"k8s-app":"kube-dns"},"ports":[]}'
    )
    assert network_policy_rule(read, tcp=True)["to"] == [
        {**KUBE_SYSTEM, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}},
        {"ipBlock": {"cidr": "169.254.20.10/32"}},
    ]


def test_openshift_and_a_node_local_cache_come_out_of_the_setting() -> None:
    for tcp in (True, False):
        assert network_policy_rule(OPENSHIFT, tcp=tcp) == {
            "to": [
                {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}},
                {"ipBlock": {"cidr": "169.254.20.10/32"}},
            ],
            "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}],
        }
    endpoints, cidr = cilium_rules(OPENSHIFT, ["a.example"])
    ports = [{"port": "5353", "protocol": "UDP"}, {"port": "5353", "protocol": "TCP"}]
    assert endpoints["toEndpoints"] == [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "openshift-dns"}}]
    assert cidr["toCIDR"] == ["169.254.20.10/32"]
    for rule in (endpoints, cidr):
        assert rule["toPorts"] == [{"ports": ports, "rules": {"dns": [{"matchName": "a.example"}]}}]


def test_the_cache_alone_opens_no_namespace() -> None:
    alone = DnsPolicy(namespace="", cidrs=("169.254.20.10/32",))
    assert network_policy_rule(alone, tcp=False)["to"] == [{"ipBlock": {"cidr": "169.254.20.10/32"}}]
    assert [list(r) for r in cilium_rules(alone, ["a.example"])] == [["toCIDR", "toPorts"]]


@pytest.mark.parametrize(
    ("value", "said"),
    [
        ({"namespace": ""}, "neither a namespace nor cidrs"),
        ({"namespace": "", "cidrs": ["10.0.0.10/32"], "podLabels": {"a": "b"}}, "podLabels"),
        ({"cidrs": ["kube-dns"]}, "not a CIDR"),
        ({"ports": [{"port": 53}]}, "protocol"),
        ({"ports": [{"port": 53, "protocol": "SCTP"}]}, "protocol"),
        ({"namespaces": "kube-system"}, "namespaces"),
    ],
)
def test_a_setting_that_opens_nothing_or_is_misspelt_is_refused(value: dict[str, object], said: str) -> None:
    with pytest.raises(ValidationError, match=said):
        DnsPolicy.model_validate(value)
