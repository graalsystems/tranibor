"""An interpreter (re)started on a working work item plays a NEW attempt (S22-45, #550).

On the dev tenant (2026-10-15, v0.17.15), `mark_agent_ready` restarted the dead interpreters of
`choregos-sandbox-dev#4` and `#6`, in `live`. The new interpreter called `prepare_stage` for
`t-verify-prod` at attempt 1, which handed back the run `…-t-verify-prod-1` that had failed on
2026-10-10 (`reused: true`); `await_run` returned its old result at once, and the work item escalated
within a minute without any new attempt. A run still running when the interpreter died, on the
other hand, must be awaited again, not started twice (ADR 0008).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

import pytest
from tranibor_contracts import StageResult, StageStatus
from tranibor_testkit.interpreter import start
from tranibor_testkit.workflows import use_workflow

from .conftest import Fixture, recording_history, stage_result
from .kits.train_and_work_item import wait_until_state

pytestmark = pytest.mark.asyncio

HISTORIES = pathlib.Path(__file__).resolve().parents[3] / "tests" / "replay" / "histories"
FAILURE = StageResult(status=StageStatus.FAILED, summary="the smoke test failed", reason="agent_error")

#: The tail of `dev-simple` the dev tenant's work items stood in, on the core's gates: `t-verify-prod`
#: from `live`, one attempt, then a person. How the change reached production (`dev-simple`'s pull
#: request and release train, the `software-delivery` plugin's) is not this test's: a person says so.
VERIFY_IN_PRODUCTION = """\
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: verify-in-production
  version: 1
  description: A change in production is verified by an agent, read-only.
initial: inbox
actors:
  prod_verifier: { type: agent, role: verify_prod, model: "profile:standard" }
  maintainers: { type: human, group: maintainers, sla_hours: 24 }
states:
  inbox: { display: To verify, kind: wait, tracker: { status: Todo } }
  live: { display: In production, tracker: { status: Deployed } }
  verified: { display: Verified in production, terminal: true, tracker: { status: Done } }
  needs_human: { display: Needs a human, kind: wait, tracker: { status: Blocked } }
  abandoned: { display: Abandoned, terminal: true, tracker: { status: Cancelled } }
transitions:
  - id: t-deployed
    from: inbox
    to: live
    by: maintainers
  - id: t-verify-prod
    from: live
    to: verified
    by: prod_verifier
    outputs: [verdict]
    gates:
      - { name: outputs_in, params: { values: { verdict: [approve] } } }
      - { name: evidence_facts, params: { keys: [smoke_ok], must_be_true: [smoke_ok] } }
    on_fail: { to: live, max_attempts: 1, escalate_to: needs_human }
  - id: t-abandon
    from: needs_human
    to: abandoned
    by: maintainers
    timeout_hours: 168
defaults:
  from_any_agent_state:
    on_question: needs_human
    on_budget_exceeded: needs_human
    on_timeout: needs_human
  needs_human:
    on_answer: resume
    on_abandon: abandoned
"""


def prod_verifier_result() -> Any:
    from tranibor_contracts import Evidence

    proof = Evidence(tests_passed=True, tests_run=3, facts={"smoke_ok": True})
    return stage_result("go: error rate unchanged", outputs={"verdict": "approve"}, evidence=proof)


async def _set_state(item_id: str, state: str) -> None:
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        item = await session.get(WorkItem, item_id)
        assert item is not None
        item.state = state


async def _runs(item_id: str) -> list[tuple[str, str]]:
    from sqlalchemy import select
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        rows = (
            await session.execute(select(Run).where(Run.work_item_id == item_id).order_by(Run.created_at))
        ).scalars()
        return [(str(r.id), str(r.status)) for r in rows]


async def _undecide(item_id: str) -> int:
    """Forget that the runs' decisions were applied; returns how many runs had one recorded."""
    from sqlalchemy import select
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        rows = (await session.execute(select(Run).where(Run.work_item_id == item_id))).scalars().all()
        decided = [run for run in rows if run.decided_at is not None]
        for run in decided:
            run.decided_at = None
        return len(decided)


async def _live_ticket(setup: Fixture) -> str:
    """A work item already in production, waiting for `t-verify-prod`, as dev #4 was."""
    await use_workflow(setup, source=VERIFY_IN_PRODUCTION)
    await _set_state(setup.work_item_id, "live")
    return setup.work_item_id


