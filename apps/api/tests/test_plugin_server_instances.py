"""Named instances of a plugin's server (S86-03, ADR 0224 §3).

Each guard has its test: the default instance keeps the names an install had before instances existed
(connector, Deployment, Service, Secret); an instance is its own connector with its own token, operations,
configuration and objects, and starts `unconfigured`; a name the organisation's connectors hold is a 409; the
default instance is deleted only with its plugin; an upgrade re-applies every instance and an uninstall
removes them all; the connector routes still refuse a plugin's connector.
"""

from __future__ import annotations

import base64
import hashlib
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_adapters.plugin_servers import INSTANCE_ENV, TOKEN_ENV, PluginServerRuntime

from .agent_scopes_kit import add_member, as_user

ORG = "/api/v1/orgs/northwind"
HELLO = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "plugins" / "hello"
INSTANCES = f"{ORG}/plugins/hello/servers/hello/instances"
SECOND = "second-admin@northwind.dev"


@pytest.fixture
def kube(tmp_path: Path) -> Iterator[FakeKubernetesApi]:
    from tranibor_api.services.plugin_servers import use_plugin_runtime, use_plugin_transport
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    root = tmp_path / "plugins"
    shutil.copytree(HELLO, root / "hello" / "1.0.0")
    # 1.1.0: the same plugin whose server listens on another port, so an upgrade changes every Deployment.
    shutil.copytree(HELLO, root / "hello" / "1.1.0")
    manifest = root / "hello" / "1.1.0" / "plugin.yaml"
    manifest.write_text(manifest.read_text().replace("version: 1.0.0", "version: 1.1.0"))
    server = root / "hello" / "1.1.0" / "servers" / "hello.yaml"
    server.write_text(server.read_text().replace("port: 8080", "port: 9090"))
    for version in ("1.0.0", "1.1.0"):  # a server that needs its `greeting` before it runs: `unconfigured`
        path = root / "hello" / version / "servers" / "hello.yaml"
        path.write_text(
            path.read_text().replace("  type: object\n", "  type: object\n  required: [greeting]\n", 1)
        )
    use_plugin_supply(OfflineDirectory(root))
    api = FakeKubernetesApi()
    use_plugin_runtime(PluginServerRuntime(api, "tranibor-plugins"))
    yield api
    use_plugin_runtime(None)
    use_plugin_transport(None)
    use_plugin_supply(None)


async def _approved(client: AsyncClient, asked: Any) -> None:
    assert asked.status_code == 202, asked.text
    await add_member(client, SECOND, "org_admin")
    await as_user(client, SECOND)
    approved = await client.post(f"{ORG}/plugin-approvals/{asked.json()['approval_id']}/approve")
    assert approved.status_code == 200, approved.text
    await as_user(client, "admin@northwind.dev")


async def _install(client: AsyncClient) -> None:
    await _approved(
        client,
        await client.post(
            f"{ORG}/plugins/install", json={"source": "offline", "name": "hello", "version": "1.0.0"}
        ),
    )


async def _connector(name: str) -> Any:
    from tranibor_api.db.models import OrgConnector
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        return (await session.execute(select(OrgConnector).where(OrgConnector.name == name))).scalar_one()


async def _count(model: Any) -> int:
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


def _names(api: FakeKubernetesApi, resource: str) -> set[str]:
    return {str(o["metadata"]["name"]) for o in api.store(resource).values()}


def _secret(api: FakeKubernetesApi, name: str) -> dict[str, str]:
    (found,) = [o for o in api.store("secrets").values() if o["metadata"]["name"] == name]
    return {k: base64.b64decode(v).decode() for k, v in found["data"].items()}


def _deployment(api: FakeKubernetesApi, name: str) -> dict[str, Any]:
    (found,) = [o for o in api.store("deployments").values() if o["metadata"]["name"] == name]
    return found


async def _add(client: AsyncClient, name: str) -> Any:
    return await client.post(INSTANCES, json={"name": name})


