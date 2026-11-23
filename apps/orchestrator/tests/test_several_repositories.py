"""Everything that talks to git works on the work item's target repository (S58-02, ADR 0124).

The project of the scene holds its primary, `billing-api` on github.com, and a second repository,
`diamctl`, on another host with its own default branch, branch prefix and test command: what a run
clones, the branch and pull request, the guarantees that read a diff or the checks, the run's network
and the SCM installation must all come from the one the work item names. What the `software-delivery`
plugin does with it (the branch, the pull request, the diff and CI guarantees, the GitHub App
installation of another owner) is in its suite: `plugins/software-delivery/tests/orchestrator/
test_several_repositories_delivery.py` (ADR 0181, round D).
"""

from __future__ import annotations

from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from tranibor_testkit.workflows import use_workflow

from .conftest import Fixture, refine_result
from .kits.escalation import requests_of
from .test_interpreter import _runs, _wait_state, _wait_until, scripted, start

pytestmark = pytest.mark.asyncio

PRIMARY_URL = "https://github.com/northwind/billing-api.git"
DIAMCTL = {
    "name": "diamctl",
    "url": "https://git.acme.example/northwind/diamctl.git",
    "default_branch": "trunk",
    "branch_prefix": "fix/",
    "test_command": "go test ./...",
}


async def _scene(
    setup: Fixture,
    *,
    fields: dict[str, Any] | None = None,
    documents: dict[str, Any] | None = None,
    repositories: list[dict[str, Any]] | None = None,
) -> None:
    """The project holds `repositories` beside its primary; the work item says `fields`."""
    from tranibor_api.db.models import Project, WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        project = await session.get(Project, setup.project_id)
        item = await session.get(WorkItem, setup.work_item_id)
        assert project is not None and item is not None
        delivery = dict(project.config["delivery"])
        delivery["repositories"] = [DIAMCTL] if repositories is None else repositories
        project.config = {**project.config, "delivery": delivery}
        item.fields = dict(fields or {})
        item.documents = dict(documents or {})


