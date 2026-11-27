"""The API writes a plugin server's policies towards the resolver the chart names (#2277).

The chart renders `networkPolicies.dns` into the API's environment (`TRANIBOR_DNS_POLICY`, from the
ConfigMap `tranibor-dns-policy`); the provision of a server reads it. Before, a server's policy opened
`kube-system:53` whatever the cluster: on OpenShift (`openshift-dns`, 5353) or behind a node-local cache,
the server could not resolve the host it calls.
"""

# ruff: noqa: F811 - fixtures imported from the plugin server tests
from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_adapters.fakes.resolver import FakeResolver

from .kits.private_egress import dns  # noqa: F401 - fixture
from .test_plugin_servers import ORG, _install, _only, _set, kube, settings_env  # noqa: F401 - fixtures

OPENSHIFT = json.dumps(
    {
        "namespace": "openshift-dns",
        "podLabels": {},
        "cidrs": ["169.254.20.10/32"],
        "ports": [{"port": 5353, "protocol": "UDP"}, {"port": 5353, "protocol": "TCP"}],
    }
)


def test_the_setting_is_read_and_unset_is_kube_system(settings_env: pytest.MonkeyPatch) -> None:
    from tranibor_api.config import get_settings
    from tranibor_core.dns_policy import UPSTREAM

    _set(settings_env)
    assert get_settings().dns_policy == UPSTREAM
    _set(settings_env, TRANIBOR_DNS_POLICY=OPENSHIFT)
    read = get_settings().dns_policy
    assert (read.namespace, read.cidrs) == ("openshift-dns", ("169.254.20.10/32",))
    _set(settings_env, TRANIBOR_DNS_POLICY='{"namespace": "", "podLabels": {}, "cidrs": [], "ports": []}')
    with pytest.raises(ValueError, match="neither a namespace nor cidrs"):
        get_settings()


async def test_a_server_policy_opens_openshift_dns_and_the_node_local_cache(
    client: AsyncClient,
    admin: str,
    kube: FakeKubernetesApi,
    dns: FakeResolver,
    settings_env: pytest.MonkeyPatch,
) -> None:
    await _install(client)
    _set(settings_env, TRANIBOR_DNS_POLICY=OPENSHIFT)
    typed = await client.patch(
        f"{ORG}/connectors/hello", json={"config": {"base_url": "https://api.vendor.example"}}
    )
    assert typed.status_code == 200, typed.text
    policy = _only(kube, "networkpolicies")
    assert policy["spec"]["egress"][0] == {
        "to": [
            {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "openshift-dns"}}},
            {"ipBlock": {"cidr": "169.254.20.10/32"}},
        ],
        "ports": [{"protocol": "UDP", "port": 5353}, {"protocol": "TCP", "port": 5353}],
    }
    assert "kube-system" not in json.dumps(policy)
