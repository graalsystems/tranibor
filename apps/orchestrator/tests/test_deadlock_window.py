# SPDX-License-Identifier: Apache-2.0
"""The orchestrator suite's workers give a workflow activation 10 s, not Temporal's 2 s (issue #2307).

On runners shared by many CI runs, replaying a history for a query or a resumed run took longer than
2 s: four tests failed on `TMPRL1101 Potential deadlock detected`, never on a loop. A workflow whose
first activation holds the thread for 3 s stands for that slow replay: the SDK's window fails it, the
suite's window lets it finish. The replay suite and production keep the SDK's 2 s.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from temporalio import workflow

#: Longer than the SDK's window, far shorter than the suite's.
HOLD_SECONDS = 3.0


@workflow.defn(name="SlowFirstActivation", sandboxed=False)
class SlowFirstActivation:
    """Holds its first activation's thread, as a long replay does, then returns."""

    @workflow.run
    async def run(self) -> str:
        time.sleep(HOLD_SECONDS)
        return "done"


def test_the_window_sits_between_the_sdk_s_and_a_real_deadlock() -> None:
    from tranibor_orchestrator.testing import SDK_DEADLOCK_SECONDS
    from tranibor_testkit.orchestrator import SUITE_DEADLOCK_SECONDS

    assert SDK_DEADLOCK_SECONDS < HOLD_SECONDS < SUITE_DEADLOCK_SECONDS


@pytest.fixture
def slow_workflow_carried(monkeypatch: pytest.MonkeyPatch) -> None:
    """The suite's workers carry the platform's workflows: this one is added before they are built."""
    from tranibor_orchestrator import extension_parts

    platform_workflows = extension_parts.all_workflows
    monkeypatch.setattr(
        extension_parts, "all_workflows", lambda: [*platform_workflows(), SlowFirstActivation]
    )


async def test_the_suite_s_workers_let_a_slow_activation_finish(
    slow_workflow_carried: None, worker_factory: Any, temporal_env: Any
) -> None:
    async with worker_factory("deadlock-window"):
        result = await asyncio.wait_for(
            temporal_env.client.execute_workflow(
                SlowFirstActivation.run, id="deadlock-window-suite", task_queue="deadlock-window"
            ),
            timeout=60,
        )
    assert result == "done"


async def test_the_sdk_s_window_still_fails_it(temporal_env: Any) -> None:
    """Without the suite's window, the same activation is a deadlock: the test above proves the window."""
    from temporalio.api.enums.v1 import EventType
    from tranibor_orchestrator.testing import wait_for_event, workers_by_queue

    async with workers_by_queue(
        temporal_env.client,
        activities=[],
        workflows=[SlowFirstActivation],
        workflow_queue="deadlock-window-sdk",
    ):
        handle = await temporal_env.client.start_workflow(
            SlowFirstActivation.run, id="deadlock-window-sdk", task_queue="deadlock-window-sdk"
        )
        try:
            await wait_for_event(handle, EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED, seconds=30)
            history = await handle.fetch_history()
        finally:
            await handle.terminate("the test has seen the deadlock")
    failed = [
        event.workflow_task_failed_event_attributes.failure.message
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_WORKFLOW_TASK_FAILED
    ]
    assert any("TMPRL1101" in message for message in failed), failed


def test_a_moved_sdk_attribute_is_refused_not_ignored() -> None:
    from tranibor_orchestrator.testing import set_deadlock_seconds

    class Moved:
        _workflow_worker = object()

    with pytest.raises(RuntimeError, match="no longer keeps its deadlock window"):
        set_deadlock_seconds(Moved(), 10)