async def _allow_only_pypi(setup: Fixture) -> None:
    """The project's policy opens `pypi.org` alone: a repository host on the run comes from its clone."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import active_policy, policy_model

    async with session_scope() as session:
        row = await active_policy(session, setup.project_id)
        assert row is not None
        policy = policy_model(row)
        policy.sandbox.network.allow_domains = ["pypi.org"]
        row.json_doc = policy.model_dump(mode="json", exclude_none=True)


async def _item(setup: Fixture) -> Any:
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        return await session.get(WorkItem, setup.work_item_id)


def _plan(setup: Fixture) -> dict[str, Any]:
    return {
        "project_id": setup.project_id,
        "work_item_id": setup.work_item_id,
        "transition_id": "t-implement",
        "role": "implement",
        "from_state": "ready",
        "to_state": "done",
        "actor": "dev",
        "attempt": 1,
    }


async def _prepared(setup: Fixture) -> dict[str, Any]:
    from tranibor_orchestrator.activities.stage import prepare_stage

    return dict((await prepare_stage(_plan(setup)))["stage_input"])


# ───────────────────────── what a run clones ─────────────────────────


async def test_a_work_item_that_names_no_repository_works_on_the_primary(setup: Fixture) -> None:
    await _scene(setup)
    stage_input = await _prepared(setup)
    assert stage_input["delivery"]["repo"]["url"] == PRIMARY_URL
    assert stage_input["delivery"]["repo"]["base_branch"] == "main"
    assert stage_input["delivery"]["repo"]["work_branch"] == "choregos/123"
    assert stage_input["delivery"]["commands"]["test"] == "make test"
    assert (await _item(setup)).documents["repository_fixed"] == "billing-api", "fixed at its first clone"


async def test_a_work_item_on_diamctl_clones_diamctl_with_its_branch_commands_and_host(
    setup: Fixture,
) -> None:
    await _scene(setup, fields={"repository": "diamctl"})
    await _allow_only_pypi(setup)
    stage_input = await _prepared(setup)
    assert (stage_input.get("delivery") or {}).get("repo") == {
        "url": DIAMCTL["url"],
        "base_branch": "trunk",
        "work_branch": "fix/123",
        "clone_depth": 50,
    }
    assert stage_input["delivery"]["commands"]["test"] == "go test ./..."
    assert stage_input["delivery"]["commands"].get("lint") is None, "diamctl's commands, not the primary's"
    reachable = stage_input["permissions"]["allow_domains"]
    assert "git.acme.example" in reachable, "the target's host is implied on the run's network"
    assert "github.com" not in reachable, "the primary's host is not: the run never clones it"
    assert (await _item(setup)).documents["repository_fixed"] == "diamctl"


async def test_a_project_with_one_repository_is_unchanged(setup: Fixture) -> None:
    await _scene(setup, repositories=[])
    await _allow_only_pypi(setup)
    stage_input = await _prepared(setup)
    assert stage_input["delivery"]["repo"]["url"] == PRIMARY_URL
    assert stage_input["delivery"]["repo"]["work_branch"] == "choregos/123"
    assert "github.com" in stage_input["permissions"]["allow_domains"]
    assert "repository_fixed" not in ((await _item(setup)).documents or {}), "nothing to choose, nothing kept"


async def test_an_unknown_repository_refuses_the_stage_before_anything_runs(setup: Fixture) -> None:
    from tranibor_orchestrator.activities.stage import prepare_stage

    await _scene(setup, fields={"repository": "diamclt"})
    with pytest.raises(ApplicationError) as refused:
        await prepare_stage(_plan(setup))
    assert refused.value.type == "RepositoryRefused"
    assert refused.value.non_retryable
    assert "`diamclt` is not a repository" in str(refused.value)
    assert "`billing-api`, `diamctl`" in str(refused.value), "the names it may take"
    assert await _runs(setup) == 0
    assert setup.adapters.executor.started == [], "no runner pod"


async def test_a_work_item_fixed_to_diamctl_refuses_another_repository(setup: Fixture) -> None:
    from tranibor_orchestrator.activities.stage import prepare_stage

    await _scene(setup, fields={"repository": "billing-api"}, documents={"repository_fixed": "diamctl"})
    with pytest.raises(ApplicationError) as refused:
        await prepare_stage(_plan(setup))
    assert refused.value.type == "RepositoryRefused"
    assert "works on `diamctl` since its first step" in str(refused.value)


async def test_a_job_step_clones_the_target_too(setup: Fixture) -> None:
    from tranibor_orchestrator.activities.stage import prepare_stage

    await _scene(setup, fields={"repository": "diamctl"})
    await _allow_only_pypi(setup)
    prepared = await prepare_stage({**_plan(setup), "job": {"command": ["make", "scan"]}})
    stage_input = prepared["stage_input"]
    assert stage_input["delivery"]["repo"]["url"] == DIAMCTL["url"]
    assert stage_input["delivery"]["repo"]["work_branch"] == "fix/123"
    assert stage_input["delivery"]["commands"]["test"] == "go test ./..."
    assert "git.acme.example" in stage_input["permissions"]["allow_domains"]
    assert "github.com" not in stage_input["permissions"]["allow_domains"]


# ───────────────────────── branch, pull request, guarantees ─────────────────────────


# ───────────────────────── a step names the repository ─────────────────────────


async def _run_with_outputs(setup: Fixture, run_id: str, outputs: dict[str, Any], declared: list[str]) -> Any:
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    from .conftest import stage_result

    result = stage_result(outputs=outputs)
    async with session_scope() as session:
        session.add(
            Run(
                id=run_id,
                work_item_id=setup.work_item_id,
                project_id=setup.project_id,
                stage_role="triage",
                status="running",
                stage_input={"transition": {"outputs": declared}},
            )
        )
    return result.model_dump(mode="json", by_alias=True)


async def test_a_declared_repository_output_becomes_the_target(setup: Fixture) -> None:
    from tranibor_orchestrator.activities.outcome import record_run_outcome

    await _scene(setup)
    result = await _run_with_outputs(setup, "run-triage", {"repository": "diamctl"}, ["repository"])
    await record_run_outcome({"project_id": setup.project_id, "run_id": "run-triage", "result": result})
    assert (await _item(setup)).fields["repository"] == "diamctl"
    assert (await _prepared(setup))["delivery"]["repo"]["url"] == DIAMCTL["url"], "the next stage clones it"


@pytest.mark.parametrize("step", ["job", "agent"])
async def test_a_step_that_names_the_repository_does_not_fix_the_one_it_reads(
    setup: Fixture, step: str
) -> None:
    """A context job reads the primary, then names the repository the finding lives in: fixing the
    primary while it runs would refuse the one it names at the next step."""
    from tranibor_orchestrator.activities.outcome import record_run_outcome
    from tranibor_orchestrator.activities.stage import prepare_stage

    await _scene(setup)
    await _allow_only_pypi(setup)
    plan = {**_plan(setup), "outputs": ["repository"]}
    if step == "job":
        plan["job"] = {"command": ["make", "context"]}
    prepared = await prepare_stage(plan)
    assert prepared["stage_input"]["delivery"]["repo"]["url"] == PRIMARY_URL, "it reads the current target"
    assert "repository_fixed" not in ((await _item(setup)).documents or {}), "nothing is fixed yet"

    result = await _run_with_outputs(setup, "run-context", {"repository": "diamctl"}, ["repository"])
    await record_run_outcome({"project_id": setup.project_id, "run_id": "run-context", "result": result})
    # The next stage (another attempt: the same plan returns the run it already prepared).
    nxt = (await prepare_stage({**_plan(setup), "attempt": 2}))["stage_input"]
    assert nxt["delivery"]["repo"]["url"] == DIAMCTL["url"], "the next stage clones the named one"
    assert (await _item(setup)).documents["repository_fixed"] == "diamctl"


async def test_an_undeclared_repository_output_changes_nothing(setup: Fixture) -> None:
    from tranibor_orchestrator.activities.outcome import record_run_outcome

    await _scene(setup)
    result = await _run_with_outputs(setup, "run-free", {"repository": "diamctl"}, ["summary_markdown"])
    await record_run_outcome({"project_id": setup.project_id, "run_id": "run-free", "result": result})
    assert "repository" not in ((await _item(setup)).fields or {}), "an agent's word is not a field"


async def test_a_decision_question_named_repository_answers_the_target(setup: Fixture) -> None:
    from tranibor_contracts import DecisionActor
    from tranibor_orchestrator.activities.decision import decide, decision_request

    from .test_decision_step import _catalogue, _Transition

    await _scene(setup)
    await _catalogue(setup)
    where = DecisionActor.model_validate(
        {
            "type": "decision",
            "role": "locate",
            "state": ["title"],
            "questions": {
                "repository": {
                    "type": "choice",
                    "instructions": "Which repository holds the finding?",
                    "options": ["billing-api", "diamctl"],
                }
            },
        }
    )
    setup.adapters.decisions.answer(repository=("diamctl", 0.93))
    await decide(
        decision_request(
            project_id=setup.project_id,
            work_item_id=setup.work_item_id,
            transition=_Transition(),
            decision=where,
            attempt=1,
            last_outputs=None,
        )
    )
    assert (await _item(setup)).fields["repository"] == "diamctl"


def test_only_a_declared_repository_output_is_adopted() -> None:
    from tranibor_orchestrator.activities.repositories import adopted

    assert adopted({"repository": "diamctl", "repository_confidence": 0.9}, ["repository"]) == "diamctl"
    assert adopted({"repository": " "}, ["repository"]) is None
    assert adopted({"repository": 3}, ["repository"]) is None
    assert adopted({"repository": "diamctl"}, ["severity"]) is None


# ───────────────────────── the interpreter escalates, then runs on the target ─────────────────────────


async def test_an_unknown_repository_escalates_then_the_stage_runs_on_the_repository_set(
    setup: Fixture, temporal_env: Any, worker_factory: Any
) -> None:
    """On a workflow of the core's (ADR 0181, round D). The same walk on `default-simple`, the walk that
    records `wi-repository-refused-S58-02` for `tests/replay`, is in the `software-delivery` plugin's
    suite (`test_several_repositories_delivery.py`), so the history keeps being recorded as it was."""
    from sqlalchemy import select
    from temporalio.worker import Replayer
    from tranibor_api.db.models import Run, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_orchestrator.extension_parts import all_workflows

    await use_workflow(setup)  # the core's refinement: `default-simple` is the plugin's
    await _scene(setup, fields={"repository": "diamclt"})  # a label typed in the tracker
    scripted(setup.adapters, refine_result("spec"))
    async with worker_factory():
        handle = await start(temporal_env, setup)
        await _wait_state(handle, "needs_human")
        await _wait_until(handle, "the work item waits for a person", lambda s: s["pending_request"])
        assert await _runs(setup) == 0
        asked = [d for d in await requests_of(setup) if (d.payload or {}).get("reason") == "configuration"]
        assert asked and "`diamclt` is not a repository" in asked[0].payload["question"], asked

        async with session_scope() as session:  # a person sets the field right, then replays
            item = await session.get(WorkItem, setup.work_item_id)
            assert item is not None
            item.fields = {"repository": "diamctl"}
        await handle.signal("control", {"action": "rerun_stage", "by": "operator"})
        await _wait_state(handle, "awaiting_spec_approval")
        async with session_scope() as session:
            run = (
                await session.execute(select(Run).where(Run.work_item_id == setup.work_item_id))
            ).scalar_one()
            assert run.stage_input["delivery"]["repo"]["url"] == DIAMCTL["url"]
        await handle.signal("control", {"action": "stop"})
        await handle.result()
        history = await handle.fetch_history()

    await Replayer(workflows=all_workflows()).replay_workflow(history)
