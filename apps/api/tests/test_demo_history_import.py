"""A demonstration history taken in through the API (ADR 0216).

The import writes a past that nothing measured, so what these tests hold is the opposite of what a live
test holds: the rows exist and look lived in, and every one of them says it is imported. The gate is the
other half: off unless asked, never in production, the organisation administrator only.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from .conftest import login
from .kits.demo_history import DOCUMENT, INVOICES, URL, _document, demo_history_opted_in  # noqa: F401 - the fixture `demo`


async def _count(model: Any, *where: Any) -> int:
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return int(
            (await session.execute(select(func.count()).select_from(model).where(*where))).scalar_one()
        )


async def test_the_history_is_written_and_every_row_says_it_is_imported(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import CostLedger, Event, Finding, HumanRequest, Run

    answer = await client.post(URL.format(id=project["id"]), json=DOCUMENT)

    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["created"] == {
        "work_items": 2,
        "runs": 2,
        "decisions": 1,
        "findings": 1,
        "pending": 0,
        "actions": 0,
    }
    assert body["unchanged"] == {
        "work_items": 0,
        "runs": 0,
        "decisions": 0,
        "findings": 0,
        "pending": 0,
        "actions": 0,
    }
    # 112000 fresh input at 3, 300000 cached at 0.3, 18500 output at 15, then the declared 0.25.
    assert body["cost_usd"] == pytest.approx(0.7035 + 0.25)

    items = (await client.get(f"/api/v1/projects/{project['id']}/work-items")).json()["items"]
    by_key = {item["tracker_key"]: item for item in items}
    done = by_key["demo:inv-142"]
    assert done["state"] == "done" and done["imported"] is True
    assert done["closed_at"] is not None and done["created_at"].startswith("2026-11-02T09:00")
    assert done["totals"]["cost_usd"] == pytest.approx(0.9535) and done["totals"]["runs"] == 2
    assert by_key["demo:inv-143"]["state"] == "inbox", "no history: the workflow's initial state"

    runs = (await client.get(f"/api/v1/work-items/{done['id']}/runs")).json()
    assert {r["stage_role"]: (r["status"], r["imported"]) for r in runs["items"]} == {
        "implement": ("succeeded", True),
        "verify": ("failed", True),
    }
    timeline = (await client.get(f"/api/v1/work-items/{done['id']}/timeline")).json()["items"]
    moves = [e["title"] for e in timeline if e["kind"] == "state_change"]
    assert moves == ["inbox → in_progress", "in_progress → done"]
    assert {e["kind"] for e in timeline} >= {"run", "decision", "finding", "state_change"}
    findings = (await client.get(f"/api/v1/projects/{project['id']}/findings")).json()["items"]
    assert [(f["title"], f["imported"]) for f in findings] == [
        ("The invoice download link does not expire", True)
    ]

    assert await _count(CostLedger) == 2 and await _count(CostLedger, CostLedger.imported.is_(False)) == 0
    assert await _count(Run, Run.imported.is_(False)) == 0
    assert await _count(Finding, Finding.imported.is_(False)) == 0
    assert await _count(HumanRequest, HumanRequest.decided_at.is_not(None)) == 1
    assert await _count(Event) >= 2

    report = (await client.get(f"/api/v1/projects/{project['id']}/costs?since=2026-11-01")).json()
    assert report["total"]["cost_usd"] == pytest.approx(0.9535)
    assert report["total"]["imported_usd"] == pytest.approx(0.9535), "shown apart, never as measured"


async def test_an_imported_cost_counts_in_no_budget(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from datetime import UTC, datetime

    from tranibor_api.db.session import session_scope
    from tranibor_api.services.budgets import project_spent_since

    assert (await client.post(URL.format(id=project["id"]), json=DOCUMENT)).status_code == 200

    async with session_scope(orgs="*") as session:
        spent = await project_spent_since(session, project["id"], datetime(2026, 1, 1, tzinfo=UTC))
    assert spent == 0.0


async def test_importing_the_same_document_again_changes_nothing(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import CostLedger, Event, Finding, HumanRequest, Run, WorkItem

    await client.post(URL.format(id=project["id"]), json=DOCUMENT)
    models = (WorkItem, Run, CostLedger, Finding, HumanRequest, Event)
    before = [await _count(model) for model in models]

    again = await client.post(URL.format(id=project["id"]), json=_document())

    assert again.status_code == 200, again.text
    assert again.json()["created"] == {
        "work_items": 0,
        "runs": 0,
        "decisions": 0,
        "findings": 0,
        "pending": 0,
        "actions": 0,
    }
    assert again.json()["unchanged"] == {
        "work_items": 2,
        "runs": 2,
        "decisions": 1,
        "findings": 1,
        "pending": 0,
        "actions": 0,
    }
    assert again.json()["cost_usd"] == 0.0
    assert [await _count(model) for model in models] == before


async def test_an_id_that_holds_other_content_is_refused_not_overwritten(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import WorkItem

    await client.post(URL.format(id=project["id"]), json=DOCUMENT)
    changed = _document()
    changed["work_items"][0]["title"] = "Another title"

    answer = await client.post(URL.format(id=project["id"]), json=changed)

    assert answer.status_code == 409 and "inv-142" in answer.text
    assert await _count(WorkItem, WorkItem.title == "Another title") == 0


async def test_a_new_work_item_in_a_later_document_is_added_beside_the_first(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import WorkItem

    await client.post(URL.format(id=project["id"]), json=DOCUMENT)
    more = _document()
    more["work_items"].append({"id": "inv-144", "title": "A third", "created_at": "2026-11-05T09:00:00Z"})

    answer = await client.post(URL.format(id=project["id"]), json=more)

    assert answer.json()["created"]["work_items"] == 1 and answer.json()["unchanged"]["work_items"] == 2
    assert await _count(WorkItem) == 3


async def test_it_is_refused_on_an_instance_that_did_not_opt_in(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.db.models import WorkItem

    answer = await client.post(URL.format(id=project["id"]), json=DOCUMENT)

    assert answer.status_code == 409
    assert "TRANIBOR_DEMO_HISTORY_IMPORT" in answer.text
    assert await _count(WorkItem) == 0


@pytest.mark.parametrize("environment", ["prod", "staging"])
async def test_it_is_refused_in_production_even_when_the_variable_is_on(
    client: AsyncClient,
    project: dict[str, Any],
    demo: None,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.db.models import WorkItem

    monkeypatch.setenv("TRANIBOR_ENV", environment)
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")  # production refuses the development sign-in
    reset_settings_cache()

    answer = await client.post(URL.format(id=project["id"]), json=DOCUMENT)

    assert answer.status_code == 409
    assert environment in answer.text and "refuse" in answer.text
    assert await _count(WorkItem) == 0


def test_the_api_does_not_start_in_production_with_the_variable_on() -> None:
    from tranibor_core.production import DemoHistoryImportRefusedError, refuse_demo_history_import

    refuse_demo_history_import(environment="dev", enabled="1")
    refuse_demo_history_import(environment="prod", enabled=None)
    for environment in ("prod", "staging"):
        with pytest.raises(DemoHistoryImportRefusedError, match="TRANIBOR_DEMO_HISTORY_IMPORT"):
            refuse_demo_history_import(environment=environment, enabled="1")


def _with(change: Any) -> dict[str, Any]:
    document = _document()
    change(document["work_items"][0])
    return document


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (
            lambda item: item["history"].append({"state": "teleported", "at": "2026-11-04T00:00:00Z"}),
            "teleported",
        ),
        (lambda item: item.update(state="warp"), "warp"),
        (lambda item: item.update(created_by="stranger@elsewhere.test"), "stranger@elsewhere.test"),
        (lambda item: item["decisions"][0].update(by="stranger@elsewhere.test"), "stranger@elsewhere.test"),
        (lambda item: item["runs"][0].update(agent="ghost-agent"), "ghost-agent"),
        (lambda item: item["runs"][0].update(model="vendor/unpriced"), "vendor/unpriced"),
        (lambda item: item["runs"].append(dict(item["runs"][0])), "implement-1"),
        (lambda item: item["findings"][0].update(run="missing-run"), "missing-run"),
        (lambda item: item["findings"][0].update(type="made-up-type"), "made-up-type"),
    ],
    ids=["state", "current-state", "creator", "decider", "agent", "price", "duplicate-id", "run", "type"],
)
async def test_a_document_the_project_cannot_hold_is_refused_naming_the_culprit_and_writes_nothing(
    client: AsyncClient, project: dict[str, Any], demo: None, change: Any, named: str
) -> None:
    from tranibor_api.db.models import CostLedger, WorkItem

    answer = await client.post(URL.format(id=project["id"]), json=_with(change))

    assert answer.status_code == 422, answer.text
    assert named in answer.text
    assert await _count(WorkItem) == 0 and await _count(CostLedger) == 0


async def test_a_history_that_disagrees_with_itself_is_refused(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    out_of_order = _with(lambda item: item["history"].reverse())
    wrong_state = _with(lambda item: item.update(state="inbox"))

    for document in (out_of_order, wrong_state):
        assert (await client.post(URL.format(id=project["id"]), json=document)).status_code == 422


async def test_an_agent_of_the_registry_is_accepted(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import Agent, Organization
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "northwind"))
        ).scalar_one()
        session.add(Agent(org_id=org.id, slug="coder", display_name="Coder"))
    document = _with(lambda item: item["runs"][0].update(agent="coder"))

    answer = await client.post(URL.format(id=project["id"]), json=document)

    assert answer.status_code == 200, answer.text


async def test_another_organisation_cannot_import_into_the_project(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import Membership, Organization, User, WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        other = Organization(slug="acme", name="Acme")
        user = User(email="boss@acme.test", display_name="boss")
        session.add_all([other, user])
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=other.id, role="org_admin"))
    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url="http://test") as c:  # type: ignore[attr-defined]
        await login(c, "boss@acme.test")
        answer = await c.post(URL.format(id=project["id"]), json=DOCUMENT)

    assert answer.status_code in {403, 404}, answer.text
    assert await _count(WorkItem) == 0


async def test_a_project_owner_is_not_enough(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "northwind"))
        ).scalar_one()
        user = User(email="owner@northwind.dev", display_name="owner")
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=org.id, role="project_owner"))
    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url="http://test") as c:  # type: ignore[attr-defined]
        await login(c, "owner@northwind.dev")
        answer = await c.post(URL.format(id=project["id"]), json=DOCUMENT)

    assert answer.status_code == 403, answer.text


async def test_every_import_is_audited(client: AsyncClient, project: dict[str, Any], demo: None) -> None:
    from tranibor_api.db.models import AuditLog

    await client.post(URL.format(id=project["id"]), json=DOCUMENT)

    assert await _count(AuditLog, AuditLog.action == "demo_history.import") == 1


WARRANTY = """
apiVersion: tranibor/v1
kind: Workflow
metadata:
  name: warranty-claims
  version: 1
  finding_types:
    - name: duplicate-claim
      title: Suspected duplicate claim
      severities: [medium, high]
