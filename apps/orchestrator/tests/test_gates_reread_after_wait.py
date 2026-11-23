# SPDX-License-Identifier: Apache-2.0
"""S22-32 (#359): a transition's synchronous guarantees are read again once its pending ones resolve.

`_gates` evaluated every guarantee on entry, then the wait re-read the pending ones only: a synchronous
guarantee that turned red meanwhile was judged on the verdict read when the transition was entered.
Played here on the core alone (ADR 0181, round D): a workflow of the test's own, whose step carries a
guarantee pending until an event and a synchronous one, both registered by the test as an extension
would register them. The software delivery walk that found it (an ADR number taken while the CI ran, the
history archived for `tests/replay`) is in the `software-delivery` plugin's suite
(`plugins/software-delivery/tests/orchestrator/test_adr_number_taken_during_the_ci.py`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from tranibor_testkit.workflows import use_workflow

from .conftest import Fixture, stage_result
from .test_interpreter import start

pytestmark = pytest.mark.asyncio

PLUGIN = "reread-test"
AWAITED = "awaited_event"
STILL_TRUE = "still_true"

WORKFLOW = f"""\
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: reread-after-wait
  version: 1
  description: One step whose guarantees wait for an event, then are read again.
actors:
  dev: {{ type: agent, role: implement, model: "profile:standard" }}
  owner: {{ type: human, group: product-owners, sla_hours: 24 }}
states:
  inbox: {{ display: To do, kind: wait, tracker: {{ status: Todo }} }}
  done: {{ display: Done, terminal: true, tracker: {{ status: Done }} }}
  needs_human: {{ display: Needs a human, kind: wait, tracker: {{ status: Blocked }} }}
  abandoned: {{ display: Abandoned, terminal: true, tracker: {{ status: Cancelled }} }}
transitions:
  - id: t-implement
    from: inbox
    to: done
    by: dev
    gates: [{AWAITED}, {STILL_TRUE}]
    timeout_hours: 4
    on_fail: {{ to: inbox, max_attempts: 2, escalate_to: needs_human }}
  - id: t-abandon
    from: needs_human
    to: abandoned
    by: owner
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


@pytest.fixture
def world() -> Iterator[dict[str, bool]]:
    """What the two guarantees read: `arrived` (the awaited event) and `true` (the synchronous fact).
    Registered for the test, then forgotten, as if the extension had never been installed."""
    from tranibor_core.gates import GateContext, GateOutcome, forget, gate

    state = {"arrived": False, "true": True}

    @gate(AWAITED, asynchronous=True, plugin=PLUGIN)
    def _awaited(ctx: GateContext, params: dict[str, Any]) -> GateOutcome:
        if not state["arrived"]:
            return GateOutcome(AWAITED, False, pending=True, detail="waiting for the event")
        return GateOutcome(AWAITED, True, detail="the event arrived")

    @gate(STILL_TRUE, plugin=PLUGIN)
    def _still_true(ctx: GateContext, params: dict[str, Any]) -> GateOutcome:
        return GateOutcome(STILL_TRUE, state["true"], detail="true" if state["true"] else "no longer true")

    try:
        yield state
    finally:
        forget(PLUGIN)


async def _gate_journal(setup: Fixture) -> list[dict[str, Any]]:
    """Every guarantee verdict journaled for the work item's runs, in order."""
    from sqlalchemy import select
    from tranibor_api.db.models import Run, RunEvent
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        runs = (
            (await session.execute(select(Run.id).where(Run.work_item_id == setup.work_item_id)))
            .scalars()
            .all()
        )
        rows = (
            (
                await session.execute(
                    select(RunEvent)
                    .where(RunEvent.run_id.in_(list(runs)), RunEvent.type == "gate.outcome")
                    .order_by(RunEvent.ts, RunEvent.seq)
                )
            )
            .scalars()
            .all()
        )
        return [dict(r.payload) for r in rows]


async def _transitions_played(setup: Fixture) -> list[str]:
    from sqlalchemy import select
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        rows = await session.execute(
            select(Run.transition_id).where(Run.work_item_id == setup.work_item_id).order_by(Run.created_at)
        )
        return [str(t) for t in rows.scalars()]


async def _event_awaited(setup: Fixture) -> None:
    """The step is done: the awaited guarantee was read pending, the synchronous one passed."""
    for _ in range(600):
        journal = await _gate_journal(setup)
        if any(v["name"] == AWAITED and v["pending"] for v in journal):
            still = [v for v in journal if v["name"] == STILL_TRUE]
            assert still and still[-1]["passed"], still
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"the event was never awaited: {await _gate_journal(setup)}")


async def test_a_synchronous_guarantee_turned_red_during_the_wait_is_refused_when_the_wait_ends(
    setup: Fixture, temporal_env: Any, worker_factory: Any, world: dict[str, bool]
) -> None:
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    await use_workflow(setup, source=WORKFLOW)
    attempts: list[int] = []

    def agent(stage_input: Any) -> Any:
        attempts.append(stage_input.attempt)
        if len(attempts) > 1:
            world["true"] = True  # the agent sent back puts it right
        return stage_result("done")

    setup.adapters.executor.handler = agent
    with temporal_env.auto_time_skipping_disabled():
        async with worker_factory():
            handle = await start(temporal_env, setup)
            await _event_awaited(setup)
            # While the step waits, the synchronous fact turns false; then the event arrives.
            world["true"] = False
            world["arrived"] = True
            await handle.signal("inbound", {"type": "test.event", "source": "test", "delivery_id": "e-1"})
            outcome = await asyncio.wait_for(handle.result(), timeout=90)
            history = await handle.fetch_history()

    assert outcome["state"] == "done", outcome
    assert await _transitions_played(setup) == ["t-implement", "t-implement"], (
        "the guarantee turned red during the wait sent the step back once"
    )
    refused = [v for v in await _gate_journal(setup) if v["name"] == STILL_TRUE and not v["passed"]]
    assert refused and refused[0]["detail"] == "no longer true", refused
    await Replayer(workflows=all_workflows()).replay_workflow(history)
