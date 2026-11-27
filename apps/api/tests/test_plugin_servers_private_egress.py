"""A deployment may let plugin servers reach named private ranges (ADR 0224 §6a, S86-06).

Admission at typing (the organisation that owns the range only, the bounds of ADR 0070 first), the
refusals that stay (every other private address, the metadata and link-local ranges whatever is listed),
the start-up refusal of a multi-organisation deployment with an unbound range, and the NetworkPolicy and
Cilium rules the provision renders: exactly the resolved address and port.
"""

# ruff: noqa: F811 - fixtures imported from test_plugin_servers
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_adapters.fakes.resolver import FakeResolver
from tranibor_adapters.plugin_servers import PRIVATE_RANGES

from .kits.private_egress import dns  # noqa: F401 - fixture
from .test_plugin_servers import ORG, _install, _only, _set, kube, settings_env  # noqa: F401 - fixtures

LAN = "https://controller.lan.example:8443"
LISTED = json.dumps(["10.20.0.0/16"])


@pytest.fixture
def multi_org() -> Iterator[None]:
    from tranibor_api import edition

    edition.declare(edition.ENTERPRISE, features=frozenset({"multi_org"}))
    yield
    edition.reset()


async def _type(client: AsyncClient, base_url: str) -> Any:
    return await client.patch(f"{ORG}/connectors/hello", json={"config": {"base_url": base_url}})


def _rules(kube: FakeKubernetesApi) -> list[dict[str, Any]]:
    return _only(kube, "networkpolicies")["spec"]["egress"]


# -- off by default ----------------------------------------------------------------------------------------


async def test_nothing_listed_a_private_host_stays_refused_at_typing(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi, dns: FakeResolver
) -> None:
    await _install(client)
    refused = await _type(client, LAN)
    assert refused.status_code == 422, refused.text
    assert "10.20.1.5" in refused.text and "lists no range" in refused.text
    ok = await _type(client, "https://api.vendor.example")
    assert ok.status_code == 200, ok.text
    internet = _rules(kube)[1]
    assert internet["to"][0]["ipBlock"]["except"] == list(PRIVATE_RANGES)


# -- single organisation -----------------------------------------------------------------------------------


async def test_a_listed_range_admits_its_host_and_the_policy_opens_that_address_and_port_only(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
) -> None:
    await _install(client)
    _set(settings_env, TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED)
    admitted = await _type(client, LAN)
    assert admitted.status_code == 200, admitted.text
    dns_rule, *rest = _rules(kube)
    assert dns_rule["ports"][0]["port"] == 53
    assert rest == [
        {"to": [{"ipBlock": {"cidr": "10.20.1.5/32"}}], "ports": [{"protocol": "TCP", "port": 8443}]}
    ], "the address and the port, no public rule for this host"
    # Every other private address stays refused.
    other = await _type(client, "https://elsewhere.lan.example")
    assert other.status_code == 422 and "outside the ranges" in other.text


@pytest.mark.parametrize("host", ["metadata.lan.example", "v6metadata.lan.example", "mixed.lan.example"])
async def test_the_metadata_and_link_local_addresses_are_refused_whatever_is_listed(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
    host: str,
) -> None:
    await _install(client)
    _set(settings_env, TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED)
    refused = await _type(client, f"https://{host}")
    assert refused.status_code == 422, refused.text
    assert "closed whatever" in refused.text


async def test_an_address_literal_in_the_range_is_admitted_and_a_literal_metadata_address_never(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
) -> None:
    await _install(client)
    _set(settings_env, TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED)
    assert (await _type(client, "https://10.20.7.7")).status_code == 200
    assert _rules(kube)[1]["to"] == [{"ipBlock": {"cidr": "10.20.7.7/32"}}]
    assert (await _type(client, "http://169.254.169.254/latest")).status_code == 422
    assert (await _type(client, "https://[fd00:ec2::254]")).status_code == 422


async def test_the_organisations_bounds_still_decide_first(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
) -> None:
    await _install(client)
    _set(
        settings_env,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED,
        TRANIBOR_NETWORK_INSTANCE_DENY="controller.lan.example",
    )
    refused = await _type(client, LAN)
    assert refused.status_code == 422, refused.text
    assert "network policy" in refused.text


