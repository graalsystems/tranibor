"""An imported run carries what its step returned and its gates' verdicts (ADR 0216, point 13).

A demonstration's run page showed an imported run's summary and nothing else: its outputs (the
pre-qualification report, the cited clauses), its evidence and the verdicts of its gates were written by
`seed_local.py` straight into the database, never by the import. The document now carries them; the
import judges the outputs and evidence by the stage result's contract and writes the gates' verdicts in
the run's journal as a live step does (`gate.outcome`), so the console reads them as it reads a live run's.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from httpx import AsyncClient

from .kits.demo_history import DOCUMENT, URL, demo_history_opted_in  # noqa: F401 - the fixture `demo`

pytestmark = pytest.mark.usefixtures("demo")

REPORT = "## Pre-qualification\n\nThe alternator is covered (WP-3.2); a duplicate is suspected (DC-2)."


def _with_outputs() -> dict[str, Any]:
    document = copy.deepcopy(DOCUMENT)
    implement = document["work_items"][0]["runs"][0]
    implement["outputs"] = {
        "prequalification_report": REPORT,
        "cited_clauses": [{"clause": "WP-3.2", "document": "warranty-policy.md", "quote": "Alternators..."}],
    }
    implement["evidence"] = {"facts": {"clauses_cited": 1, "duplicate_suspected": True}}
    implement["gates"] = [
        {"name": "output_schema", "passed": True},
        {"name": "evidence_facts", "passed": False, "detail": "no clause cited for the labour time"},
    ]
    return document


async def _runs(client: AsyncClient, project: dict[str, Any]) -> dict[str, Any]:
    items = (await client.get(f"/api/v1/projects/{project['id']}/work-items")).json()["items"]
    shipped = next(item for item in items if item["tracker_key"] == "demo:inv-142")
    runs = (await client.get(f"/api/v1/work-items/{shipped['id']}/runs")).json()["items"]
    return {run["stage_role"]: run for run in runs}


async def test_an_imported_run_carries_its_outputs_evidence_and_gate_verdicts(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    answer = await client.post(URL.format(id=project["id"]), json=_with_outputs())
    assert answer.status_code == 200, answer.text

    runs = await _runs(client, project)
    implement = (await client.get(f"/api/v1/runs/{runs['implement']['id']}")).json()
    assert implement["result"]["outputs"]["prequalification_report"] == REPORT
    assert implement["result"]["outputs"]["cited_clauses"][0]["clause"] == "WP-3.2"
    assert implement["result"]["evidence"]["facts"] == {"clauses_cited": 1, "duplicate_suspected": True}
    events = (await client.get(f"/api/v1/runs/{runs['implement']['id']}/events")).json()["items"]
    verdicts = [(e["payload"]["name"], e["payload"]["passed"]) for e in events if e["type"] == "gate.outcome"]
    assert verdicts == [("output_schema", True), ("evidence_facts", False)]

    verify = (await client.get(f"/api/v1/runs/{runs['verify']['id']}/events")).json()["items"]
    assert not [e for e in verify if e["type"] == "gate.outcome"], "a run without gates has no verdict"

    again = await client.post(URL.format(id=project["id"]), json=_with_outputs())
    assert again.status_code == 200 and again.json()["created"]["runs"] == 0, "the same document writes nothing"
    events = (await client.get(f"/api/v1/runs/{runs['implement']['id']}/events")).json()["items"]
    assert len([e for e in events if e["type"] == "gate.outcome"]) == 2


async def test_outputs_that_are_not_a_stage_result_s_are_refused_and_nothing_is_written(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    document = _with_outputs()
    document["work_items"][0]["runs"][0]["evidence"] = {"tests_run": "many"}

    answer = await client.post(URL.format(id=project["id"]), json=document)

    assert answer.status_code == 422, answer.text
    assert "implement-1" in answer.text and "stage result" in answer.text
    items = (await client.get(f"/api/v1/projects/{project['id']}/work-items")).json()["items"]
    assert not [item for item in items if item["tracker_key"].startswith("demo:")]


async def test_a_document_imported_before_outputs_existed_is_still_the_same_document(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    """A run without outputs, evidence or gates keeps its former digest: no 409 on a re-import."""
    from tranibor_api.services.demo_history import digest_of
    from tranibor_contracts import DemoHistory

    first = await client.post(URL.format(id=project["id"]), json=DOCUMENT)
    assert first.status_code == 200, first.text
    again = await client.post(URL.format(id=project["id"]), json=DOCUMENT)
    assert again.status_code == 200 and again.json()["unchanged"]["work_items"] == 2

    item = DemoHistory.model_validate(DOCUMENT).work_items[0]
    dumped = item.model_dump(mode="json")
    assert all({"outputs", "evidence", "gates"} <= set(run) for run in dumped["runs"])
    for run in dumped["runs"]:
        del run["outputs"], run["evidence"], run["gates"]
    import hashlib
    import json

    used = {run.model: DOCUMENT["prices"][run.model] for run in item.runs if run.model in DOCUMENT["prices"]}
    dumped.pop("pending", None)
    former = {"item": dumped, "workflow": "invoices", "prices": used}
    expected = hashlib.sha256(json.dumps(former, sort_keys=True, default=str).encode()).hexdigest()
    assert digest_of(item, "invoices", DemoHistory.model_validate(DOCUMENT).prices) == expected
