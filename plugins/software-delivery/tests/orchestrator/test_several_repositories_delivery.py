# SPDX-License-Identifier: Apache-2.0
"""What the `software-delivery` plugin does on the work item's target repository (S58-02, ADR 0124).

The scene's project holds its primary, `billing-api` on github.com, and a second repository, `diamctl`, on
another host with its own default branch and branch prefix: the branch and the pull request, the guarantees
that read a diff or the checks, and the SCM installation come from the one the work item names. Moved from
the core suite's `test_several_repositories.py` (ADR 0181, round D), which keeps what the core does with a
target (what a run clones, a step naming it, a refusal) and the helpers copied here.
"""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from tranibor_testkit.escalation import requests_of
from tranibor_testkit.interpreter import runs as _runs
from tranibor_testkit.interpreter import scripted, start
from tranibor_testkit.interpreter import wait_state as _wait_state
from tranibor_testkit.interpreter import wait_until as _wait_until
from tranibor_testkit.orchestrator import Fixture, recording_history, refine_result, stage_result

pytestmark = pytest.mark.asyncio

HISTORIES = pathlib.Path(__file__).resolve().parents[4] / "tests" / "replay" / "histories"
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


async def _item(setup: Fixture) -> Any:
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        return await session.get(WorkItem, setup.work_item_id)


# ───────────────────────── branch, pull request, guarantees ─────────────────────────


async def test_the_branch_the_pull_request_and_the_guarantees_are_diamctl_s(setup: Fixture) -> None:
    from tranibor_orchestrator.activities.gates import evaluate_gates
    from tranibor_software_delivery.source_control import (
        check_scope_violations,
        ensure_branch,
        open_pull_request,
    )

    await _scene(setup, fields={"repository": "diamctl"})
    ids = {"project_id": setup.project_id, "work_item_id": setup.work_item_id}
    scm = setup.adapters.scm

    assert await ensure_branch(ids) == {"branch": "fix/123"}
    assert scm.branches["northwind/diamctl"] == {"fix/123": "trunk"}
    assert "northwind/billing-api" not in scm.branches
    assert (await _item(setup)).documents["repository_fixed"] == "diamctl"

    await open_pull_request(ids)
    (pr,) = scm.prs.values()
    assert (pr.ref.repo, pr.ref.head, pr.ref.base) == ("northwind/diamctl", "fix/123", "trunk")

    # The diff guarantees read diamctl's branch: a file outside the item's scope there is seen.
    scm.set_diff("northwind/diamctl", "trunk", "fix/123", [("cmd/root.go", 3, 1)])
    [scope] = await evaluate_gates({**ids, "gates": [{"name": "scope_respected", "params": {}}]})
    assert scope["passed"] is False and "cmd/root.go" in scope["annotations"], scope
    assert await check_scope_violations(ids) == ["cmd/root.go"]

    # `ci_green` reads diamctl's pull request.
    scm.set_checks(pr.ref, "success")
    [ci] = await evaluate_gates({**ids, "gates": [{"name": "ci_green", "params": {}}]})
    assert ci["passed"] is True and not ci["pending"], ci

    assert scm.check_runs == [] or all(c["repo"] == "northwind/diamctl" for c in scm.check_runs)


async def test_the_diff_shown_for_a_run_is_the_target_s(setup: Fixture) -> None:
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope
    from tranibor_software_delivery.source_control import collect_run_artifacts

    await _scene(setup, fields={"repository": "diamctl"})
    setup.adapters.scm.set_diff("northwind/diamctl", "trunk", "fix/123", [("cmd/root.go", 3, 1)])
    async with session_scope() as session:
        session.add(
            Run(
                id="run-diff",
                work_item_id=setup.work_item_id,
                project_id=setup.project_id,
                stage_role="implement",
                status="succeeded",
                result=stage_result().model_dump(mode="json", by_alias=True),
            )
        )
    ids = {"project_id": setup.project_id, "work_item_id": setup.work_item_id, "run_id": "run-diff"}
    assert await collect_run_artifacts(ids) == {"files": 1}
    async with session_scope() as session:
        run = await session.get(Run, "run-diff")
        assert run is not None
        assert run.result["artifacts"]["reports"]["base"] == "trunk"