async def test_cilium_opens_the_address_by_cidr(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
) -> None:
    from tranibor_adapters.plugin_servers import PluginServerRuntime
    from tranibor_api.services.plugin_servers import use_plugin_runtime

    await _install(client)
    use_plugin_runtime(PluginServerRuntime(kube, "tranibor-plugins", cilium=True))
    _set(
        settings_env,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED,
        TRANIBOR_PLUGIN_SERVERS_EGRESS="cilium",
    )
    assert (await _type(client, LAN)).status_code == 200
    rules = _only(kube, "ciliumnetworkpolicies")["spec"]["egress"]
    assert rules[-1] == {
        "toCIDR": ["10.20.1.5/32"],
        "toPorts": [{"ports": [{"port": "8443", "protocol": "TCP"}]}],
    }
    assert not any("toFQDNs" in r for r in rules), "the name is not opened, its address is"


# -- several organisations ---------------------------------------------------------------------------------


async def test_a_range_is_admitted_for_the_organisation_that_owns_it_only(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
    multi_org: None,
) -> None:
    await _install(client)
    _set(
        settings_env,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS_BY_ORG=json.dumps({"contoso": ["10.20.0.0/16"]}),
    )
    refused = await _type(client, LAN)
    assert refused.status_code == 422, "northwind does not own the range: " + refused.text
    _set(
        settings_env,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS_BY_ORG=json.dumps({"northwind": ["10.20.0.0/16"]}),
    )
    assert (await _type(client, LAN)).status_code == 200
    assert _rules(kube)[1]["to"] == [{"ipBlock": {"cidr": "10.20.1.5/32"}}]


async def test_a_former_slug_of_the_organisation_owns_the_range_too(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
    multi_org: None,
) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import OrgConnector
    from tranibor_api.db.models.identity import OrgSlugAlias
    from tranibor_api.db.session import session_scope

    await _install(client)
    async with session_scope() as session:
        org_id = (
            await session.execute(select(OrgConnector.org_id).where(OrgConnector.name == "hello"))
        ).scalar_one()
        session.add(OrgSlugAlias(alias="northwind-old", org_id=org_id))
    _set(
        settings_env,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS_BY_ORG=json.dumps({"northwind-old": ["10.20.0.0/16"]}),
    )
    assert (await _type(client, LAN)).status_code == 200


def test_a_multi_organisation_deployment_does_not_start_with_an_unbound_range(
    settings_env: pytest.MonkeyPatch, multi_org: None
) -> None:
    from tranibor_api.config import get_settings
    from tranibor_api.services.private_egress import refuse_unbound_ranges

    _set(settings_env, TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED)
    with pytest.raises(ValueError, match="not bound to an organisation"):
        refuse_unbound_ranges(get_settings())
    _set(
        settings_env,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED,
        TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS_BY_ORG=json.dumps({"northwind": ["10.20.0.0/16"]}),
    )
    refuse_unbound_ranges(get_settings())


async def test_the_api_start_stops_on_an_unbound_range_and_a_single_organisation_one_does_not(
    settings_env: pytest.MonkeyPatch,
) -> None:
    from tranibor_api import edition
    from tranibor_api.main import create_app, lifespan

    _set(settings_env, TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS=LISTED)
    edition.declare(edition.ENTERPRISE, features=frozenset({"multi_org"}))
    try:
        with pytest.raises(ValueError, match="not bound to an organisation"):
            async with lifespan(create_app()):
                pass
    finally:
        edition.reset()


def test_a_closed_range_or_a_typo_stops_the_settings(settings_env: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError
    from tranibor_api.config import Settings

    for bad in (["169.254.0.0/16"], ["10.20.1.5/16"], ["10.0.0.0/8", "not-a-cidr"], ["fd00:ec2::254/128"]):
        settings_env.setenv("TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS", json.dumps(bad))
        with pytest.raises(ValidationError, match="PRIVATE_CIDRS"):
            Settings()
    settings_env.setenv("TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS", LISTED)
    settings_env.setenv("TRANIBOR_PLUGIN_SERVERS_EGRESS_PRIVATE_CIDRS_BY_ORG", '{"a": ["10.30.0.0/16"]}')
    with pytest.raises(ValidationError, match="not inside"):
        Settings()
