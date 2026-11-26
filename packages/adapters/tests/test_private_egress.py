"""Private ranges a plugin server may reach, and the rules that open exactly them (ADR 0224 §6a)."""

from __future__ import annotations

import ipaddress

import pytest
from tranibor_adapters.fakes.resolver import FakeResolver
from tranibor_adapters.plugin_servers import PRIVATE_RANGES, ServerDeployment, server_manifests
from tranibor_adapters.private_egress import (
    admitted,
    check_configuration,
    classify,
    parse_cidrs,
    refuse_unbound,
    system_resolver,
)

IMAGE = "registry.example/tranibor-agents/hello-mcp@sha256:" + "a" * 64


def _server(**changes: object) -> ServerDeployment:
    base: dict[str, object] = {
        "name": "unifi-0123abcd",
        "namespace": "tranibor-plugins",
        "image": IMAGE,
        "port": 8080,
        "health_path": "/healthz",
        "env": {"TRANIBOR_SERVER_TOKEN": "t" * 43},
        "labels": {"app.kubernetes.io/name": "tranibor-plugin-server"},
        "egress": (("controller.lan.example", 8443), ("api.vendor.example", 443)),
        "private_egress": (("controller.lan.example", "10.20.1.5", 8443),),
    }
    base.update(changes)
    return ServerDeployment(**base)  # type: ignore[arg-type]


def _kind(objects: list[dict[str, object]], kind: str) -> dict[str, object]:
    return next(o for o in objects if o["kind"] == kind)


# -- the lists ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cidr",
    [
        "169.254.169.254/32",
        "169.254.0.0/16",
        "fd00:ec2::254/128",
        "fd00:ec2::/32",
        "fe80::/10",
        "127.0.0.0/8",
        "100.64.0.0/10",  # contains 100.100.100.200, Alibaba's metadata: list a narrower range
        "0.0.0.0/0",
        "10.0.0.0/7",  # reaches 11/8: public
        "8.8.8.0/24",  # public: already open
    ],
)
def test_a_range_that_stays_closed_or_is_not_private_is_refused(cidr: str) -> None:
    with pytest.raises(ValueError, match=r"closed whatever|not inside a private"):
        parse_cidrs([cidr], where="privateCidrs")


def test_a_host_bit_is_refused_rather_than_silently_widening() -> None:
    with pytest.raises(ValueError, match="not a network"):
        parse_cidrs(["10.20.1.5/16"], where="privateCidrs")


def test_a_cidr_by_org_must_be_inside_the_list_and_belong_to_one_organisation() -> None:
    with pytest.raises(ValueError, match="not inside"):
        check_configuration(["10.20.0.0/16"], {"northwind": ["10.30.0.0/16"]})
    with pytest.raises(ValueError, match="belongs to one organisation"):
        check_configuration(["10.20.0.0/16"], {"a": ["10.20.0.0/16"], "b": ["10.20.1.0/24"]})
    check_configuration(["10.20.0.0/16"], {"a": ["10.20.0.0/17"], "b": ["10.20.128.0/17"]})


def test_a_multi_organisation_deployment_refuses_a_cidr_no_organisation_owns() -> None:
    with pytest.raises(ValueError, match="not bound to an organisation"):
        refuse_unbound(["10.20.0.0/16", "10.30.0.0/16"], {"northwind": ["10.20.0.0/16"]})
    with pytest.raises(ValueError, match="not bound to an organisation"):
        refuse_unbound(["10.20.0.0/16"], {})
    refuse_unbound(["10.20.0.0/16", "10.30.0.0/16"], {"a": ["10.20.0.0/16"], "b": ["10.30.0.0/16"]})


# -- the addresses -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "kind"),
    [
        ("169.254.169.254", "never"),
        ("fd00:ec2::254", "never"),
        ("fe80::1", "never"),
        ("100.100.100.200", "never"),
        ("127.0.0.1", "never"),
        ("::ffff:169.254.169.254", "never"),
        ("10.20.1.5", "private"),
        ("100.64.3.4", "private"),
        ("fd12:3456::1", "private"),
        ("::ffff:10.20.1.5", "private"),
        ("93.184.216.34", "public"),
    ],
)
def test_classify(address: str, kind: str) -> None:
    assert classify(address) == kind