# ───────────────────────── the App token of another owner ─────────────────────────


#: The other kinds a project's bundle builds, answered by a probe so nothing real is reached.
KINDS = ("tracker", "ci", "cd", "runtime", "memory", "gateway", "notify")


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """What the GitHub SCM is built with, once fakes are off (as in production)."""
    from tranibor_adapters import ConnectorTypeSpec, register
    from tranibor_adapters.registry import _REGISTRY, _SPECS

    received: list[dict[str, Any]] = []
    spec = ConnectorTypeSpec("probe", KINDS, {"type": "object", "properties": {}})
    for kind in KINDS:
        register(kind, "probe", spec)(lambda cfg: object())
    monkeypatch.setitem(_REGISTRY, ("scm", "github"), lambda cfg: received.append(cfg) or object())
    monkeypatch.delenv("TRANIBOR_FAKES", raising=False)
    try:
        yield received
    finally:
        for kind in KINDS:
            _REGISTRY.pop((kind, "probe"), None)
            _SPECS.pop((kind, "probe"), None)


async def test_a_target_of_another_owner_gets_that_owner_s_installation(
    setup: Fixture, built: list[dict[str, Any]]
) -> None:
    from tranibor_api.db.models import Connector, GitHubInstallation, Project
    from tranibor_api.db.session import session_scope
    from tranibor_orchestrator.activities.base import (
        adapters_override,
        load_project,
        set_adapters_override,
    )
    from tranibor_software_delivery.source_control import scm_for

    async with session_scope(orgs="*") as session:
        project = await session.get(Project, setup.project_id)
        assert project is not None
        session.add(
            Connector(
                project_id=project.id, kind="scm", type="github", config={"repo": "northwind/billing-api"}
            )
        )
        for kind in KINDS:
            session.add(Connector(project_id=project.id, kind=kind, type="probe", config={}))
        for installation, owner in ((55, "northwind"), (77, "acme-tools")):
            session.add(
                GitHubInstallation(
                    id=installation,
                    account_login=owner,
                    account_key=owner,
                    account_id=installation * 10,
                    account_type="Organization",
                    org_id=project.org_id,
                )
            )
    shared = adapters_override()
    set_adapters_override(None)
    try:
        async with session_scope(orgs="*") as session:
            bundle = await load_project(session, setup.project_id)
            assert built[-1]["installation_id"] == 55, "the project's adapter: its connector's owner"
            same_owner = await scm_for(session, bundle, "https://github.com/northwind/diamctl.git")
            assert same_owner is bundle.adapters.scm, "one owner, one installation: the same adapter"
            count = len(built)
            await scm_for(session, bundle, "https://github.com/acme-tools/diamctl.git")
        assert len(built) == count + 1
        assert built[-1]["repo"] == "acme-tools/diamctl"
        assert built[-1]["installation_id"] == 77, "the installation claimed on the target's owner"
    finally:
        set_adapters_override(shared)


# ───────────────────────── the interpreter escalates, then runs on the target ─────────────────────────


async def test_an_unknown_repository_escalates_then_the_stage_runs_on_the_repository_set(
    setup: Fixture, temporal_env: Any, worker_factory: Any
) -> None:
    from sqlalchemy import select
    from temporalio.worker import Replayer
    from tranibor_api.db.models import Run, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_orchestrator.extension_parts import all_workflows

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
    if recording_history():
        target = HISTORIES / "wi-repository-refused-S58-02.json"
        target.write_text(json.dumps(json.loads(history.to_json()), indent=1) + "\n", encoding="utf-8")
