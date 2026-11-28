# SPDX-License-Identifier: Apache-2.0
"""What the console reads of a profile (S86-05, ADR 0224): the installed plugins' profiles, the servers that
implement each, and the instances the organisation runs of them. Read only, and from what is installed."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_adapters.plugin_servers import PluginServerRuntime

from .agent_scopes_kit import add_member, as_user

ORG = "/api/v1/orgs/northwind"
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "profiles"
SECOND = "second-admin@northwind.dev"
NOTES = "notes/notes@1"


@pytest.fixture
def offline(tmp_path: Path) -> Iterator[FakeKubernetesApi]:
    from tranibor_api.services.plugin_servers import use_plugin_runtime, use_plugin_transport
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    root = tmp_path / "plugins"
    for name in ("notes", "shelf"):
        shutil.copytree(FIXTURES / name, root / name / "1.0.0")
    use_plugin_supply(OfflineDirectory(root))
    api = FakeKubernetesApi()
    use_plugin_runtime(PluginServerRuntime(api, "tranibor-plugins"))
    yield api
    use_plugin_runtime(None)
    use_plugin_transport(None)
    use_plugin_supply(None)


async def _install_both(client: AsyncClient) -> None:
    notes = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "notes", "version": "1.0.0"}
    )
    assert notes.status_code == 201, notes.text
    asked = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "shelf", "version": "1.0.0"}
    )
    assert asked.status_code == 202, asked.text
    await add_member(client, SECOND, "org_admin")
    await as_user(client, SECOND)
    approved = await client.post(f"{ORG}/plugin-approvals/{asked.json()['approval_id']}/approve")
    assert approved.status_code == 200, approved.text
    await as_user(client, "admin@northwind.dev")


async def test_a_profile_lists_its_tools_and_the_servers_that_implement_it(
    client: AsyncClient, admin: str, offline: FakeKubernetesApi
) -> None:
    await _install_both(client)
    listed = await client.get(f"{ORG}/plugin-profiles")
    assert listed.status_code == 200, listed.text
    [profile] = listed.json()["items"]
    assert (profile["profile"], profile["plugin"], profile["name"], profile["version"]) == (
        NOTES,
        "notes",
        "notes",
        1,
    )
    assert [t["name"] for t in profile["tools"]] == ["get_provider", "list_notes", "add_note"]
    assert [t["access"] for t in profile["tools"]] == ["read", "read", "write"]
    [implementation] = profile["implementations"]
    assert (implementation["plugin"], implementation["server"]) == ("shelf", "shelf")
    assert [(i["name"], i["default"]) for i in implementation["instances"]] == [("shelf", True)]


async def test_an_added_instance_is_listed_under_the_profile_with_its_state(
    client: AsyncClient, admin: str, offline: FakeKubernetesApi
) -> None:
    await _install_both(client)
    added = await client.post(f"{ORG}/plugins/shelf/servers/shelf/instances", json={"name": "shelf-archive"})
    assert added.status_code == 201, added.text
    [profile] = (await client.get(f"{ORG}/plugin-profiles")).json()["items"]
    [implementation] = profile["implementations"]
    assert [(i["name"], i["default"]) for i in implementation["instances"]] == [
        ("shelf", True),
        ("shelf-archive", False),
    ]
    assert {i["state"] for i in implementation["instances"]} <= {
        "unconfigured",
        "starting",
        "ready",
        "failing",
    }
    assert (
        implementation["instances"][1]["state"] == "starting"
    )  # nothing required: deployed, not yet answering


async def test_a_profile_without_an_installed_implementation_has_none(
    client: AsyncClient, admin: str, offline: FakeKubernetesApi
) -> None:
    installed = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "notes", "version": "1.0.0"}
    )
    assert installed.status_code == 201, installed.text
    [profile] = (await client.get(f"{ORG}/plugin-profiles")).json()["items"]
    assert profile["implementations"] == []


async def test_an_organisation_with_no_plugin_has_no_profile(
    client: AsyncClient, admin: str, offline: FakeKubernetesApi
) -> None:
    assert (await client.get(f"{ORG}/plugin-profiles")).json() == {"items": []}
