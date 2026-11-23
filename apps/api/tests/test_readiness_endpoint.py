"""`GET /projects/{id}/readiness` (S40-06), on a project the core runs by itself.

The items a plugin's template brings (the repository, the release train, the GitHub App, the object store
of a campaign that writes files, a workflow copied before its template was fixed) are read with that
plugin: `plugins/software-delivery/tests/api/test_delivery_readiness.py` and
`plugins/investment-research/tests/api/test_research_readiness.py`.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from .conftest import login


async def _project(client: AsyncClient) -> dict[str, Any]:
    """A project born of no template and without a repository: `default-request` and the `solo` policy,
    which the core publishes by itself, with or without the plugins' extensions. The readiness of a project
    born of a plugin's template is read with that plugin (`plugins/software-delivery/tests/api/
    test_delivery_readiness.py`, `plugins/investment-research/tests/api/test_research_readiness.py`)."""
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={"slug": "dev", "name": "Development", "config": {"slug": "dev", "org": "northwind"}},
    )
    assert created.status_code == 201, created.text
    assert created.json()["workflow_name"] == "default-request"
    return dict(created.json())


async def _activate(project_id: str) -> None:
    from tranibor_api.db.models import Project
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        row = await session.get(Project, project_id)
        assert row is not None
        row.status = "active"


async def _readiness(client: AsyncClient, project_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/projects/{project_id}/readiness")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _by_state(body: dict[str, Any], state: str) -> set[str]:
    return {i["id"] for i in body["items"] if i["state"] == state}


async def test_a_viewer_reads_it_and_it_is_visible_to_no_more_than_the_project(
    client: AsyncClient, admin: str
) -> None:
    project = await _project(client)
    pid = project["id"]
    added = await client.post(
        "/api/v1/orgs/northwind/members",
        json={"email": "viewer@northwind.dev", "role": "viewer", "project_slug": "dev"},
    )
    assert added.status_code == 201, added.text
    client.cookies.clear()
    await login(client, "viewer@northwind.dev")
    assert (await client.get(f"/api/v1/projects/{pid}/readiness")).status_code == 200, "project:read suffices"
    client.cookies.clear()
    await login(client, "stranger@elsewhere.dev")
    denied = await client.get(f"/api/v1/projects/{pid}/readiness")
    # Whoever the project route hides it from (another organisation) is refused here too: the
    # readiness shows no more than `GET /projects/{id}` does.
    assert denied.status_code == (await client.get(f"/api/v1/projects/{pid}")).status_code
    client.cookies.clear()
    assert (await client.get(f"/api/v1/projects/{pid}/readiness")).status_code == 401


async def test_readiness_says_a_gvisor_policy_the_warm_pool_cannot_run(
    client: AsyncClient, admin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S38-13d / #750: before any run, the item names the policy's runtime, the pool's, and the fix."""
    project = await _project(client)
    await _activate(project["id"])
    policy = (await client.get(f"/api/v1/projects/{project['id']}/policy")).json()["yaml"]
    assert "runtime: default" in policy
    edited = policy.replace("runtime: default", "runtime: gvisor", 1)
    put = await client.put(f"/api/v1/projects/{project['id']}/policy", json={"yaml": edited})
    assert put.status_code == 200, put.text
    monkeypatch.setenv("TRANIBOR_FAKES", "0")
    monkeypatch.setenv("TRANIBOR_EXECUTOR_KIND", "agent_sandbox")
    monkeypatch.setenv("TRANIBOR_RUNNER_RUNTIME_CLASS", "")
    item = next(i for i in (await _readiness(client, project["id"]))["items"] if i["id"] == "runtime-class")
    assert item["state"] == "todo"
    assert "gvisor" in item["detail"] and "the cluster default" in item["detail"]
    assert "runner.runtimeClass" in item["detail"]
    monkeypatch.setenv("TRANIBOR_RUNNER_RUNTIME_CLASS", "gvisor")
    item = next(i for i in (await _readiness(client, project["id"]))["items"] if i["id"] == "runtime-class")
    assert item["state"] == "done"
    monkeypatch.setenv("TRANIBOR_EXECUTOR_KIND", "k8s_job")
    assert "runtime-class" not in {i["id"] for i in (await _readiness(client, project["id"]))["items"]}


async def test_the_probe_of_the_object_store_is_cached_and_reports_its_failure() -> None:
    """A store that fails is named; the answer is reused for a minute; the fake answers."""
    from tranibor_adapters.fakes import FakeObjectStore
    from tranibor_api.object_store import use_object_store
    from tranibor_api.services import readiness

    store = FakeObjectStore()
    use_object_store(store)
    readiness.forget_object_store_probe()
    try:
        store.fail_next("object store unreachable (https://s3.example): refused")
        failed = await readiness.probe_object_store()
        assert failed.error is not None and "unreachable" in failed.error
        assert (await readiness.probe_object_store()) is failed, "cached"
        readiness.forget_object_store_probe()
        assert (await readiness.probe_object_store()).error is None
        assert store.puts == 0 and not store.objects, "the probe creates nothing"
    finally:
        use_object_store(None)
        readiness.forget_object_store_probe()


def _template_item(body: dict[str, Any]) -> dict[str, Any] | None:
    return next((i for i in body["items"] if i["id"] == "workflow-template"), None)


async def test_a_plugin_server_is_not_a_direct_connector(client: AsyncClient, admin: str) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import Organization, OrgConnector
    from tranibor_api.db.session import session_scope
    from tranibor_api.secret_store import FakeSecretStore, set_secret_store

    set_secret_store(FakeSecretStore())
    try:
        project = await _project(client)
        put = await client.put(
            "/api/v1/orgs/northwind/mcp-gateway",
            json={
                "mode": "required",
                "endpoint": "https://gw.example.test/{server}/mcp",
                "routing": "path",
                "auth": {"type": "jwt_assertion"},
                "audience": "gw",
            },
        )
        assert put.status_code == 200, put.text
        async with session_scope(orgs="*") as session:
            org = (
                await session.execute(select(Organization).where(Organization.slug == "northwind"))
            ).scalar_one()
            session.add(OrgConnector(org_id=org.id, name="crm", kind="mcp", type="mcp", config={}))
            session.add(
                OrgConnector(
                    org_id=org.id,
                    name="desk",
                    kind="mcp",
                    type="mcp",
                    config={},
                    plugin_server={"plugin": "desk", "server": "desk"},
                )
            )
        body = await _readiness(client, project["id"])
    finally:
        set_secret_store(None)
    [item] = [i for i in body["items"] if i["id"] == "mcp_gateway"]
    assert item["state"] == "todo"
    assert "crm" in item["detail"]
    assert "desk" not in item["detail"]


async def test_a_project_with_no_template_is_not_judged_on_one(client: AsyncClient, admin: str) -> None:
    bare = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={"slug": "bare", "name": "Bare", "config": {"slug": "bare", "org": "northwind"}},
    )
    assert bare.status_code == 201, bare.text
    await _activate(bare.json()["id"])
    assert _template_item(await _readiness(client, bare.json()["id"])) is None
