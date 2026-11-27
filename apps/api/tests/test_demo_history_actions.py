"""A demonstration history declares its governed actions; deciding an imported one plays nothing (ADR 0216, 14).

`seed_local.py` proposed WC-7's action straight into the database: an instance prepared through the API had
no action waiting for an analyst. The document now names the transitions whose actions were proposed; the
import proposes each with the product's own service, as a live transition does, and marks it imported. A
pending one waits in the project's actions; approving it records the decision and starts nothing: no
`ActionWorkflow`, no relay, no write to the directory. A succeeded one carries its approver and an effects' log
that says it was never played.
"""

from __future__ import annotations

import copy
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient

ORG = "/api/v1/orgs/northwind"
URL = "/api/v1/projects/{id}/demo-history"
ONBOARDING = """
apiVersion: tranibor/v1
kind: Workflow
metadata:
  name: onboarding
  version: 1
  inputs:
    type: object
    required: [upn]
    properties:
      upn: {type: string}
initial: prepared
actors:
  platform: {type: system}
states:
  prepared: {display: Prepared, kind: wait}
  to_review: {display: To review, kind: wait}
  done: {display: Done, terminal: true}
transitions:
  - id: t-accounts
    from: prepared
    to: done
    by: platform
    action:
      kind: arrival.accounts
      title: "The accounts of {{ fields.upn }}"
      params: {upn: "{{ fields.upn }}"}
      effects:
        - effect: connector.call
          with:
            connector: entra-acme
            operation: create_user
            arguments: {upn: "{{ params.upn }}", display_name: Lea}
    on_fail: {to: prepared, max_attempts: 1, escalate_to: to_review}
  - id: t-close
    from: prepared
    to: done
    by: platform
"""
DOCUMENT: dict[str, Any] = {
    "schema": "tranibor/DemoHistory/v1",
    "workflow": "onboarding",
    "work_items": [
        {
            "id": "lea",
            "title": "Arrival of Lea",
            "fields": {"upn": "lea@acme.test"},
            "created_at": "2026-11-02T09:00:00Z",
            "actions": [{"transition": "t-accounts", "at": "2026-11-02T10:00:00Z"}],
        },
        {
            "id": "max",
            "title": "Arrival of Max",
            "fields": {"upn": "max@acme.test"},
            "created_at": "2026-11-03T09:00:00Z",
            "history": [
                {"state": "prepared", "at": "2026-11-03T09:00:00Z"},
                {"state": "done", "at": "2026-11-03T12:00:00Z"},
            ],
            "actions": [
                {
                    "transition": "t-accounts",
                    "status": "succeeded",
                    "at": "2026-11-03T10:00:00Z",
                    "decided_by": "admin@northwind.dev",
                    "decided_at": "2026-11-03T11:00:00Z",
                }
            ],
        },
    ],
}


