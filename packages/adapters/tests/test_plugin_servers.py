"""The objects that run a plugin's server, and how they are applied (ADR 0096, S43-06)."""

from __future__ import annotations

import base64
import json

import httpx
import pytest
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_adapters.oci import RegistryError
from tranibor_adapters.plugin_servers import (
    CONFIG_DIGEST,
    PRIVATE_RANGES,
    TOKEN_ENV,
    PluginServerClient,
    PluginServerRuntime,
    ServerDeployment,
    check_mirrors,
    mirrored,
    server_manifests,
)

IMAGE = "registry.example/choregos-agents/hello-mcp@sha256:" + "a" * 64


def _server(**changes: object) -> ServerDeployment:
    base: dict[str, object] = {
        "name": "hello-0123abcd",
        "namespace": "choregos-plugins",
        "image": IMAGE,
        "port": 8080,
        "health_path": "/healthz",
        "env": {"TRANIBOR_SERVER_TOKEN": "t" * 43, "BASE_URL": "https://greet.example.com"},
        "labels": {"app.kubernetes.io/name": "choregos-plugin-server"},
        "egress": (("greet.example.com", 443),),
    }
    base.update(changes)
    return ServerDeployment(**base)  # type: ignore[arg-type]


def _kind(objects: list[dict[str, object]], kind: str) -> dict[str, object]:
    return next(o for o in objects if o["kind"] == kind)


def test_default_egress_is_public_addresses_on_the_configured_ports() -> None:
    policy = _kind(server_manifests(_server()), "NetworkPolicy")["spec"]
    dns, internet = policy["egress"]  # type: ignore[index]
    assert dns["ports"][0]["port"] == 53
    assert internet["to"] == [{"ipBlock": {"cidr": "0.0.0.0/0", "except": list(PRIVATE_RANGES)}}]
    assert "169.254.0.0/16" in PRIVATE_RANGES, "never the cloud's metadata endpoint"
    assert internet["ports"] == [{"protocol": "TCP", "port": 443}]


def test_cilium_names_the_exact_hosts_and_the_network_policy_opens_no_egress() -> None:
    """S80-20b: Cilium unions policies, so a plain port-53 allow in the NetworkPolicy would wildcard
    the CiliumNetworkPolicy's L7 `dns` rule and the server would resolve any name. With `cilium` the
    DNS rule is the CNP's alone, narrowed to the configured hosts."""
    objects = server_manifests(_server(egress_mode="cilium"))
    assert _kind(objects, "NetworkPolicy")["spec"]["egress"] == []  # type: ignore[index]
    dns, fqdns = _kind(objects, "CiliumNetworkPolicy")["spec"]["egress"]  # type: ignore[index]
    assert dns["toPorts"][0]["rules"]["dns"] == [{"matchName": "greet.example.com"}]
    assert fqdns["toFQDNs"] == [{"matchName": "greet.example.com"}]


def test_no_host_no_egress_at_all() -> None:
    assert _kind(server_manifests(_server(egress=())), "NetworkPolicy")["spec"]["egress"] == []  # type: ignore[index]


def test_the_secret_gives_the_token_under_both_names_during_the_window() -> None:
    """#2191: core 0.28 renamed the variable it writes into a third party's pod, and every server image
    published before the rename (reading only the former name) refused to start. Until ADR 0176's R4
    window closes, the Secret carries both; the server's own configuration is not doubled."""
    data = _kind(server_manifests(_server()), "Secret")["data"]
    env = {k: base64.b64decode(v).decode() for k, v in data.items()}  # type: ignore[union-attr]
    legacy = "CHOREGOS_" + TOKEN_ENV.removeprefix("TRANIBOR_")
    assert env[TOKEN_ENV] == env[legacy] == "t" * 43
    assert set(env) == {TOKEN_ENV, legacy, "BASE_URL"}


def test_the_configuration_digest_rolls_the_pods() -> None:
    first = _kind(server_manifests(_server()), "Deployment")
    second = _kind(server_manifests(_server(env={"TRANIBOR_SERVER_TOKEN": "x"})), "Deployment")
    digest = [d["spec"]["template"]["metadata"]["annotations"][CONFIG_DIGEST] for d in (first, second)]  # type: ignore[index]
    assert digest[0] != digest[1]


async def test_apply_is_idempotent_and_delete_removes_everything() -> None:
    api = FakeKubernetesApi()
    runtime = PluginServerRuntime(api, "choregos-plugins")
    await runtime.apply(_server())
    created = len(api.calls)
    await runtime.apply(_server())
    assert [c for c in api.calls[created:] if c[0] != "GET"] == [], "replayed: nothing written"
    await runtime.apply(_server(replicas=0))
    assert api.store("deployments")["hello-0123abcd"]["spec"]["replicas"] == 0
    await runtime.delete("hello-0123abcd")
    assert not any(api.store(r) for r in ("deployments", "services", "networkpolicies", "secrets"))


async def test_the_server_client_presents_its_token_and_needs_no_egress_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The platform's call to its own Service is not an egress of the organisation: it passes in
    `enforce` with no policy in context, where any guarded client fails closed."""
    monkeypatch.setenv("TRANIBOR_EGRESS_MODE", "enforce")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        result = {"tools": []} if body["method"] == "tools/list" else {"protocolVersion": "2025-06-18"}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    client = PluginServerClient(
        "http://hello-0123abcd.choregos-plugins.svc:8080/mcp",
        token="secret",
        transport=httpx.MockTransport(handle),
    )
    assert await client.list_tools() == []
    assert seen and all(r.headers["authorization"] == "Bearer secret" for r in seen)


MIRRORS = {
    "registry.example/choregos-agents": "harbor.example/proxy/choregos-agents",
    "registry.example": "harbor.example/everything",
}


def test_a_mirror_moves_the_pull_and_keeps_the_digest() -> None:
    """S80-20: the longest matching prefix, matched by path segment, the rest of the reference kept."""
    assert mirrored(IMAGE, MIRRORS) == "harbor.example/proxy/choregos-agents/hello-mcp@sha256:" + "a" * 64
    other = "registry.example/other/x@sha256:" + "b" * 64
    assert mirrored(other, MIRRORS) == "harbor.example/everything/other/x@sha256:" + "b" * 64
    evil = "registry.example-evil/choregos-agents/x@sha256:" + "c" * 64
    assert mirrored(evil, MIRRORS) == evil, "a prefix is matched by path segment"
    assert mirrored(IMAGE, {}) == IMAGE
    assert mirrored(IMAGE, {"registry.example/choregos-agents/": "harbor.example/p/"}).startswith(
        "harbor.example/p/hello-mcp@sha256:"
    )


@pytest.mark.parametrize(
    "mirrors",
    [{"acme": "harbor.example/acme"}, {"ghcr.io/acme": "harbor.example/acme:1"}, {"ghcr.io/acme": ""}],
    ids=["no-registry", "a-tag", "empty"],
)
def test_a_mirror_that_is_not_a_prefix_is_refused(mirrors: dict[str, str]) -> None:
    with pytest.raises(RegistryError):
        check_mirrors(mirrors)
    check_mirrors(MIRRORS)


def test_the_pull_secrets_reach_the_pod() -> None:
    pod = _kind(server_manifests(_server(image_pull_secrets=("mirror-pull",))), "Deployment")
    spec = pod["spec"]["template"]["spec"]  # type: ignore[index]
    assert spec["imagePullSecrets"] == [{"name": "mirror-pull"}]
    plain = _kind(server_manifests(_server()), "Deployment")["spec"]["template"]["spec"]  # type: ignore[index]
    assert "imagePullSecrets" not in plain