async def test_a_restart_after_a_failed_attempt_plays_the_next_one(
    setup: Fixture, temporal_env: Any, worker_factory: Any
) -> None:
    """`t-verify-prod-1` failed; the interpreter died with the work item in `live` and was started
    again. It plays `t-verify-prod-2` — the old code got run 1 back and escalated at once."""
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    item_id = await _live_ticket(setup)
    played: list[str] = []

    def verifier(stage_input: Any) -> Any:
        played.append(stage_input.run_id)
        return FAILURE if len(played) == 1 else prod_verifier_result()

    setup.adapters.executor.handler = verifier
    async with worker_factory():
        first = await start(temporal_env, setup)
        await wait_until_state(first, "status", lambda s: s["state"] == "needs_human")
        # The abandon is proposed once the escalation is mirrored: no write of it is still in flight.
        await wait_until_state(first, "status", lambda s: bool(s["pending_request"]))
        await first.terminate(reason="the interpreter died (#497)")
        # Where dev #4 stood: in `live`, its first verification failed, no interpreter.
        await _set_state(item_id, "live")
        assert [status for _, status in await _runs(item_id)] == ["failed"]

        handle = await start(temporal_env, setup)
        settled = await wait_until_state(
            handle, "status", lambda s: s["state"] in ("verified", "needs_human")
        )
        assert settled["state"] == "verified", f"escalated without a new attempt: {settled}"
        outcome = await asyncio.wait_for(handle.result(), timeout=60)
        history = await handle.fetch_history()

    assert outcome["state"] == "verified", outcome
    runs = await _runs(item_id)
    assert [run_id.rsplit("-", 1)[1] for run_id, _ in runs] == ["1", "2"], runs
    assert len(played) == 2 and played[1].endswith("-t-verify-prod-2"), played
    await Replayer(workflows=all_workflows()).replay_workflow(history)
    if recording_history():
        target = HISTORIES / "wi-restarted-new-attempt-S22-45.json"
        target.write_text(json.dumps(json.loads(history.to_json()), indent=1) + "\n", encoding="utf-8")


async def test_a_restart_while_the_attempt_runs_awaits_it(
    setup: Fixture, temporal_env: Any, worker_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The interpreter dies while `t-verify-prod-1` runs: the restarted one awaits that run, and
    starts no second one — only a run that is over is a spent attempt."""
    from tranibor_core.domain import ExecRef, ExecStatus, StageJobSpec
    from tranibor_orchestrator.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_HEARTBEAT_SECONDS", "1")  # `await_run` polls every second
    reset_settings_cache()
    item_id = await _live_ticket(setup)
    executor = setup.adapters.executor
    executor.handler = lambda _stage_input: prod_verifier_result()
    real_start = executor.start

    async def start_and_keep_running(spec: StageJobSpec) -> ExecRef:
        ref = await real_start(spec)
        executor.statuses[spec.run_id] = ExecStatus(state="running")
        return ref

    executor.start = start_and_keep_running
    async with worker_factory():
        first = await start(temporal_env, setup)
        await wait_until_state(first, "status", lambda s: bool(s["current_run"]))
        for _ in range(100):
            if [status for _, status in await _runs(item_id)] == ["running"]:
                break
            await asyncio.sleep(0.1)
        await first.terminate(reason="the interpreter died (#497)")

        handle = await start(temporal_env, setup)
        status = await wait_until_state(handle, "status", lambda s: bool(s["current_run"]))
        ((run_id, _),) = await _runs(item_id)
        assert status["current_run"] == run_id and run_id.endswith("-t-verify-prod-1"), status
        executor.statuses[run_id] = ExecStatus(
            state="succeeded", exit_code=0, result_url=f"fake://runs/{run_id}/result.json"
        )
        outcome = await asyncio.wait_for(handle.result(), timeout=60)

    assert outcome["state"] == "verified", outcome
    assert len(await _runs(item_id)) == 1
    assert executor.started and len(executor.started) == 1


async def test_a_restart_after_a_succeeded_run_reads_it_back(
    setup: Fixture, temporal_env: Any, worker_factory: Any
) -> None:
    """`t-verify-prod-1` succeeded, but the interpreter died before its decision was recorded: the
    restarted one reads that run's result back and moves on — no second run, which would cost the
    stage again and could push its commits twice."""
    item_id = await _live_ticket(setup)
    executor = setup.adapters.executor
    executor.handler = lambda _stage_input: prod_verifier_result()
    async with worker_factory():
        first = await start(temporal_env, setup)
        await asyncio.wait_for(first.result(), timeout=60)
        # Where the work item stands if the interpreter died between the run and its decision:
        # its state not moved, and the run not marked decided (#552).
        await _set_state(item_id, "live")
        assert await _undecide(item_id) == 1, "the applied decision is recorded on the run"
        assert [status for _, status in await _runs(item_id)] == ["succeeded"]

        handle = await start(temporal_env, setup)
        outcome = await asyncio.wait_for(handle.result(), timeout=60)

    assert outcome["state"] == "verified", outcome
    runs = await _runs(item_id)
    assert len(runs) == 1 and runs[0][0].endswith("-t-verify-prod-1"), runs
    assert len(executor.started) == 1, [spec.run_id for spec in executor.started]