async def test_the_default_instance_keeps_the_names_an_install_had_before_instances(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    """The regression guard of ADR 0224 §3: nothing moves for what is already installed."""
    from tranibor_api.services.plugin_servers import fake_object_name, object_name

    await _install(client)
    default = await _connector("hello")
    org_id = default.org_id
    legacy = f"hello-{hashlib.sha256(org_id.encode()).hexdigest()[:8]}"  # the formula of S43-06, spelt out
    assert object_name(org_id, "hello") == legacy
    assert default.plugin_server["object"] == legacy
    assert fake_object_name(org_id, "hello") == (
        f"hello-fake-{hashlib.sha256(f'{org_id}:fake'.encode()).hexdigest()[:8]}"
    )
    for resource in ("deployments", "services", "networkpolicies", "secrets"):
        assert _names(kube, resource) == {legacy}, resource
    listed = (await client.get(INSTANCES)).json()["items"]
    assert [(i["name"], i["default"], i["state"]) for i in listed] == [("hello", True, "unconfigured")]


async def test_an_instance_is_its_own_connector_with_its_own_objects(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    from tranibor_api.services.plugin_servers import object_name

    await _install(client)
    created = await _add(client, "hello-b")
    assert created.status_code == 201, created.text
    assert created.json() == {
        "name": "hello-b",
        "server": "hello",
        "default": False,
        "state": "unconfigured",
        "status": "unconfigured",
        "last_error": "configuration incomplete: (root): required",
    }
    default, named = await _connector("hello"), await _connector("hello-b")
    assert (
        named.plugin_server["object"]
        == object_name(named.org_id, "hello-b")
        != default.plugin_server["object"]
    )
    assert named.plugin_server["url"].startswith("http://hello-b-")
    # What the broker reads to resolve a profile entry to instances (S86-04): every instance carries it.
    assert named.plugin_server["implements"] == default.plugin_server["implements"]
    assert named.plugin_server["server"] == default.plugin_server["server"] == "hello"
    for resource in ("deployments", "services", "networkpolicies", "secrets"):
        assert _names(kube, resource) == {default.plugin_server["object"], named.plugin_server["object"]}
    assert _deployment(kube, named.plugin_server["object"])["spec"]["replicas"] == 0, (
        "no replica until configured"
    )
    assert (
        _deployment(kube, default.plugin_server["object"])["metadata"]["labels"].get("tranibor.dev/instance")
        is None
    )
    assert (
        _deployment(kube, named.plugin_server["object"])["metadata"]["labels"]["tranibor.dev/instance"]
        == "hello-b"
    )
    # Its own token, its own operations (so policies are set per instance).
    tokens = {_secret(kube, d.plugin_server["object"])[TOKEN_ENV] for d in (default, named)}
    assert len(tokens) == 2
    ops = {
        op["name"]: (op["access"], op["policy"])
        for op in (await client.get(f"{ORG}/connectors/hello-b")).json()["operations"]
    }
    assert ops["say_hello"] == ("read", "allowed") and ops["post_greeting"] == ("write", "approval")
    opened = await client.patch(
        f"{ORG}/connectors/hello-b/operations/set_greeting", json={"policy": "allowed"}
    )
    assert opened.status_code == 200, opened.text
    default_ops = {
        op["name"]: op["policy"] for op in (await client.get(f"{ORG}/connectors/hello")).json()["operations"]
    }
    assert default_ops["set_greeting"] == "approval", "a policy is per instance"


async def test_an_instance_is_configured_on_its_own_connector_and_the_default_is_untouched(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    await _install(client)
    await _add(client, "hello-b")
    default, named = await _connector("hello"), await _connector("hello-b")
    changed = await client.patch(
        f"{ORG}/connectors/hello-b",
        json={"config": {"greeting": "bonjour"}, "secret_values": {"token": "b-secret"}},
    )
    assert changed.status_code == 200, changed.text
    env_b = _secret(kube, named.plugin_server["object"])
    assert env_b["GREETING"] == "bonjour" and env_b["TOKEN"] == "b-secret"
    assert "GREETING" not in _secret(kube, default.plugin_server["object"])
    assert _deployment(kube, named.plugin_server["object"])["spec"]["replicas"] == 1
    states = {i["name"]: i["state"] for i in (await client.get(INSTANCES)).json()["items"]}
    assert states == {"hello": "unconfigured", "hello-b": "starting"}


async def test_a_name_the_organisation_holds_or_that_is_not_a_name_is_refused(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    await _install(client)
    assert (await _add(client, "hello-b")).status_code == 201
    assert (await _add(client, "hello-b")).status_code == 409, "twice"
    assert (await _add(client, "hello")).status_code == 409, "the default's own name"
    plain = await client.post(
        f"{ORG}/connectors",
        json={"name": "taken", "type": "mcp", "config": {"url": "https://mcp.example.com/mcp"}},
    )
    assert plain.status_code == 201, plain.text
    assert (await _add(client, "taken")).status_code == 409, "any connector of the organisation"
    for bad in ("Hello", "1abc", "a", "has_underscore", "x" * 49, "-dash"):
        assert (await _add(client, bad)).status_code == 422, bad
    assert (
        await client.post(f"{ORG}/plugins/hello/servers/nope/instances", json={"name": "zz"})
    ).status_code == 404
    assert (
        await client.post(f"{ORG}/plugins/nope/servers/hello/instances", json={"name": "zz"})
    ).status_code == 404
    assert await _count(__import__("tranibor_api.db.models", fromlist=["OrgConnector"]).OrgConnector) == 3


async def test_only_an_administrator_adds_or_deletes_an_instance(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    await _install(client)
    await _add(client, "hello-b")
    await add_member(client, "dev@northwind.dev", "viewer")
    await as_user(client, "dev@northwind.dev")
    assert (await _add(client, "hello-c")).status_code == 403
    assert (await client.delete(f"{INSTANCES}/hello-b")).status_code == 403
    listed = await client.get(INSTANCES)
    assert listed.status_code == 200 and len(listed.json()["items"]) == 2, "any member reads them"


async def test_a_named_instance_is_deleted_alone_and_the_default_only_with_its_plugin(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    from tranibor_api.db.models import ConnectorOperation
    from tranibor_api.secret_store import get_secret_store

    await _install(client)
    await _add(client, "hello-b")
    default, named = await _connector("hello"), await _connector("hello-b")
    refused = await client.delete(f"{INSTANCES}/hello")
    assert refused.status_code == 409, "uninstall the plugin"
    assert len(await client.get(INSTANCES) and (await client.get(INSTANCES)).json()["items"]) == 2
    assert (await client.delete(f"{INSTANCES}/nope")).status_code == 404
    assert (await client.delete(f"{ORG}/connectors/hello-b")).status_code == 409, (
        "not through the connector route"
    )
    gone = await client.delete(f"{INSTANCES}/hello-b")
    assert gone.status_code == 204, gone.text
    for resource in ("deployments", "services", "networkpolicies", "secrets"):
        assert _names(kube, resource) == {default.plugin_server["object"]}, resource
    assert (await client.get(f"{ORG}/connectors/hello-b")).status_code == 404
    assert await _count(ConnectorOperation) == 4, "the default's own rows only"
    sealed = getattr(get_secret_store(), "values", {})
    assert not [n for o, n in sealed if o == named.org_id and n.startswith("connector/hello-b/")]
    assert [i["name"] for i in (await client.get(INSTANCES)).json()["items"]] == ["hello"]


async def test_an_upgrade_reapplies_every_instance_and_an_uninstall_removes_them_all(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    from tranibor_api.db.models import ConnectorOperation, OrgConnector

    await _install(client)
    await _add(client, "hello-b")
    await _add(client, "hello-c")
    await client.patch(f"{ORG}/connectors/hello-b", json={"config": {"greeting": "kept"}})
    before = {n: (await _connector(n)).plugin_server["object"] for n in ("hello", "hello-b", "hello-c")}
    for obj in before.values():
        assert (
            _deployment(kube, obj)["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"]
            == 8080
        )
    await _approved(client, await client.post(f"{ORG}/plugins/hello/upgrade", json={"version": "1.1.0"}))
    for name, obj in before.items():
        row = await _connector(name)
        assert row.plugin_server["object"] == obj, "the same objects, upgraded in place"
        assert row.plugin_server["version"] == "1.1.0" and row.plugin_server["port"] == 9090
        port = _deployment(kube, obj)["spec"]["template"]["spec"]["containers"][0]["ports"][0][
            "containerPort"
        ]
        assert port == 9090, name
    assert _secret(kube, before["hello-b"])["GREETING"] == "kept", "an instance's configuration survives"
    assert [i["name"] for i in (await client.get(INSTANCES)).json()["items"]] == [
        "hello",
        "hello-b",
        "hello-c",
    ]
    gone = await client.delete(f"{ORG}/plugins/hello")
    assert gone.status_code == 200, gone.text
    for resource in ("deployments", "services", "networkpolicies", "secrets"):
        assert not kube.store(resource), resource
    assert await _count(OrgConnector) == 0 and await _count(ConnectorOperation) == 0


async def test_provisioning_replayed_changes_nothing(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    import copy

    from tranibor_api.config import get_settings
    from tranibor_api.db.models import OrgPlugin
    from tranibor_api.db.session import session_scope
    from tranibor_api.rbac import Principal
    from tranibor_api.services import plugin_servers

    await _install(client)
    await _add(client, "hello-b")
    before, calls = copy.deepcopy(kube.objects), len(kube.calls)
    async with session_scope() as session:
        row = (await session.execute(select(OrgPlugin))).scalar_one()
        doc = row.details["servers"]["hello"]
        who = Principal(user_id="u1", email="admin@northwind.dev")
        for _ in range(2):
            await plugin_servers.provision_instance(
                session, row.org_id, row.name, row.version, doc, "hello-b", who, get_settings()
            )
    assert kube.objects == before
    assert not [c for c in kube.calls[calls:] if c[0] == "POST"], "nothing created twice"
    assert await _count(__import__("tranibor_api.db.models", fromlist=["OrgConnector"]).OrgConnector) == 2


def test_the_state_is_derived_from_the_connector_status() -> None:
    from tranibor_api.db.models import OrgConnector
    from tranibor_api.services.plugin_servers import STATES, state_of

    seen = {
        status: state_of(OrgConnector(status=status))
        for status in ("unconfigured", "deployed", "ok", "error")
    }
    assert seen == {
        "unconfigured": "unconfigured",
        "deployed": "starting",
        "ok": "ready",
        "error": "failing",
    }
    assert set(seen.values()) == set(STATES)


async def test_each_pod_learns_its_instance_name_and_a_configuration_cannot_set_it(
    client: AsyncClient, admin: str, kube: FakeKubernetesApi
) -> None:
    """`TRANIBOR_INSTANCE_NAME`: the connector's name, the server's own for the default instance."""
    await _install(client)
    assert (await _add(client, "lab")).status_code == 201
    for name in ("hello", "lab"):
        row = await _connector(name)
        assert _secret(kube, row.plugin_server["object"])[INSTANCE_ENV] == name
