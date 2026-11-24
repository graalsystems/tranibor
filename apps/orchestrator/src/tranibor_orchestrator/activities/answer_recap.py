# SPDX-License-Identifier: Apache-2.0
"""What an agent reads when a person's answer sends its work item back to it (ADR 0223).

Two doors, one text: the same run resumed in its kept sandbox (`resume_run`) and a stage replayed in a
new run (`start_run`). Both put the answer, with who gave it and when, at the head of the playbook. A
replayed stage has no conversation to reload (a new run, a new workspace), so it also gets a recap of
the run that asked, built from that run's journal. A resumed run builds its own recap in the runner, and
only when its agent cannot reload its conversation.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from tranibor_api.db.models import Run, RunEvent
from tranibor_core.recap import build_recap

#: What the agent reads first when its run goes on — the same heading for both doors.
ANSWER_HEADING = "## A person answered the question that stopped this work item"
#: How many of the earlier run's last events are read: the end of a journal is what the recap keeps.
EVENTS_READ = 3_000


def answer_of(documents: dict[str, Any]) -> str:
    """The answer block (question, answer, who and when), or `""` when nobody answered."""
    answer = str((documents or {}).get("human_answer") or "").strip()
    if not answer:
        return ""
    by = str(documents.get("human_answer_by") or "").strip()
    return f"{answer}\n\n{by}" if by else answer


def with_the_answer(documents: dict[str, Any], prompt: str) -> str:
    """`prompt` under the answer, when there is one."""
    answer = answer_of(documents)
    return f"{ANSWER_HEADING}\n\n{answer}\n\n---\n\n{prompt}" if answer else prompt


async def recap_of_the_run_that_asked(session: Any, item: Any, transition_id: str, run_id: str) -> str:
    """The recap of the latest earlier run of this work item on this transition, for a stage replayed
    after an answer; `""` when nobody answered, there is no such run, or it did nothing worth recalling."""
    if not str((item.documents or {}).get("human_answer") or "").strip():
        return ""
    previous = (
        await session.execute(
            select(Run)
            .where(Run.work_item_id == item.id, Run.transition_id == transition_id, Run.id != run_id)
            .where(Run.started_at.is_not(None))
            .order_by(Run.started_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if previous is None:
        return ""
    rows = (
        await session.execute(
            select(RunEvent)
            .where(RunEvent.run_id == previous.id)
            .order_by(RunEvent.seq.desc())
            .limit(EVENTS_READ)
        )
    ).scalars()
    events = [{"type": row.type, "payload": row.payload} for row in reversed(list(rows))]
    return build_recap(events, stage=transition_id, task=str(item.title or "")).text
