# SPDX-License-Identifier: Apache-2.0
"""The diff guarantees on a run whose agent declared no workspace (S51-03, ADR 0119).

Such a run never cloned the repository, so it cannot have pushed to it: the `software-delivery` plugin's
diff guarantees read no change instead of "diff unavailable". Moved from the core suite's
`test_agent_workspace.py` (ADR 0181, round D), whose helpers it copies.
"""

from __future__ import annotations

from typing import Any

import pytest
from tranibor_testkit.orchestrator import Fixture

pytestmark = pytest.mark.asyncio


async def _network(setup: Fixture, *, project: dict[str, Any]) -> None:
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import active_policy, policy_model

    async with session_scope() as session:
        row = await active_policy(session, setup.project_id)
        assert row is not None
        policy = policy_model(row)
        for key, value in project.items():
            setattr(policy.sandbox.network, key, value)
        row.json_doc = policy.model_dump(mode="json", exclude_none=True)


def _plan(setup: Fixture, **extra: Any) -> dict[str, Any]:
    return {
        "project_id": setup.project_id,
        "work_item_id": setup.work_item_id,
        "transition_id": "t-implement",
        "role": "implement",
        "from_state": "ready",
        "to_state": "done",
        "actor": "dev",
        "attempt": 1,
        **extra,
    }


async def _agent(setup: Fixture, **spec: Any) -> None:
    from tranibor_api.db.models import Agent, AgentVersion, Project
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        project = await session.get(Project, setup.project_id)
        assert project is not None
        agent = Agent(
            org_id=project.org_id,
            slug="analyst",
            kind="internal",
            display_name="analyst",
            status="active",
            scope="org",
        )
        session.add(agent)
        await session.flush()
        body = {"instructions": "Analyse the ticket.", "model": "profile:standard", **spec}
        session.add(
            AgentVersion(agent_id=agent.id, org_id=project.org_id, version=1, spec=body, checksum="sha256:v1")
        )


async def _prepared(setup: Fixture, **extra: Any) -> dict[str, Any]:
    from tranibor_orchestrator.activities.stage import prepare_stage

    return dict((await prepare_stage(_plan(setup, **extra)))["stage_input"])


async def test_the_diff_gates_pass_on_a_run_that_never_cloned(
    setup: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No branch (the SCM says 404), and whatever the agent wrote in its empty directory is no
    change to the repository: `diff_size_max files: 0` passes instead of "diff unavailable"."""
    from tranibor_adapters.errors import UpstreamError
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope
    from tranibor_contracts import StageResult, StageStatus
    from tranibor_orchestrator.activities.gates import evaluate_gates

    async def compare(repo: str, base: str, head: str) -> Any:
        raise UpstreamError("github", f"compare {base}...{head}", status_code=404)

    monkeypatch.setattr(setup.adapters.scm, "compare", compare)
    await _network(setup, project={"allow_domains": ["pypi.org"]})
    await _agent(setup, workspace="none")
    await _prepared(setup, agent="analyst")
    result = StageResult(status=StageStatus.DONE, summary="Analysed")
    # The runner's local git measured the notes the agent left in its empty directory.
    result.evidence.diff_files, result.evidence.diff_lines = 1, 12
    result.evidence.measured = ["diff_files", "diff_lines"]
    result.artifacts.commits = ["0ddba11"]
    async with session_scope() as session:
        run = await session.get(Run, f"{setup.work_item_id}-t-implement-1")
        assert run is not None
        run.result = result.model_dump(mode="json", by_alias=True)
        run_id = run.id

    verdicts = await evaluate_gates(
        {
            "project_id": setup.project_id,
            "work_item_id": setup.work_item_id,
            "run_id": run_id,
            "gates": [{"name": "diff_size_max", "params": {"files": 0, "lines": 0}}],
        }
    )
    assert [v["passed"] for v in verdicts] == [True], verdicts
    assert verdicts[0]["detail"] == "0 files / 0 lines"
