# SPDX-License-Identifier: Apache-2.0
"""Judging a finding's type against the work item's workflow (ADR 0217).

The contracts read a finding's `type` as a name; here, where the work item and so its workflow are known,
it is either one the core or a plugin declares, or one the workflow declares in `metadata.finding_types`.
Any other is refused with a 422 that names the workflow and the types allowed.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_contracts import Finding, Workflow
from tranibor_core.finding_types import refusal, title_of

from ..db.models import WorkItem
from ..errors import unprocessable
from .definitions import work_item_workflow, workflow_model


async def workflow_of(session: AsyncSession, item: WorkItem | None) -> Workflow | None:
    """The workflow the work item was born in; `None` for a finding with no work item."""
    if item is None:
        return None
    return workflow_model(await work_item_workflow(session, item))


def refuse_undeclared(workflow: Workflow | None, findings: Iterable[Finding]) -> None:
    """Raise a 422 for the first finding whose type or severity `workflow` does not allow."""
    for finding in findings:
        why = refusal(workflow, str(finding.type), str(finding.severity))
        if why is not None:
            raise unprocessable(why)


async def titles_for(
    session: AsyncSession, items: dict[str, WorkItem], pairs: Iterable[tuple[str, str]]
) -> dict[tuple[str, str], str | None]:
    """The title each `(work item id, type)` pair has in its work item's workflow, one load per version."""
    workflows: dict[str, Workflow | None] = {}
    titles: dict[tuple[str, str], str | None] = {}
    for item_id, finding_type in pairs:
        item = items.get(item_id)
        if item is None:
            titles[(item_id, finding_type)] = None
            continue
        key = item.workflow_def_id or f"default:{item.project_id}"
        if key not in workflows:
            workflows[key] = await workflow_of(session, item)
        titles[(item_id, finding_type)] = title_of(workflows[key], finding_type)
    return titles
