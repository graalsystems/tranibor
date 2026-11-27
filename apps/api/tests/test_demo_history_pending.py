"""A demonstration history's waiting requests, its removal, and the metrics it stays out of (ADR 0216).

An imported request is the row a live step writes, so the inbox lists it and an approver decides it through
the usual route. What the decision must not do is the point of these tests: an imported work item has no
workflow, so its decision is recorded and audited and passed to nothing; no interpreter is signalled or
started for it. The removal deletes the rows marked imported and nothing else, behind the import's gate.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from .conftest import login

URL = "/api/v1/projects/{id}/demo-history"
#: The workflow the document's history moves through: the test's own, so that the history holds with or
#: without the software-delivery plugin, whose workflow a project otherwise starts with. `shipped` is
#: terminal, so that a request on the first work item is refused as one on an ended work item.
INVOICES = """
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: invoices
  version: 1
initial: inbox
actors:
  implementer: { type: agent, role: implement }
  verifier: { type: agent, role: verify }
states:
  inbox: { display: Inbox }
  in_progress: { display: In progress }
  shipped: { display: Shipped, terminal: true }
transitions:
  - { id: t-implement, from: inbox, to: in_progress, by: implementer }
  - { id: t-shipped, from: in_progress, to: shipped, by: verifier }
"""
PENDING = {
    "id": "spec-review",
    "kind": "approval",
    "summary": "Approve the specification of the localised reminders?",
    "requested_at": "2026-11-05T09:40:00Z",
}
DOCUMENT: dict[str, Any] = {
    "schema": "choregos/DemoHistory/v1",
    "workflow": "invoices",
    "work_items": [
        {
            "id": "inv-142",
            "title": "Credit notes are not deducted from the invoice total",
            "created_at": "2026-11-02T09:00:00Z",
            "history": [
                {"state": "inbox", "at": "2026-11-02T09:00:00Z"},
                {"state": "shipped", "at": "2026-11-03T08:00:00Z"},
            ],
            "runs": [
                {
                    "id": "implement-1",
                    "stage": "implement",
                    "status": "succeeded",
                    "started_at": "2026-11-02T10:31:00Z",
                    "duration_s": 600,
                    "cost_usd": 0.5,
                }
            ],
            "decisions": [
                {
                    "id": "spec-ok",
                    "kind": "approval",
                    "summary": "Approve the specification?",
                    "at": "2026-11-02T10:25:00Z",
                    "by": "admin@northwind.dev",
                }
            ],
            "findings": [
                {
                    "id": "reset-link",
                    "title": "The invoice download link does not expire",
                    "type": "security",
                    "severity": "medium",
                    "evidence": "src/invoices/links.py:41",
                }
            ],
        },
        {
            "id": "inv-150",
            "title": "Send the overdue invoice reminders in the customer's language",
            "created_at": "2026-11-05T09:00:00Z",
            "history": [
                {"state": "inbox", "at": "2026-11-05T09:00:00Z"},
                {"state": "in_progress", "at": "2026-11-05T09:30:00Z"},
            ],
            "pending": PENDING,
        },
    ],
}


def _document() -> dict[str, Any]:
    return copy.deepcopy(DOCUMENT)


@pytest.fixture
async def demo(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[None]:
    """An instance that opted in: the variable on, outside production; the project holds `invoices`."""
    from tranibor_api.config import reset_settings_cache

    published = await client.put(
        f"/api/v1/projects/{project['id']}/workflows/invoices", json={"yaml": INVOICES}
    )
    assert published.status_code in (200, 201), published.text
    monkeypatch.setenv("TRANIBOR_DEMO_HISTORY_IMPORT", "1")
    reset_settings_cache()
    yield
    reset_settings_cache()


async def _count(model: Any, *where: Any) -> int:
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return int(
            (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )


async def _imported(client: AsyncClient, project: dict[str, Any], document: dict[str, Any]) -> None:
    answer = await client.post(URL.format(id=project["id"]), json=document)
    assert answer.status_code == 200, answer.text


async def _waiting_item(client: AsyncClient, project: dict[str, Any]) -> dict[str, Any]:
    items = (await client.get(f"/api/v1/projects/{project['id']}/work-items")).json()["items"]
    return next(item for item in items if item["tracker_key"] == "demo:inv-150")


async def _member(email: str, role: str) -> None:
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "northwind"))
        ).scalar_one()
        user = User(email=email, display_name=email.split("@", 1)[0])
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=org.id, role=role))


# ───────────────────────── a request that still waits ─────────────────────────


async def test_a_pending_request_is_imported_and_waits_in_the_inbox(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    answer = await client.post(URL.format(id=project["id"]), json=DOCUMENT)

    assert answer.status_code == 200, answer.text
    assert answer.json()["created"] == {
        "work_items": 2,
        "runs": 1,
        "decisions": 1,
        "findings": 1,
        "pending": 1,
    }
    item = await _waiting_item(client, project)
    assert item["imported"] is True and item["state"] == "in_progress"
    request = item["pending_request"]
    assert request["kind"] == "approval" and request["decided_at"] is None
    assert request["payload"]["summary"] == PENDING["summary"] and request["payload"]["imported"] is True
    assert request["requested_at"].startswith("2026-11-05T09:40")
    assert request["due_at"].startswith("2026-11-06T09:40"), "absent: 24 hours after it was asked"

    mine = (await client.get("/api/v1/me/pending")).json()["items"]
    assert [(p["kind"], p["where"]["work_item_key"]) for p in mine] == [("human_step", "demo:inv-150")]

    again = await client.post(URL.format(id=project["id"]), json=_document())
    assert again.json()["created"]["pending"] == 0 and again.json()["unchanged"]["pending"] == 1


async def test_an_approver_decides_it_and_no_workflow_hears_of_it(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import AuditLog, Event, HumanRequest, WorkItem
    from tranibor_api.temporal import get_temporal

    await _imported(client, project, DOCUMENT)
    item = await _waiting_item(client, project)
    await _member("approver@northwind.dev", "approver")
    temporal: Any = get_temporal()
    signals, started = len(temporal.signals), dict(temporal.started)

    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url="http://test") as c:  # type: ignore[attr-defined]
        await login(c, "approver@northwind.dev")
        decided = await c.post(
            f"/api/v1/work-items/{item['id']}/decisions", json={"kind": "approve", "reason": "Looks right"}
        )

    assert decided.status_code == 202, decided.text
    assert decided.json()["decided_by"] == "approver@northwind.dev"
    assert decided.json()["decision"]["approved"] is True
    # The point: nothing was passed to a workflow, and none was started for the imported work item.
    assert len(temporal.signals) == signals, "an imported decision is signalled to no interpreter"
    assert temporal.started == started
    assert await _count(HumanRequest, HumanRequest.decided_at.is_(None)) == 0
    assert await _count(WorkItem, WorkItem.temporal_wf_id.is_not(None)) == 0
    assert (await _waiting_item(client, project))["state"] == "in_progress", "nothing follows from it"
    assert (await _waiting_item(client, project))["pending_request"] is None
    assert (
        await _count(
            Event, Event.type == "tranibor.workitem.human_decided", Event.payload["imported"].as_boolean()
        )
        == 1
    )
    assert (
        await _count(
            AuditLog, AuditLog.action == "workitem.decision", AuditLog.payload["imported"].as_boolean()
        )
        == 1
    )
    assert (await client.get("/api/v1/me/pending")).json()["items"] == []


async def test_a_live_work_items_decision_is_still_passed_to_its_workflow(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    """The other side of the guard: only the import's own column skips the signal, not a payload."""
    from tranibor_api.db.models import HumanRequest
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.human_requests import new_human_request
    from tranibor_api.temporal import get_temporal

    created = await client.post(
        f"/api/v1/projects/{project['id']}/work-items", json={"title": "Live", "start": False}
    )
    assert created.status_code in (200, 201), created.text
    live = created.json()
    async with session_scope(orgs="*") as session:
        session.add(
            new_human_request(
                work_item_id=live["id"],
                project_id=project["id"],
                transition_id=None,
                kind="approval",
                payload={"summary": "Ship it?", "imported": True},  # a payload says so; the row is live
            )
        )
    temporal: Any = get_temporal()
    signals = len(temporal.signals)

    decided = await client.post(f"/api/v1/work-items/{live['id']}/decisions", json={"kind": "approve"})

    assert decided.status_code == 202, decided.text
    assert [s[1] for s in temporal.signals[signals:]] == ["human_decision"]
    assert await _count(HumanRequest, HumanRequest.decided_at.is_(None)) == 0


