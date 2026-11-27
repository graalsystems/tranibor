# SPDX-License-Identifier: Apache-2.0
"""The cluster's DNS resolver, as the policies the platform writes open it (#2277).

The chart made the resolver a setting (`networkPolicies.dns`, S87-16): OpenShift serves DNS from the
pods of `openshift-dns` on 5353, and GKE's NodeLocal DNSCache answers at a link-local address no
namespace holds. The chart renders the same setting, as JSON, into the API's and the workers'
environment (`TRANIBOR_DNS_POLICY`), so that the policies written by code (a plugin server's, a project
namespace's) open the resolver the chart's own policies open, and not `kube-system:53` whatever the
cluster.

One builder for both writers: the API (`tranibor_adapters.plugin_servers`) and the orchestrator
(`tranibor_orchestrator.gitops`) import this module, so a rule comes out the same from either. Unset,
the setting is the upstream layout, and each writer renders the rule it always rendered.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: The namespace label every namespace carries (Kubernetes 1.22+).
NAMESPACE_LABEL = "kubernetes.io/metadata.name"
#: The upstream resolver: CoreDNS in `kube-system`, whose pods carry this label.
UPSTREAM_NAMESPACE = "kube-system"
UPSTREAM_POD_LABELS = {"k8s-app": "kube-dns"}


class DnsPort(BaseModel):
    """A port the resolver listens on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: int = Field(ge=1, le=65535)
    protocol: Literal["UDP", "TCP"]


class DnsPolicy(BaseModel):
    """`networkPolicies.dns` of the chart, the keys as the chart writes them.

    - `namespace`: the resolver's namespace; empty, only `cidrs` are opened;
    - `podLabels`: the resolver's pods in that namespace, as `matchLabels`; empty, every pod of it;
    - `cidrs`: resolver addresses no namespace holds (a node-local cache, a resolver outside the cluster);
    - `ports`: the resolver's ports, for every policy at once; empty, each writer's own (UDP 53, and TCP 53
      where that policy always opened it).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    namespace: str = UPSTREAM_NAMESPACE
    pod_labels: dict[str, str] = Field(default_factory=dict, alias="podLabels")
    cidrs: tuple[str, ...] = ()
    ports: tuple[DnsPort, ...] = ()

    @field_validator("cidrs")
    @classmethod
    def _cidrs_are_networks(cls, cidrs: tuple[str, ...]) -> tuple[str, ...]:
        for cidr in cidrs:
            try:
                ipaddress.ip_network(cidr, strict=False)
            except ValueError as wrong:
                raise ValueError(f"networkPolicies.dns.cidrs: {cidr!r} is not a CIDR") from wrong
        return cidrs

    @model_validator(mode="after")
    def _opens_something(self) -> DnsPolicy:
        # The chart's own refusals (`tranibor.dnsEgress`), for a value that did not come through it.
        if not self.namespace and not self.cidrs:
            raise ValueError(
                "networkPolicies.dns: neither a namespace nor cidrs: no pod could resolve a name"
            )
        if self.pod_labels and not self.namespace:
            raise ValueError(
                "networkPolicies.dns.podLabels selects the resolver's pods in networkPolicies.dns.namespace, "
                "which is empty"
            )
        return self


#: The upstream layout, what every writer opened before the setting reached the code.
UPSTREAM = DnsPolicy()


def _ports(policy: DnsPolicy, *, tcp: bool) -> list[DnsPort]:
    if policy.ports:
        return list(policy.ports)
    return [DnsPort(protocol="UDP", port=53), *([DnsPort(protocol="TCP", port=53)] if tcp else [])]


def network_policy_rule(policy: DnsPolicy, *, tcp: bool) -> dict[str, Any]:
    """The `egress` rule of a NetworkPolicy that opens the resolver, as the chart's `tranibor.dnsEgress`
    renders it: the namespace (and its resolver pods), then each address. `tcp`: without ports in the
    setting, TCP 53 beside UDP 53 (what that policy always opened)."""
    peers: list[dict[str, Any]] = []
    if policy.namespace:
        peer: dict[str, Any] = {"namespaceSelector": {"matchLabels": {NAMESPACE_LABEL: policy.namespace}}}
        if policy.pod_labels:
            peer["podSelector"] = {"matchLabels": dict(policy.pod_labels)}
        peers.append(peer)
    peers.extend({"ipBlock": {"cidr": cidr}} for cidr in policy.cidrs)
    return {
        "to": peers,
        "ports": [{"protocol": p.protocol, "port": p.port} for p in _ports(policy, tcp=tcp)],
    }


def cilium_rules(policy: DnsPolicy, names: list[str]) -> list[dict[str, Any]]:
    """The `egress` rules of a CiliumNetworkPolicy that let a pod look up `names` and nothing else (an L7
    `dns` rule): towards the resolver's pods, and towards each of its addresses (`toCIDR`).

    The resolver's pods are `podLabels`; without them, in the upstream namespace, CoreDNS's
    (`k8s-app: kube-dns`, what this rule always selected), and elsewhere every pod of the namespace. Without
    ports in the setting, 53 over any protocol."""

    def to_ports() -> list[dict[str, Any]]:
        ports = [{"port": str(p.port), "protocol": p.protocol} for p in policy.ports] or [
            {"port": "53", "protocol": "ANY"}
        ]
        return [{"ports": ports, "rules": {"dns": [{"matchName": host} for host in names]}}]

    rules: list[dict[str, Any]] = []
    if policy.namespace:
        labels = dict(policy.pod_labels) or (
            dict(UPSTREAM_POD_LABELS) if policy.namespace == UPSTREAM_NAMESPACE else {}
        )
        rules.append(
            {
                "toEndpoints": [
                    {"matchLabels": {"k8s:io.kubernetes.pod.namespace": policy.namespace, **labels}}
                ],
                "toPorts": to_ports(),
            }
        )
    if policy.cidrs:
        rules.append({"toCIDR": list(policy.cidrs), "toPorts": to_ports()})
    return rules
