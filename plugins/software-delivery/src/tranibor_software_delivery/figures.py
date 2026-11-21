# SPDX-License-Identifier: Apache-2.0
"""The figures of a project's page that are software delivery's (ADR 0181, round D):
`Project.has_release_train`
and `ProjectStats.trains_pending`, which `openapi.yaml` marks `x-plugin: software-delivery`. The core keeps
the fields at their default and fills them with what the extension declares here
(`extensions.declare_project_figure`)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from tranibor_api.db.models import Release, WorkflowDef

#: The release statuses of a train that has not delivered yet.
PENDING = ("collecting", "departing", "staging", "awaiting_approval", "promoting")


async def has_release_train(session: Any, project: Any) -> bool:
    """Whether an active workflow of the project has a `via: release_train` transition: the engine
    starts a train only for such a transition, so a project without one has no train (S30-07)."""
    documents = (
        await session.execute(
            select(WorkflowDef.json_doc).where(
                WorkflowDef.project_id == project.id, WorkflowDef.is_active.is_(True)
            )
        )
    ).scalars()
    return any(
        transition.get("via") == "release_train"
        for document in documents
        for transition in (document or {}).get("transitions") or []
    )


async def trains_pending(session: Any, project: Any) -> int:
    """The project's releases a train is still carrying."""
    count = (
        await session.execute(
            select(func.count())
            .select_from(Release)
            .where(Release.project_id == project.id, Release.status.in_(PENDING))
        )
    ).scalar_one()
    return int(count)