@pytest.mark.parametrize("action", ["mark_agent_ready", "pause", "rerun_stage"])
async def test_an_imported_work_item_takes_no_action_so_no_interpreter_starts(
    client: AsyncClient, project: dict[str, Any], demo: None, action: str
) -> None:
    from tranibor_api.db.models import WorkItem
    from tranibor_api.temporal import get_temporal

    await _imported(client, project, DOCUMENT)
    item = await _waiting_item(client, project)
    temporal: Any = get_temporal()
    started, signals = dict(temporal.started), len(temporal.signals)

    answer = await client.post(f"/api/v1/work-items/{item['id']}/actions", json={"action": action})

    assert answer.status_code == 409, answer.text
    assert "imported" in answer.text
    assert temporal.started == started and len(temporal.signals) == signals
    assert await _count(WorkItem, WorkItem.temporal_wf_id.is_not(None)) == 0


@pytest.mark.parametrize(
    ("change", "named"),
    [
        # An ended work item waits for no one.
        (lambda doc: doc["work_items"][0].update(pending=dict(PENDING, id="late")), "terminal"),
        (
            lambda doc: doc["work_items"][1]["pending"].update(requested_at="2026-11-01T00:00:00Z"),
            "before the work item was created",
        ),
        (lambda doc: doc["work_items"][0].update(pending=dict(PENDING, id="spec-ok")), "spec-ok"),
    ],
    ids=["terminal-state", "asked-before-created", "id-of-a-decision"],
)
async def test_a_pending_request_the_work_item_cannot_hold_is_refused_and_nothing_is_written(
    client: AsyncClient, project: dict[str, Any], demo: None, change: Any, named: str
) -> None:
    from tranibor_api.db.models import HumanRequest, WorkItem

    document = _document()
    change(document)
    if named == "spec-ok":  # on a work item that can wait, so that the id is what is refused
        document["work_items"][0]["history"].pop()

    answer = await client.post(URL.format(id=project["id"]), json=document)

    assert answer.status_code == 422, answer.text
    assert named in answer.text
    assert await _count(WorkItem) == 0 and await _count(HumanRequest) == 0


