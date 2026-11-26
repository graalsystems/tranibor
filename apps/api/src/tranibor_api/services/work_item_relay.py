# SPDX-License-Identifier: Apache-2.0
"""The relay of new work items' interpreters (ADR 0085, #2291).

A work item's interpreter is started once the row that justifies it has COMMITTED
(`start_work_item` with its session): before #2291 it was started inside the creating transaction,
and the worker's first activity could read before the row was visible, or run on a row a failed
commit rolled back. The start that follows the commit is best effort: between the two, Temporal may be
down or the process killed. This relay makes that start again.

A candidate is a work item whose interpreter was asked for (`temporal_wf_id` written in the creating
transaction), still open, created more than `GRACE` ago (younger ones are left to their own commit's
start) and less than `WINDOW` ago, on which nothing has run yet (no run, no human request). Starting is
idempotent: the id is deterministic and the start refuses a duplicate of a running or completed
interpreter (`ALLOW_DUPLICATE_FAILED_ONLY`), so an interpreter that did start, but waits in a state that
writes neither, is asked again for at most `WINDOW` and starts nothing. Every replica runs it, without a
lock, as the actions' relay does.

What it does not do: start a work item created without its interpreter (`start=False`: no
`temporal_wf_id`), a trial's work item, or one lost for longer than `WINDOW` (a platform down that long
is an incident: `POST /work-items/{id}/start` starts one by hand).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import exists, select
from tranibor_core import utcnow
from tranibor_core.temporal_connection import organisation_scope

from ..db.models import HumanRequest, Project, Run, WorkItem
from ..db.session import ALL_ORGS, session_scope
from ..logging import get_logger

logger = get_logger("tranibor.work_items.relay")

#: A work item younger than this is left to the start that follows its own commit.
GRACE = timedelta(seconds=30)
#: A work item older than this is no longer looked at.
WINDOW = timedelta(minutes=15)
BATCH = 100


async def start_unstarted_work_items(
    *, grace: timedelta = GRACE, window: timedelta = WINDOW, batch: int = BATCH
) -> list[str]:
    """Start (idempotently) the interpreter of each new work item on which nothing has run yet.

    Returns the work items whose start Temporal accepted or already had. One that fails is tried again
    at the next pass, while it is in the window."""
    from ..temporal import get_temporal
    from .agent_lifecycle import TRIAL_STATE
    from .tickets import interpreter_payload

    now = utcnow()
    async with session_scope(orgs=ALL_ORGS) as session:
        rows = (
            await session.execute(
                select(WorkItem, Project)
                .join(Project, WorkItem.project_id == Project.id)
                .where(
                    WorkItem.temporal_wf_id.is_not(None),
                    WorkItem.closed_at.is_(None),
                    WorkItem.state != TRIAL_STATE,
                    WorkItem.created_at <= now - grace,
                    WorkItem.created_at > now - window,
                    ~exists().where(Run.work_item_id == WorkItem.id),
                    ~exists().where(HumanRequest.work_item_id == WorkItem.id),
                )
                .order_by(WorkItem.created_at)
                .limit(batch)
            )
        ).all()
        candidates = [
            (item.id, str(item.temporal_wf_id), project.org_id, interpreter_payload(project, item))
            for item, project in rows
        ]
    started: list[str] = []
    for item_id, workflow_id, org_id, payload in candidates:
        try:
            with organisation_scope(org_id):
                await get_temporal().start_interpreter(workflow_id, payload)
        except Exception as error:
            logger.warning(
                "work_item.relay.failed", work_item=item_id, error=f"{type(error).__name__}: {error}"[:300]
            )
            continue
        started.append(item_id)
    return started