def test_only_a_private_address_inside_a_listed_range_is_admitted() -> None:
    ranges = parse_cidrs(["10.20.0.0/16", "fd12:3456::/32"], where="t")
    assert admitted("10.20.1.5", ranges) and admitted("fd12:3456::1", ranges)
    assert not admitted("10.30.1.5", ranges)
    assert not admitted("93.184.216.34", ranges), "a public address is open already, not admitted"
    assert not admitted("169.254.169.254", [ipaddress.ip_network("169.254.0.0/16")]), "whatever is listed"


async def test_the_resolver_answers_a_literal_and_nothing_for_an_absent_name() -> None:
    assert await system_resolver("10.20.1.5") == ["10.20.1.5"]
    assert await system_resolver("[fd12::1]") == ["fd12::1"]
    assert await system_resolver("nothing.invalid") == []
    fake = FakeResolver({"Controller.LAN.example": ["10.20.1.5"]})
    assert await fake("controller.lan.example") == ["10.20.1.5"]
    assert await fake("other.example") == []


# -- the rendered policies ---------------------------------------------------------------------------------


def test_the_network_policy_opens_exactly_the_admitted_address_and_port() -> None:
    policy = _kind(server_manifests(_server()), "NetworkPolicy")["spec"]
    dns, internet, private = policy["egress"]  # type: ignore[index]
    assert dns["ports"][0]["port"] == 53
    assert internet["to"] == [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_RANGES)}}]
    assert internet["ports"] == [{"protocol": "TCP", "port": 443}], "the private host's port is not public"
    assert private == {
        "to": [{"ipBlock": {"cidr": "10.20.1.5/32"}}],
        "ports": [{"protocol": "TCP", "port": 8443}],
    }


def test_an_address_in_v6_is_opened_as_a_128() -> None:
    d = _server(private_egress=(("controller.lan.example", "fd12:3456::1", 8443),))
    private = _kind(server_manifests(d), "NetworkPolicy")["spec"]["egress"][-1]  # type: ignore[index]
    assert private["to"] == [{"ipBlock": {"cidr": "fd12:3456::1/128"}}]


def test_a_private_only_server_has_no_public_rule() -> None:
    d = _server(egress=(("controller.lan.example", 8443),))
    rules = _kind(server_manifests(d), "NetworkPolicy")["spec"]["egress"]  # type: ignore[index]
    assert [r["to"][0]["ipBlock"]["cidr"] for r in rules[1:]] == ["10.20.1.5/32"]


def test_cilium_opens_the_address_by_tocidr_and_the_public_hosts_by_name() -> None:
    objects = server_manifests(_server(egress_mode="cilium"))
    assert _kind(objects, "NetworkPolicy")["spec"]["egress"] == []  # type: ignore[index]
    dns, fqdns, cidr = _kind(objects, "CiliumNetworkPolicy")["spec"]["egress"]  # type: ignore[index]
    names = [r["matchName"] for r in dns["toPorts"][0]["rules"]["dns"]]
    assert names == ["api.vendor.example", "controller.lan.example"], "the pod resolves the name"
    assert fqdns["toFQDNs"] == [{"matchName": "api.vendor.example"}]
    assert cidr == {
        "toCIDR": ["10.20.1.5/32"],
        "toPorts": [{"ports": [{"port": "8443", "protocol": "TCP"}]}],
    }


def test_cilium_with_an_address_literal_looks_up_nothing() -> None:
    d = _server(
        egress_mode="cilium",
        egress=(("10.20.1.5", 8443),),
        private_egress=(("10.20.1.5", "10.20.1.5", 8443),),
    )
    rules = _kind(server_manifests(d), "CiliumNetworkPolicy")["spec"]["egress"]  # type: ignore[index]
    assert rules == [
        {"toCIDR": ["10.20.1.5/32"], "toPorts": [{"ports": [{"port": "8443", "protocol": "TCP"}]}]}
    ]


def test_without_a_private_target_the_policies_are_what_they_were() -> None:
    d = _server(egress=(("greet.example.com", 443),), private_egress=())
    policy = _kind(server_manifests(d), "NetworkPolicy")["spec"]
    assert len(policy["egress"]) == 2  # type: ignore[index]