def test_a_work_item_without_a_pending_request_keeps_the_digest_it_had_before() -> None:
    """A document imported before `pending` existed is the same document now: a re-import is not a 409."""
    import hashlib
    import json

    from tranibor_api.services.demo_history import digest_of
    from tranibor_contracts import DemoHistory

    document = DemoHistory.model_validate(DOCUMENT)
    item = document.work_items[0]
    before = item.model_dump(mode="json")
    before.pop("pending")
    for run in before["runs"]:  # nor did its runs carry outputs, evidence or gates (ADR 0216, point 13)
        del run["outputs"], run["evidence"], run["gates"]
    expected = hashlib.sha256(
        json.dumps({"item": before, "workflow": "w", "prices": {}}, sort_keys=True, default=str).encode()
    ).hexdigest()

    assert digest_of(item, "w", {}) == expected
    assert digest_of(document.work_items[1], "w", {}) != digest_of(
        document.work_items[1].model_copy(update={"pending": None}), "w", {}
    )


# ───────────────────────── removing it ─────────────────────────


async def test_removing_the_history_deletes_the_imported_rows_and_nothing_else(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import AuditLog, CostLedger, Event, Finding, HumanRequest, Run, WorkItem
    from tranibor_api.db.session import session_scope

    await _imported(client, project, DOCUMENT)
    live = (
        await client.post(
            f"/api/v1/projects/{project['id']}/work-items", json={"title": "Live", "start": False}
        )
    ).json()
    async with session_scope(orgs="*") as session:
        session.add_all(
            [
                Finding(
                    project_id=project["id"],
                    origin_work_item_id=live["id"],
                    title="A live finding",
                    type="security",
                    severity="low",
                    evidence="here",
                ),
                CostLedger(
                    project_id=project["id"],
                    work_item_id=live["id"],
                    kind="model",
                    model="m",
                    cost_usd=1.25,
                    ts=datetime.now(UTC),
                ),
            ]
        )

    removed = await client.delete(URL.format(id=project["id"]))

    assert removed.status_code == 200, removed.text
    assert removed.json() == {
        "work_items": 2,
        "runs": 1,
        "requests": 2,
        "findings": 1,
        "cost_lines": 1,
        "cost_usd": 0.5,
    }
    assert await _count(WorkItem, WorkItem.imported.is_(True)) == 0
    assert await _count(Run) == 0 and await _count(HumanRequest) == 0
    assert await _count(Event, Event.payload["imported"].as_boolean()) == 0
    # What the import did not write is still there.
    assert await _count(WorkItem) == 1 and await _count(Finding) == 1 and await _count(CostLedger) == 1
    assert await _count(AuditLog, AuditLog.action == "demo_history.delete") == 1

    again = await client.delete(URL.format(id=project["id"]))
    assert again.status_code == 200 and set(again.json().values()) == {0}
    reimported = await client.post(URL.format(id=project["id"]), json=DOCUMENT)
    assert reimported.json()["created"]["work_items"] == 2, "removed, so the same document writes again"


async def test_removing_a_history_whose_request_was_decided_removes_the_decision_too(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import Event, HumanRequest

    await _imported(client, project, DOCUMENT)
    item = await _waiting_item(client, project)
    assert (
        await client.post(f"/api/v1/work-items/{item['id']}/decisions", json={"kind": "approve"})
    ).status_code == 202

    removed = await client.delete(URL.format(id=project["id"]))

    assert removed.json()["requests"] == 2
    assert await _count(HumanRequest) == 0
    assert await _count(Event, Event.work_item_id == item["id"]) == 0


async def test_removing_is_refused_where_the_import_is(
    client: AsyncClient, project: dict[str, Any], demo: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.db.models import WorkItem

    await _imported(client, project, DOCUMENT)

    monkeypatch.setenv("TRANIBOR_DEMO_HISTORY_IMPORT", "0")
    reset_settings_cache()
    off = await client.delete(URL.format(id=project["id"]))
    assert off.status_code == 409 and "TRANIBOR_DEMO_HISTORY_IMPORT" in off.text

    monkeypatch.setenv("TRANIBOR_DEMO_HISTORY_IMPORT", "1")
    monkeypatch.setenv("TRANIBOR_ENV", "prod")
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    reset_settings_cache()
    prod = await client.delete(URL.format(id=project["id"]))
    assert prod.status_code == 409 and "prod" in prod.text

    assert await _count(WorkItem, WorkItem.imported.is_(True)) == 2


async def test_a_project_owner_cannot_remove_it(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import WorkItem

    await _imported(client, project, DOCUMENT)
    await _member("owner@northwind.dev", "project_owner")
    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url="http://test") as c:  # type: ignore[attr-defined]
        await login(c, "owner@northwind.dev")
        answer = await c.delete(URL.format(id=project["id"]))

    assert answer.status_code == 403, answer.text
    assert await _count(WorkItem, WorkItem.imported.is_(True)) == 2


# ───────────────────────── the metrics it stays out of ─────────────────────────


def _recent(agent: str) -> dict[str, Any]:
    """A history dated yesterday, so that it falls inside every metric's window whenever the test runs."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    return {
        "schema": "choregos/DemoHistory/v1",
        "work_items": [
            {
                "id": "recent",
                "title": "Recent",
                "created_at": yesterday.isoformat(),
                "runs": [
                    {
                        "id": "r1",
                        "stage": "implement",
                        "agent": agent,
                        "status": "succeeded",
                        "started_at": (yesterday + timedelta(minutes=5)).isoformat(),
                        "duration_s": 60,
                        "cost_usd": 4.0,
                    }
                ],
            }
        ],
    }


async def test_an_agents_metrics_and_budget_leave_imported_runs_out(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import Agent, Organization
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.agents import spend_since

    async with session_scope(orgs="*") as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "northwind"))
        ).scalar_one()
        session.add(Agent(org_id=org.id, slug="coder", display_name="Coder"))
        org_id = org.id
    await _imported(client, project, _recent("coder"))

    metrics = (await client.get("/api/v1/orgs/northwind/agents/coder/metrics")).json()
    assert (metrics["runs"], metrics["cost_usd"], metrics["by_project"]) == (0, 0.0, [])
    summary = (await client.get("/api/v1/orgs/northwind/agent-metrics")).json()["items"]
    assert [(s["agent"], s["runs"], s["cost_usd"]) for s in summary] == [("coder", 0, 0.0)]
    async with session_scope(orgs="*") as session:
        assert await spend_since(session, org_id, "coder", datetime(2000, 1, 1, tzinfo=UTC)) == 0.0
    # Still listed in the agent's history, marked: hidden from the measures, not from the reader.
    runs = (await client.get("/api/v1/orgs/northwind/agents/coder/runs")).json()["items"]
    assert [(r["stage_role"], r["imported"]) for r in runs] == [("implement", True)]


async def test_dora_counts_nothing_an_import_wrote(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    """DORA reads the recorded deployments and releases, which an import never writes: a work item imported
    as deployed is not a deployment."""
    await _imported(client, project, DOCUMENT)

    answer = await client.get(f"/api/v1/projects/{project['id']}/metrics/dora")
    if answer.status_code == 404:
        pytest.skip("the software-delivery plugin, which serves DORA, is not installed")
    report = answer.json()
    assert report["deployments"] == 0
    assert report["lead_time"]["sample"] == 0 and report["change_failure_rate"]["sample"] == 0
