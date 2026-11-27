"""A plugin server's policies open the cluster's resolver the deployment names (#2277).

The chart's `networkPolicies.dns` reaches the API as `TRANIBOR_DNS_POLICY` and a `ServerDeployment` as
`dns`: on OpenShift the resolver is the pods of `openshift-dns` on 5353, with GKE's NodeLocal DNSCache an
address no namespace holds. Before, a server's policy opened `kube-system:53` whatever the cluster, and
the server could not resolve a name where the chart's own pods could.
"""

from __future__ import annotations

import json
from typing import Any

from tranibor_adapters.plugin_servers import ServerDeployment, server_manifests
from tranibor_core.dns_policy import DnsPolicy

OPENSHIFT = DnsPolicy.model_validate(
    {
        "namespace": "openshift-dns",
        "cidrs": ["169.254.20.10/32"],
        "ports": [{"port": 5353, "protocol": "UDP"}, {"port": 5353, "protocol": "TCP"}],
    }
)


def _server(**changes: Any) -> ServerDeployment:
    base: dict[str, Any] = {
        "name": "hello-0123abcd",
        "namespace": "tranibor-plugins",
        "image": "registry.example/hello-mcp@sha256:" + "a" * 64,
        "port": 8080,
        "health_path": "/healthz",
        "env": {"TRANIBOR_SERVER_TOKEN": "t" * 43},
        "labels": {"app.kubernetes.io/name": "tranibor-plugin-server"},
        "egress": (("greet.example.com", 443),),
    }
    base.update(changes)
    return ServerDeployment(**base)


def _kind(objects: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    return next(o for o in objects if o["kind"] == kind)


def test_the_default_is_the_rule_a_server_always_had() -> None:
    dns = _kind(server_manifests(_server()), "NetworkPolicy")["spec"]["egress"][0]
    assert dns == {
        "to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}}}],
        "ports": [{"protocol": "UDP", "port": 53}, {"protocol": "TCP", "port": 53}],
    }
    cilium = _kind(server_manifests(_server(egress_mode="cilium")), "CiliumNetworkPolicy")
    assert cilium["spec"]["egress"][0]["toEndpoints"] == [
        {"matchLabels": {"k8s:io.kubernetes.pod.namespace": "kube-system", "k8s-app": "kube-dns"}}
    ]
    assert cilium["spec"]["egress"][0]["toPorts"][0]["ports"] == [{"port": "53", "protocol": "ANY"}]


def test_openshift_dns_on_5353_and_a_node_local_cache_come_out_of_the_setting() -> None:
    objects = server_manifests(_server(dns=OPENSHIFT))
    policy = _kind(objects, "NetworkPolicy")
    assert policy["spec"]["egress"][0] == {
        "to": [
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}},
            {"ipBlock": {"cidr": "169.254.20.10/32"}},
        ],
        "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}],
    }
    assert "kube-system" not in json.dumps(policy)


def test_the_cilium_variant_follows_the_setting_too() -> None:
    """The L7 `dns` rule stays narrowed to the server's hosts, towards the resolver the setting names."""
    cilium = _kind(server_manifests(_server(dns=OPENSHIFT, egress_mode="cilium")), "CiliumNetworkPolicy")
    endpoints, cidr, _fqdns = cilium["spec"]["egress"]
    ports = [{"port": "5353", "protocol": "UDP"}, {"port": "5353", "protocol": "TCP"}]
    looked_up = {"dns": [{"matchName": "greet.example.com"}]}
    assert endpoints == {
        "toEndpoints": [{"matchLabels": {"k8s:io.kubernetes.pod.namespace": "openshift-dns"}}],
        "toPorts": [{"ports": ports, "rules": looked_up}],
    }
    assert cidr == {"toCIDR": ["169.254.20.10/32"], "toPorts": [{"ports": ports, "rules": looked_up}]}
    assert "kube-system" not in json.dumps(cilium)
