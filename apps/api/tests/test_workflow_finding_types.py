"""A workflow declares its own finding types (ADR 0217, S83-11).

A warranty-claims workflow files a suspected duplicate claim as `duplicate-claim`, not as `security` for
lack of a business type. The type is valid for that workflow's work items alone, the console reads its
title from the finding list, and any other name is refused with a 422 that names the workflow and the types
it allows. The core's and the plugins' types stay valid everywhere.
"""

from __future__ import annotations

import importlib.util
from typing import Any

import pytest
from httpx import AsyncClient

from .kits.closed_runs import _stage_input, token_of

WARRANTY = """
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: warranty-claims
  version: 1
  finding_types:
    - name: duplicate-claim
      title: Suspected duplicate claim
      severities: [medium, high]
      description: The same serial number was claimed before.
    - { name: missing-proof, title: Proof of purchase missing }
initial: received
actors:
  assessor: { type: agent, role: claim_assessment }
  platform: { type: system }
states:
  received: { display: Received }
  assessed: { display: Assessed, terminal: true }
transitions:
  - { id: t-assess, from: received, to: assessed, by: assessor }
"""

PLAIN = WARRANTY.replace("warranty-claims", "plain-claims").replace(
    "  finding_types:\n    - name: duplicate-claim\n      title: Suspected duplicate claim\n"
    "      severities: [medium, high]\n      description: The same serial number was claimed before.\n"
    "    - { name: missing-proof, title: Proof of purchase missing }\n",
    "",
)

DUPLICATE = {
    "title": "Serial 4471 was claimed in March",
    "type": "duplicate-claim",
    "severity": "high",
    "evidence": "claims/2026-03-1187",
}


async def _publish(client: AsyncClient, project: dict[str, Any], name: str, document: str) -> Any:
    return await client.put(f"/api/v1/projects/{project['id']}/workflows/{name}", json={"yaml": document})


async def _run_of(client: AsyncClient, project: dict[str, Any], workflow: str) -> str:
    """A run of a work item born in `workflow`."""
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    created = await client.post(
        f"/api/v1/projects/{project['id']}/work-items",
        json={"title": "Claim for a dishwasher", "workflow": workflow, "start": False},
    )
    assert created.status_code == 201, created.text
    run_id = f"{created.json()['id']}-t-assess-1"
    async with session_scope(orgs="*") as session:
        session.add(
            Run(
                id=run_id,
                work_item_id=created.json()["id"],
                project_id=project["id"],
                stage_role="claim_assessment",
                transition_id="t-assess",
                attempt=1,
                status="running",
                stage_input=_stage_input(run_id),
            )
        )
    return run_id


def _post(client: AsyncClient, run_id: str, body: dict[str, Any]) -> Any:
    return client.post(f"/api/v1/internal/runs/{run_id}/findings", json=body, headers=token_of(run_id))


