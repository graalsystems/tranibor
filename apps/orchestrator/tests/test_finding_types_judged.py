"""The findings a result carries are judged against the work item's workflow (ADR 0217).

The runner reads a finding's type unjudged (it loads no extension and knows no workflow) and normally posts
each finding to the API, which refuses a type the workflow does not declare. A finding that only travels in
the result reaches `record_run_outcome` instead: the type is judged there, and one the workflow does not
declare is not filed.
"""

from __future__ import annotations

from typing import Any

import pytest

from .conftest import Fixture, stage_result

pytestmark = pytest.mark.asyncio

WORKFLOW = """
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: warranty-claims
  version: 1
  finding_types:
    - { name: duplicate-claim, title: Suspected duplicate claim, severities: [medium, high] }
initial: received
actors:
  assessor: { type: agent, role: claim_assessment }
states:
  received: { display: Received }
  assessed: { display: Assessed, terminal: true }
transitions:
  - { id: t-assess, from: received, to: assessed, by: assessor }
"""


def _finding(title: str, finding_type: str, severity: str = "high") -> dict[str, Any]:
    return {"title": title, "type": finding_type, "severity": severity, "evidence": "claims/1187"}


async def test_only_the_types_the_workflow_allows_are_filed(
    setup: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import Finding, Run
    from tranibor_api.db.session import session_scope
    from tranibor_core import parse_workflow
    from tranibor_orchestrator.activities import outcome

    workflow = parse_workflow(WORKFLOW, strict=False)[0]

    async def declared(session: Any, item: Any) -> Any:
        return workflow

    monkeypatch.setattr(outcome, "workflow_of", declared)
    async with session_scope() as session:
        session.add(
            Run(
                id="run-assess",
                work_item_id=setup.work_item_id,
                project_id=setup.project_id,
                stage_role="claim_assessment",
                status="running",
                stage_input={"transition": {"outputs": []}},
            )
        )
    result = stage_result(
        findings=[
            _finding("Serial 4471 was claimed in March", "duplicate-claim"),
            _finding("Password in a fixture", "security", "low"),
            _finding("A ring of claims", "fraud-ring"),
            _finding("Low-grade duplicate", "duplicate-claim", "low"),
        ]
    )
    await outcome.record_run_outcome(
        {
            "project_id": setup.project_id,
            "run_id": "run-assess",
            "result": result.model_dump(mode="json", by_alias=True),
        }
    )
    async with session_scope() as session:
        rows = (await session.execute(select(Finding).where(Finding.origin_run_id == "run-assess"))).scalars()
        assert sorted((row.type, row.title) for row in rows) == [
            ("duplicate-claim", "Serial 4471 was claimed in March"),
            ("security", "Password in a fixture"),
        ]
