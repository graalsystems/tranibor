# SPDX-License-Identifier: Apache-2.0
"""`GET /projects/{id}/readiness` on a project born from the `investment-research` plugin's template (S40-06).

The investment research half of `apps/api/tests/test_readiness_endpoint.py`: the template is the plugin's
(S83-07, ADR 0181 O4-3) and its archive declares the plugin's extension, so a project is born of it only
where the extension is installed. Its campaign writes files (the object store's item) and ships its own
workflows (the item that flags a copy made before the template was fixed).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

#: The investment research template is the `investment-research` plugin's (S83-07, ADR 0181 O4-3): the core
#: ships its published archive offline (`plugins/offline/`), and its job steps name the job image.
RESEARCH = "plugin:investment-research/investment-research"

OFFLINE = Path(__file__).resolve().parents[4] / "plugins" / "offline"


@pytest.fixture
def research(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_PLUGINS_OFFLINE", str(OFFLINE))
    monkeypatch.setenv(
        "TRANIBOR_JOB_IMAGES", json.dumps({"investment-research": "registry.example/ir@sha256:" + "a" * 64})
    )
    reset_settings_cache()
    yield
    monkeypatch.delenv("TRANIBOR_PLUGINS_OFFLINE")
    monkeypatch.delenv("TRANIBOR_JOB_IMAGES")
    reset_settings_cache()


async def _research_project(client: AsyncClient, slug: str) -> str:
    """A project born of the plugin's template, the plugin installed from the offline archive."""
    installed = await client.post(
        "/api/v1/orgs/northwind/plugins/install",
        json={"source": "offline", "name": "investment-research", "version": "0.1.2"},
    )
    assert installed.status_code in (201, 409), installed.text
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={
            "slug": slug,
            "name": slug.title(),
            "template_ref": RESEARCH,
            "config": {"slug": slug, "org": "northwind"},
        },
    )
    assert created.status_code == 201, created.text
    return str(created.json()["id"])


def _shipped_campaign() -> str:
    """`campaign.yaml` as the locked archive ships it."""
    from tranibor_core.plugins import opened

    with opened(OFFLINE / "investment-research-0.1.2.tar.gz") as plugin:
        return (plugin.root / "templates/investment-research/workflows/campaign.yaml").read_text(
            encoding="utf-8"
        )


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


async def _store_an_older_copy(project_id: str, name: str, drop: str) -> None:
    """The copy a project made of its template before the template was fixed (#817): the line is gone."""
    from sqlalchemy import select
    from tranibor_api.db.models import WorkflowDef
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        row = (
            await session.execute(
                select(WorkflowDef).where(
                    WorkflowDef.project_id == project_id, WorkflowDef.name == name, WorkflowDef.is_active
                )
            )
        ).scalar_one()
        assert drop in row.yaml
        row.yaml = row.yaml.replace(drop, "")


def _template_item(body: dict[str, Any]) -> dict[str, Any] | None:
    return next((i for i in body["items"] if i["id"] == "workflow-template"), None)


@pytest.mark.usefixtures("research")
async def test_readiness_of_a_project_that_writes_files_lists_the_object_store(
    client: AsyncClient, admin: str
) -> None:
    from tranibor_adapters.fakes import FakeObjectStore
    from tranibor_api.object_store import use_object_store
    from tranibor_api.services import readiness

    store = FakeObjectStore()
    use_object_store(store)
    readiness.forget_object_store_probe()
    try:
        pid = await _research_project(client, "research")
        await _activate(pid)
        item = next(i for i in (await _readiness(client, pid))["items"] if i["id"] == "object-store")
        assert item["state"] == "done"
        store.fail_next("object store unreachable (https://s3.example): refused")
        readiness.forget_object_store_probe()
        item = next(i for i in (await _readiness(client, pid))["items"] if i["id"] == "object-store")
        assert item["state"] == "todo" and "unreachable" in item["detail"]
    finally:
        use_object_store(None)
        readiness.forget_object_store_probe()


@pytest.mark.usefixtures("research")
async def test_a_workflow_copied_before_its_template_was_fixed_is_flagged_until_republished(
    client: AsyncClient, admin: str
) -> None:
    pid = await _research_project(client, "research")
    await _activate(pid)
    fresh = _template_item(await _readiness(client, pid))
    assert fresh is not None and fresh["state"] == "done", fresh
    await _store_an_older_copy(
        pid,
        "campaign",
        "    # stored as that document, not as markdown the job cannot find (S38-13, RESEARCH-2 on dev).\n",
    )
    commented = _template_item(await _readiness(client, pid))
    assert commented is not None and commented["state"] == "done", "a comment is not the definition"

    await _store_an_older_copy(pid, "campaign", "    output_types: { strategies: json }\n")
    item = _template_item(await _readiness(client, pid))
    assert item is not None and item["state"] == "check", item
    assert "`campaign` v1" in item["detail"] and "`./workflows/campaign.yaml`" in item["detail"]
    assert "`product-memo`" not in item["detail"], "the workflow that did not change is not named"

    put = await client.put(
        f"/api/v1/projects/{pid}/workflows/campaign",
        json={"yaml": _shipped_campaign(), "source": "template", "activate": True},
    )
    assert put.status_code == 200, put.text
    republished = _template_item(await _readiness(client, pid))
    assert republished is not None and republished["state"] == "done", republished


@pytest.mark.usefixtures("research")
async def test_a_workflow_published_by_hand_is_not_judged(client: AsyncClient, admin: str) -> None:
    pid = await _research_project(client, "research")
    await _activate(pid)
    mine = _shipped_campaign().replace("    output_types: { strategies: json }\n", "")
    put = await client.put(
        f"/api/v1/projects/{pid}/workflows/campaign",
        json={"yaml": mine, "source": "platform", "activate": True},
    )
    assert put.status_code == 200, put.text
    item = _template_item(await _readiness(client, pid))
    assert item is not None and item["state"] == "done" and "`campaign`" not in item["detail"], item
