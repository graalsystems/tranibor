# SPDX-License-Identifier: Apache-2.0
"""`GET /projects/{id}/readiness` on a project born from the `github-software-delivery` template (S40-06).

The delivery half of `apps/api/tests/test_readiness_endpoint.py`: the template, its repository (`scm`), its
release train (`cd`) and the GitHub App's installation on the repository's owner are the `software-delivery`
plugin's since round D of ADR 0181, so these items are read with the plugin.
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def _project(client: AsyncClient) -> dict[str, Any]:
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={
            "slug": "dev",
            "name": "Development",
            "template_ref": "github-software-delivery@1.0.0",
            "config": {
                "slug": "dev",
                "org": "northwind",
                "delivery": {
                    "repo": {"url": "https://github.com/northwind/sandbox.git", "default_branch": "main"}
                },
            },
        },
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _activate(project_id: str) -> None:
    from tranibor_api.db.models import Project
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        row = await session.get(Project, project_id)
        assert row is not None
        row.status = "active"


async def _forget_the_tests(project_id: str) -> None:
    """Saving a connector tests it (S40-05): put the cards back to "never tested" to read that state."""
    from sqlalchemy import select
    from tranibor_api.db.models import Connector
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        for row in (
            await session.execute(select(Connector).where(Connector.project_id == project_id))
        ).scalars():
            row.status = "unknown"
            row.last_check_at = None


async def _readiness(client: AsyncClient, project_id: str) -> dict[str, Any]:
    response = await client.get(f"/api/v1/projects/{project_id}/readiness")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _by_state(body: dict[str, Any], state: str) -> set[str]:
    return {i["id"] for i in body["items"] if i["state"] == state}


async def test_a_project_with_only_a_tracker_and_a_repository_is_not_ready(
    client: AsyncClient, admin: str
) -> None:
    project = await _project(client)
    await _activate(project["id"])
    for kind, type_, config in (
        ("tracker", "github-issues", {"repo": "northwind/sandbox"}),
        ("scm", "github", {"repo": "northwind/sandbox"}),
    ):
        put = await client.put(
            f"/api/v1/projects/{project['id']}/connectors/{kind}", json={"type": type_, "config": config}
        )
        assert put.status_code == 200, put.text
    body = await _readiness(client, project["id"])
    assert body["ready"] is False
    assert _by_state(body, "todo") == {"connector-cd", "notifications", "people-release-captain"}, body[
        "items"
    ]
    assert {"connector-tracker", "connector-scm"} <= _by_state(body, "done"), (
        "tested by the save itself (S40-05)"
    )
    assert "connector-runtime" in _by_state(body, "done"), "provided by the deployment"
    assert body["summary"]["todo"] == 3
    assert body["summary"]["done"] + body["summary"]["check"] + body["summary"]["todo"] == len(
        [i for i in body["items"] if i["state"] != "not_needed"]
    )
    assert "repository-checks" in _by_state(body, "check"), "no test command"
    assert all(i["fix"]["by"] for i in body["items"])
    await _forget_the_tests(project["id"])
    forgotten = await _readiness(client, project["id"])
    assert {"connector-tracker", "connector-scm"} <= _by_state(forgotten, "check"), "configured, never tested"


async def test_configuring_cd_and_notify_moves_the_items(client: AsyncClient, admin: str) -> None:
    project = await _project(client)
    await _activate(project["id"])
    pid = project["id"]
    base = f"/api/v1/projects/{pid}"
    await client.put(f"{base}/connectors/cd", json={"type": "demo", "config": {}})
    await client.put(f"{base}/connectors/notify", json={"type": "slack", "config": {}})
    body = await _readiness(client, pid)
    assert "connector-cd" in _by_state(body, "check"), "a demo stand-in is to check, not done"
    assert "connector-cd" not in _by_state(body, "todo")
    assert "notifications" in _by_state(body, "todo"), "a connector but no channel"

    tested = await client.post(f"{base}/connectors/cd/test")
    assert tested.status_code == 200
    config = (await client.get(base)).json()["config"]
    patched = await client.patch(base, json={"config": {**config, "notify": {"slack_channel": "#choregos"}}})
    assert patched.status_code == 200, patched.text
    body = await _readiness(client, pid)
    assert "notifications" not in _by_state(body, "todo")
    members = await client.post(
        "/api/v1/orgs/northwind/members",
        json={"email": "rc@northwind.dev", "role": "approver", "project_slug": "dev"},
    )
    assert members.status_code == 201, members.text
    body = await _readiness(client, pid)
    assert "people-release-captain" in _by_state(body, "done")


async def test_a_github_owner_without_an_installation_is_to_do(
    client: AsyncClient, admin: str, github: Any
) -> None:
    project = await _project(client)
    pid = project["id"]
    github.add_installation(1, "northwind")
    assert (
        await client.put(
            f"/api/v1/projects/{pid}/connectors/scm",
            json={"type": "github", "config": {"repo": "northwind/sandbox"}},
        )
    ).status_code == 200
    body = await _readiness(client, pid)
    assert "github-app-scm" in _by_state(body, "done")
    github.remove_installation(1)
    github.add_installation(2, "someone-else")
    from sqlalchemy import delete
    from tranibor_api.db.models import GitHubInstallation
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        await session.execute(delete(GitHubInstallation))
    body = await _readiness(client, pid)
    assert "github-app-scm" in _by_state(body, "todo")
