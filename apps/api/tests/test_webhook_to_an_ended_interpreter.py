"""A webhook event for a work item whose interpreter has ended is kept (ADR 0102).

Temporal answers NOT_FOUND to the signal: the delivery used to fail with a 500 and roll its dedupe row
back, so the same delivery was processed again and again.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from tranibor_adapters.fakes.kubernetes import FakeKubernetesApi
from tranibor_adapters.plugin_servers import PluginServerRuntime

from .test_plugin_tracker import DESK, HOOK, SECRET, _ready


@pytest.fixture
def kube(tmp_path: Path) -> Iterator[FakeKubernetesApi]:
    """The fake plugin tracker's scene (`test_plugin_tracker.kube`), its `desk-flow` template giving its
    projects `default-request`, a workflow of the core's, instead of `default-simple`, the `software-delivery`
    plugin's: what is under test is the tracker's delivery, which runs with or without the plugins."""
    from tranibor_api.services.plugin_servers import use_plugin_runtime, use_plugin_transport
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    root = tmp_path / "plugins"
    shutil.copytree(DESK, root / "desk" / "1.0.0")
    manifest = root / "desk" / "1.0.0" / "templates" / "desk-flow" / "manifest.yaml"
    text = manifest.read_text(encoding="utf-8").replace(
        "template:default-simple@1", "template:default-request@1"
    )
    assert "template:default-request@1" in text
    manifest.write_text(text, encoding="utf-8")
    use_plugin_supply(OfflineDirectory(root))
    api = FakeKubernetesApi()
    use_plugin_runtime(PluginServerRuntime(api, "choregos-plugins"))
    yield api
    use_plugin_runtime(None)
    use_plugin_transport(None)
    use_plugin_supply(None)


REPO = {"full_name": "northwind/billing-api"}


async def _issue(client: AsyncClient, delivery: str, action: str, labels: list[str]) -> Any:
    issue = {"number": 7, "title": "Credit notes fail", "labels": [{"name": n} for n in labels]}
    body: dict[str, Any] = {"action": action, "repository": REPO, "issue": issue, "sender": {"login": "a"}}
    if action == "labeled":
        body["label"] = {"name": labels[-1]}
    return await client.post(
        "/api/v1/webhooks/github",
        content=json.dumps(body).encode(),
        headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery},
    )


async def test_an_event_for_an_ended_interpreter_is_kept(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.temporal import get_temporal, interpreter_id

    opened = await _issue(client, "e0", "opened", ["agent-ready"])
    assert opened.status_code == 202, opened.text
    get_temporal().gone.add(interpreter_id("billing-api", "northwind/billing-api#7"))  # type: ignore[attr-defined]
    first = await _issue(client, "e1", "labeled", ["hotfix"])
    assert first.status_code == 202, first.text
    assert first.json()["duplicate"] is False
    again = await _issue(client, "e1", "labeled", ["hotfix"])
    assert again.status_code == 202 and again.json()["duplicate"] is True


async def test_a_plugin_tracker_event_for_an_ended_interpreter_is_kept(
    client: AsyncClient, admin: str, kube: Any
) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import Project, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.temporal import get_temporal, interpreter_id

    fake = await _ready(client, kube)
    fake.add("INC0000001", "VPN drops")
    async with session_scope() as session:
        project = (await session.execute(select(Project).where(Project.slug == "desked"))).scalar_one()
        session.add(WorkItem(project_id=project.id, tracker_key="INC0000001", title="VPN drops"))
    get_temporal().gone.add(interpreter_id("desked", "INC0000001"))  # type: ignore[attr-defined]
    body = fake.delivery("INC0000001")
    first = await client.post(HOOK, content=body, headers={"X-Choregos-Secret": SECRET})
    assert first.status_code == 202, first.text
    assert first.json() == {"accepted": True, "duplicate": False, "events": 1}
    again = await client.post(HOOK, content=body, headers={"X-Choregos-Secret": SECRET})
    assert again.status_code == 202 and again.json()["duplicate"] is True
