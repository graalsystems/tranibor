# SPDX-License-Identifier: Apache-2.0
"""A transition a deployment train carries (`via: release_train`).

Methods moved as they were from `workflows/interpreter.py`; the handlers stay on the class. The train is
the `software-delivery` plugin's (ADR 0181, round D): the interpreter boards it by calling the activity
`signal_train` by name (`carriers.BOARD_ACTIVITY`), which the plugin's extension registers on the workers,
and waits for the deployment events its train relays. The commands are the ones every recorded history
carries; the interpreter reads no registry, so a history replays without the extension."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow

from ..scheduling import execute_on_its_queue

with workflow.unsafe.imports_passed_through():
    from tranibor_core import WorkflowEngine
    from tranibor_core.carriers import BOARD_ACTIVITY, boarding_approval, boarding_env

from ..interpreter import TRAIN_PER_ENVIRONMENT
from .base import InterpreterBase
from .helpers import DEFAULT_RETRY, InterpreterInput, _for_this_environment, wait_signal


class TrainsMixin(InterpreterBase):
    """A transition a deployment train carries."""

    async def _run_train(self, params: InterpreterInput, engine: WorkflowEngine, transition: Any) -> Any:
        """Board the ticket, then wait for ITS train to have delivered: `finish_release` and `rollback`
        warn it (ADR 0041). The approval the workflow requires travels with it up to the departure."""
        env = boarding_env(transition)
        per_environment = workflow.patched(TRAIN_PER_ENVIRONMENT)
        boarding: dict[str, Any] = {
            "project_id": params.project_id,
            "project_slug": params.project_slug,
            "work_item_id": params.work_item_id,
            "env": env,
        }
        if per_environment:
            boarding["approval"] = dict(boarding_approval(engine.wf, transition))
        await execute_on_its_queue(
            BOARD_ACTIVITY["release_train"],
            boarding,
            start_to_close_timeout=timedelta(minutes=1),
            retry_policy=DEFAULT_RETRY,
        )
        timeout = timedelta(hours=transition.timeout_hours or 72)
        waited = timedelta()
        step = timedelta(minutes=10)
        while waited < timeout and not self.stopped:
            await wait_signal(lambda: bool(self.inbox) or self.stopped, step)
            while self.inbox:
                event = self.inbox.popleft()
                if per_environment and not _for_this_environment(event, env):
                    # A finished staging is not prod: a ticket that takes two trains in a row
                    # used to take the second as delivered as soon as the first was.
                    self._absorb(event)
                    continue
                if event.get("type") in {"cd.rollout.completed", "cd.app.synced"}:
                    return engine.after_carrier(transition, ok=True)
                if event.get("type") in {"cd.rollout.aborted", "cd.app.degraded"}:
                    return engine.after_carrier(transition, ok=False)
                self._absorb(event)
            waited += step
        return engine.after_carrier(transition, ok=False)