async def test_a_workflow_declaring_a_finding_type_publishes(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    published = await _publish(client, project, "warranty-claims", WARRANTY)
    assert published.status_code == 200, published.text


async def test_a_finding_of_a_declared_type_is_accepted_and_listed_with_its_title(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert (await _publish(client, project, "warranty-claims", WARRANTY)).status_code == 200
    run_id = await _run_of(client, project, "warranty-claims")
    accepted = await _post(client, run_id, DUPLICATE)
    assert accepted.status_code == 201, accepted.text
    listed = (await client.get(f"/api/v1/projects/{project['id']}/findings")).json()["items"]
    assert [(f["type"], f["type_title"]) for f in listed] == [
        ("duplicate-claim", "Suspected duplicate claim")
    ]


async def test_an_undeclared_type_is_refused_naming_the_workflow_and_the_allowed_types(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert (await _publish(client, project, "warranty-claims", WARRANTY)).status_code == 200
    run_id = await _run_of(client, project, "warranty-claims")
    refused = await _post(client, run_id, {**DUPLICATE, "type": "fraud-ring"})
    assert refused.status_code == 422, refused.text
    detail = refused.json()["detail"]
    assert "`warranty-claims`" in detail and "`fraud-ring`" in detail
    for allowed in ("duplicate-claim", "missing-proof", "security", "docs", "ux"):
        assert allowed in detail
    assert (await client.get(f"/api/v1/projects/{project['id']}/findings")).json()["items"] == []


async def test_a_type_is_valid_for_its_own_workflow_alone(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert (await _publish(client, project, "warranty-claims", WARRANTY)).status_code == 200
    assert (await _publish(client, project, "plain-claims", PLAIN)).status_code == 200
    run_id = await _run_of(client, project, "plain-claims")
    refused = await _post(client, run_id, DUPLICATE)
    assert refused.status_code == 422 and "`plain-claims`" in refused.json()["detail"]


async def test_a_severity_outside_the_types_scale_is_refused(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert (await _publish(client, project, "warranty-claims", WARRANTY)).status_code == 200
    run_id = await _run_of(client, project, "warranty-claims")
    refused = await _post(client, run_id, {**DUPLICATE, "severity": "low"})
    assert refused.status_code == 422, refused.text
    assert "severity `low`" in refused.json()["detail"] and "medium, high" in refused.json()["detail"]
    assert (
        await _post(
            client, run_id, {**DUPLICATE, "title": "No proof", "type": "missing-proof", "severity": "low"}
        )
    ).status_code == 201


@pytest.mark.parametrize("core", ["security", "docs", "ux"])
async def test_the_cores_types_stay_valid_in_a_workflow_that_declares_its_own(
    client: AsyncClient, project: dict[str, Any], core: str
) -> None:
    assert (await _publish(client, project, "warranty-claims", WARRANTY)).status_code == 200
    run_id = await _run_of(client, project, "warranty-claims")
    accepted = await _post(client, run_id, {**DUPLICATE, "type": core})
    assert accepted.status_code == 201, accepted.text
    (listed,) = (await client.get(f"/api/v1/projects/{project['id']}/findings")).json()["items"]
    assert listed["type"] == core and listed["type_title"] is None


@pytest.mark.skipif(
    importlib.util.find_spec("tranibor_software_delivery") is None,
    reason="`flaky-test` is the software-delivery plugin's: without it, nobody declares the name",
)
async def test_a_plugins_type_stays_valid_and_a_workflow_may_not_take_its_name(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert (await _publish(client, project, "warranty-claims", WARRANTY)).status_code == 200
    taken = WARRANTY.replace("name: duplicate-claim", "name: flaky-test")
    refused = await _publish(client, project, "warranty-claims", taken)
    assert refused.status_code == 422, refused.text
    assert "finding_types.taken" in {e["code"] for e in refused.json()["errors"]}
    # `flaky-test` is the software-delivery plugin's: valid in every workflow, with no title of its own.
    run_id = await _run_of(client, project, "warranty-claims")
    assert (await _post(client, run_id, {**DUPLICATE, "type": "flaky-test"})).status_code == 201


async def test_a_result_naming_an_undeclared_type_is_refused_whole(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert (await _publish(client, project, "warranty-claims", WARRANTY)).status_code == 200
    run_id = await _run_of(client, project, "warranty-claims")
    result = {
        "schema": "choregos/StageResult/v1",
        "status": "done",
        "summary": "assessed",
        "outputs": {},
        "findings": [{**DUPLICATE, "type": "fraud-ring"}],
    }
    url = f"/api/v1/internal/runs/{run_id}/result"
    refused = await client.post(url, json=result, headers=token_of(run_id))
    assert refused.status_code == 422 and "`warranty-claims`" in refused.json()["detail"]
    result["findings"] = [DUPLICATE]
    assert (await client.post(url, json=result, headers=token_of(run_id))).status_code == 200
