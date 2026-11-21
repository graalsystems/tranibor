"""An interpreter that meets a trial's work item ends, instead of failing its task forever (#1865).

Before 0.27.11 (#1781) the catch-up pass started a `WorkflowInterpreter` on trial items (`state =
"trial"`, key `TRIAL-<hex>`). #1804 stopped the starts, not the interpreters already open: in
production three of them (`wi-a2a-smoke-TRIAL-1205c339`, `-ca683c47`, `-72d56e59`, started on
2026-11-15 and 16) stayed `Running`, their workflow task failing with `UnknownStateError: 'trial'`
on every replay, and each worker restart (0.27.13) logged it again. The interpreter re-raised a plain
exception after `record_workflow_failure`: Temporal retries such a task, it never fails the run.

Now it ends after the same commands. `wi-trial-item-left-over-before-the-fix-S67-16.json` was recorded
with the code of 0.27.13 (an interpreter started on a trial item, its first task failure included)
and replays to the end with this code; the production history of `wi-a2a-smoke-TRIAL-72d56e59` did
the same, read with `temporal workflow show` (not archived: production data).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from typing import Any

import pytest
from temporalio.worker import (
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from .conftest import Fixture, recording_history
from .test_interpreter import start
from .test_trial_item_has_no_interpreter import _make_trial

pytestmark = pytest.mark.asyncio

HISTORIES = pathlib.Path(__file__).resolve().parents[3] / "tests" / "replay" / "histories"
LEFT_OVER = HISTORIES / "wi-trial-item-left-over-before-the-fix-S67-16.json"


async def test_an_interpreter_on_a_trial_item_ends(
    setup: Fixture, temporal_env: Any, worker_factory: Any
) -> None:
    from temporalio.worker import Replayer
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_orchestrator.workflows import ALL_WORKFLOWS

    await _make_trial(setup)
    async with worker_factory():
        handle = await start(temporal_env, setup)
        result = await asyncio.wait_for(handle.result(), timeout=60)
        history = await handle.fetch_history()

    assert result["state"] == "trial", result
    assert result["stopped"] is True, result
    async with session_scope() as session:
        item = await session.get(WorkItem, setup.work_item_id)
        assert item is not None
        assert item.state == "trial", "the trial's work item is left as it was"
    await Replayer(workflows=ALL_WORKFLOWS).replay_workflow(history)
    if recording_history():
        target = HISTORIES / "wi-trial-item-ends-S67-16.json"
        target.write_text(json.dumps(json.loads(history.to_json()), indent=1) + "\n", encoding="utf-8")


class _EndWitness(Interceptor):
    """A worker interceptor that keeps what each replayed workflow returned (#2002).

    The interpreter's "ended" log line was the witness, and the suite's logging state decided whether it
    was heard: after an API test ran the migrations in the same xdist worker, `fileConfig` had disabled
    `temporalio.workflow` (#2008, fixed by #2010), which load and test distribution made intermittent.
    The return value does not go through logging, and an interpreter that does not end returns nothing.
    """

    def __init__(self) -> None:
        super().__init__()
        self.returned: list[Any] = []

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        returned = self.returned

        class _Returned(WorkflowInboundInterceptor):
            async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
                result = await super().execute_workflow(input)
                returned.append(result)
                return result

        return _Returned


async def test_an_interpreter_left_over_on_a_trial_item_replays_to_its_end() -> None:
    """The history of an interpreter stuck on a trial item, recorded before the fix, replays without a
    nondeterminism error and reaches the end: the workflows left open in production close."""
    from temporalio.client import WorkflowHistory
    from temporalio.worker import Replayer
    from tranibor_orchestrator.workflows import ALL_WORKFLOWS

    payload = json.loads(LEFT_OVER.read_text(encoding="utf-8"))
    kinds = [event["eventType"] for event in payload["events"]]
    assert "EVENT_TYPE_WORKFLOW_TASK_FAILED" in kinds, "the history is the stuck one"
    assert "EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED" not in kinds
    history = WorkflowHistory.from_json(LEFT_OVER.stem, payload)

    witness = _EndWitness()
    result = await Replayer(workflows=ALL_WORKFLOWS, interceptors=[witness]).replay_workflow(history)

    assert result.replay_failure is None
    assert len(witness.returned) == 1, "the replay did not reach the end"
    assert witness.returned[0]["state"] == "trial" and witness.returned[0]["stopped"] is True