initial: received
actors:
  assessor: { type: agent, role: claim_assessment }
states:
  received: { display: Received }
  assessed: { display: Assessed, terminal: true }
transitions:
  - { id: t-assess, from: received, to: assessed, by: assessor }
"""


def _claim(finding_type: str, severity: str) -> dict[str, Any]:
    return {
        "schema": "tranibor/DemoHistory/v1",
        "workflow": "warranty-claims",
        "work_items": [
            {
                "id": "wc-6",
                "title": "Serial 4471 claimed twice",
                "created_at": "2026-11-02T09:00:00Z",
                "history": [{"state": "received", "at": "2026-11-02T09:00:00Z"}],
                "findings": [
                    {
                        "id": "dup",
                        "title": "Serial 4471 was claimed in March",
                        "type": finding_type,
                        "severity": severity,
                        "evidence": "claims/2026-03-1187",
                    }
                ],
            }
        ],
    }


async def test_a_finding_type_the_workflow_declares_is_imported_and_judged_by_its_scale(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import Finding, WorkItem

    published = await client.put(
        f"/api/v1/projects/{project['id']}/workflows/warranty-claims", json={"yaml": WARRANTY}
    )
    assert published.status_code in (200, 201), published.text
    url = URL.format(id=project["id"])

    outside_scale = await client.post(url, json=_claim("duplicate-claim", "low"))
    assert outside_scale.status_code == 422, outside_scale.text
    assert "severity `low`" in outside_scale.text
    assert await _count(WorkItem) == 0

    answer = await client.post(url, json=_claim("duplicate-claim", "high"))
    assert answer.status_code == 200, answer.text
    assert answer.json()["created"]["findings"] == 1
    assert await _count(Finding, Finding.type == "duplicate-claim") == 1


async def test_a_workflow_declared_type_is_refused_under_a_workflow_that_does_not_declare_it(
    client: AsyncClient, project: dict[str, Any], demo: None
) -> None:
    from tranibor_api.db.models import WorkItem

    document = _with(lambda item: item["findings"][0].update(type="duplicate-claim"))
    answer = await client.post(URL.format(id=project["id"]), json=document)

    assert answer.status_code == 422, answer.text
    assert "unknown finding type `duplicate-claim`" in answer.text
    assert await _count(WorkItem) == 0