@pytest.fixture
def directory(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    import tranibor_adapters
    from tranibor_adapters.fakes.entra import FakeEntra

    fake = FakeEntra()
    monkeypatch.setattr(tranibor_adapters, "FAKE_ENTRA", fake)
    monkeypatch.setenv("CONNECTOR_ENTRA_CLIENT_SECRET", "connector-secret")
    yield fake


@pytest.fixture
async def onboarding(
    client: AsyncClient, project: dict[str, Any], directory: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[str]:
    """An instance that opted in, a directory connector, and the project's `onboarding` workflow."""
    from tranibor_api.config import reset_settings_cache

    body = {"name": "entra-acme", "type": "entra", "config": {"tenant_id": "acme", "client_id": "tranibor"},
            "secret_refs": {"client_secret": "env:CONNECTOR_ENTRA_CLIENT_SECRET"}}  # fmt: skip
    assert (await client.post(f"{ORG}/connectors", json=body)).status_code == 201
    published = await client.put(
        f"/api/v1/projects/{project['id']}/workflows/onboarding", json={"yaml": ONBOARDING}
    )
    assert published.status_code in (200, 201), published.text
    monkeypatch.setenv("TRANIBOR_DEMO_HISTORY_IMPORT", "1")
    reset_settings_cache()
    yield str(project["id"])
    reset_settings_cache()


async def _actions(client: AsyncClient, pid: str) -> dict[str, dict[str, Any]]:
    listed = (await client.get(f"/api/v1/projects/{pid}/actions")).json()["items"]
    return {a["title"]: a for a in listed}


async def test_the_actions_are_proposed_as_a_transition_proposes_them_and_marked_imported(
    client: AsyncClient, onboarding: str
) -> None:
    answer = await client.post(URL.format(id=onboarding), json=DOCUMENT)
    assert answer.status_code == 200, answer.text
    assert answer.json()["created"]["actions"] == 2

    actions = await _actions(client, onboarding)
    waiting = actions["The accounts of lea@acme.test"]
    assert (waiting["status"], waiting["imported"], waiting["decisions"]) == ("pending_approval", True, [])
    assert waiting["params"] == {"upn": "lea@acme.test"}, "rendered from the work item's fields"
    done = (await client.get(f"/api/v1/projects/{onboarding}/actions/{actions['The accounts of max@acme.test']['id']}")).json()  # noqa: E501
    assert (done["status"], done["imported"]) == ("succeeded", True)
    assert [d["by"] for d in done["decisions"]] == ["admin@northwind.dev"]
    assert [(e["status"], e["result"]) for e in done["journal"]] == [("done", {"imported": True, "played": False})]

    again = await client.post(URL.format(id=onboarding), json=DOCUMENT)
    assert again.status_code == 200 and again.json()["unchanged"]["actions"] == 2, again.text
    assert len(await _actions(client, onboarding)) == 2, "the same document proposes nothing more"


async def test_approving_an_imported_action_records_the_decision_and_plays_nothing(
    client: AsyncClient, onboarding: str, directory: Any
) -> None:
    from tranibor_api.services.action_relay import start_approved_actions
    from tranibor_api.temporal import get_temporal

    assert (await client.post(URL.format(id=onboarding), json=DOCUMENT)).status_code == 200
    waiting = (await _actions(client, onboarding))["The accounts of lea@acme.test"]
    fake = get_temporal()
    before = (dict(fake.started), list(fake.signals))  # type: ignore[attr-defined]

    decided = await client.post(
        f"/api/v1/projects/{onboarding}/actions/{waiting['id']}/decision", json={"decision": "approve"}
    )

    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved" and decided.json()["imported"] is True
    assert (dict(fake.started), list(fake.signals)) == before, "an imported action's approval started something"  # type: ignore[attr-defined]  # noqa: E501
    assert await start_approved_actions(grace=timedelta(0)) == [], "the relay started an imported action"
    assert (dict(fake.started), list(fake.signals)) == before  # type: ignore[attr-defined]
    assert not getattr(directory, "users", {}), "a user was written in the directory"


def _with(change: Any) -> dict[str, Any]:
    document = copy.deepcopy(DOCUMENT)
    change(document)
    return document


@pytest.mark.parametrize(
    ("change", "named"),
    [
        (lambda d: d["work_items"][0]["actions"][0].update(transition="t-close"), "t-close"),
        (lambda d: d["work_items"][0]["actions"].append(dict(d["work_items"][0]["actions"][0])), "twice"),
        (lambda d: d["work_items"][1]["actions"][0].update(status="pending", decided_by=None, decided_at=None), "terminal"),  # noqa: E501
        (lambda d: d["work_items"][1]["actions"][0].update(decided_by="nobody@elsewhere.test"), "nobody@elsewhere.test"),  # noqa: E501
        (lambda d: d["work_items"][0]["actions"][0].update(at="2026-11-01T00:00:00Z"), "before the work item"),
        (lambda d: d["work_items"][1]["actions"][0].pop("decided_by"), "decided_by"),
    ],
    ids=["no-action", "twice", "terminal", "stranger", "too-early", "undecided"],
)
async def test_an_action_the_project_cannot_hold_is_refused_and_nothing_is_written(
    client: AsyncClient, onboarding: str, change: Any, named: str
) -> None:
    answer = await client.post(URL.format(id=onboarding), json=_with(change))

    assert answer.status_code == 422 and named in answer.text, answer.text
    assert await _actions(client, onboarding) == {}
    items = (await client.get(f"/api/v1/projects/{onboarding}/work-items")).json()["items"]
    assert not [item for item in items if item["tracker_key"].startswith("demo:")]


async def test_removing_the_history_removes_its_actions(client: AsyncClient, onboarding: str) -> None:
    assert (await client.post(URL.format(id=onboarding), json=DOCUMENT)).status_code == 200

    removed = await client.delete(URL.format(id=onboarding))

    assert removed.status_code == 200, removed.text
    assert removed.json()["actions"] == 2
    assert await _actions(client, onboarding) == {}
