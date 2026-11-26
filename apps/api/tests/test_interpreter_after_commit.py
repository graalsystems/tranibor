"""A new work item's interpreter starts once its row is committed, and a lost start is made again (#2291).

`create_work_item` started the interpreter inside the creating transaction: on PostgreSQL the row was not
yet visible to another session when Temporal had the start, so the worker's first activity could read
"unknown work item" (118 of them in the load test, `docs/load-testing.md`), and a commit that failed
after the start left an interpreter on nothing. ADR 0085: what leaves for another system leaves after the
commit (`after_commit`), and a relay makes again a start the commit's callback lost.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient


async def _visible(work_item_id: str) -> bool:
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as other:
        return await other.get(WorkItem, work_item_id) is not None


async def test_the_interpreter_starts_once_the_row_is_visible_to_another_session(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """At the moment Temporal is asked, another session (the worker's `load_context`) finds the row."""
    from tranibor_api.temporal import get_temporal

    fake = get_temporal()
    real = fake.start_interpreter
    seen: list[bool] = []

    async def watching(workflow_id: str, payload: dict[str, Any]) -> str:
        seen.append(await _visible(payload["work_item_id"]))
        return await real(workflow_id, payload)

    monkeypatch.setattr(fake, "start_interpreter", watching)
    created = await client.post(f"/api/v1/projects/{project['id']}/work-items", json={"title": "Seen"})

    assert created.status_code == 201, created.text
    assert seen == [True], "the interpreter was asked for before its work item was committed"
    assert created.json()["temporal_wf_id"], "the interpreter's id is written with the work item"


async def test_a_creation_whose_transaction_rolls_back_starts_nothing(project: dict[str, Any]) -> None:
    from tranibor_api.db.models import Project
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.routing import WorkItemBirth, new_work_item
    from tranibor_api.services.tickets import start_work_item
    from tranibor_api.temporal import get_temporal

    before = dict(get_temporal().started)  # type: ignore[attr-defined]

    class RefusedError(Exception):
        pass

    with pytest.raises(RefusedError):
        async with session_scope(orgs="*") as session:
            row = await session.get(Project, project["id"])
            assert row is not None
            item = await new_work_item(
                session, row, WorkItemBirth(tracker_key="BILLING-API-90", title="Lost")
            )
            await start_work_item(row, item, session)
            assert get_temporal().started == before, "started before the commit"  # type: ignore[attr-defined]
            raise RefusedError

    assert get_temporal().started == before, "an interpreter was started on a rolled-back work item"  # type: ignore[attr-defined]


async def test_the_relay_starts_an_interpreter_whose_start_was_lost(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Temporal refuses the start that follows the commit: the work item is created (201), and the relay
    starts its interpreter; a work item created without one, or closed, is left alone."""
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.work_item_relay import start_unstarted_work_items
    from tranibor_api.temporal import get_temporal
    from tranibor_core import utcnow

    fake = get_temporal()
    real = fake.start_interpreter

    async def down(workflow_id: str, payload: dict[str, Any]) -> str:
        raise ConnectionError("temporal is down")

    base = f"/api/v1/projects/{project['id']}/work-items"
    monkeypatch.setattr(fake, "start_interpreter", down)
    lost = await client.post(base, json={"title": "Lost start"})
    closed = await client.post(base, json={"title": "Closed since"})
    monkeypatch.setattr(fake, "start_interpreter", real)
    unstarted = await client.post(base, json={"title": "Not started on purpose", "start": False})
    for response in (lost, closed, unstarted):
        assert response.status_code == 201, response.text
    lost_id, closed_id = lost.json()["id"], closed.json()["id"]
    async with session_scope(orgs="*") as session:
        row = await session.get(WorkItem, closed_id)
        assert row is not None
        row.closed_at = utcnow()
    started_ids = {p["work_item_id"] for p in fake.started.values()}  # type: ignore[attr-defined]
    assert lost_id not in started_ids, "the start was supposed to be lost"

    assert await start_unstarted_work_items(grace=timedelta(hours=1)) == [], "too young: left to its commit"
    assert await start_unstarted_work_items(grace=timedelta(0)) == [lost_id]
    assert lost_id in {p["work_item_id"] for p in fake.started.values()}  # type: ignore[attr-defined]
    assert await start_unstarted_work_items(grace=timedelta(0), window=timedelta(0)) == [], (
        "out of the window"
    )
