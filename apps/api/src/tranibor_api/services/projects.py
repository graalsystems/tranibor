# SPDX-License-Identifier: Apache-2.0
"""Projection of a project: its DTO and its statistics for the month."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_core import (
    utcnow,
)

from ..db.models import (
    CostLedger,
    Organization,
    Project,
    Run,
    WorkItem,
)
from ..extensions import project_figures
from ..schemas import (
    ProjectDto,
    ProjectStats,
)
from .definitions import active_policy, active_workflow, policy_engine


def config_revision(name: str, config: dict[str, Any] | None) -> str:
    """An opaque digest of a project's name and configuration: what `PATCH /projects/{id}` takes back as
    `base_revision` to refuse a write made from a stale read (S47-06)."""
    document = json.dumps({"name": name, "config": config or {}}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode()).hexdigest()[:32]


async def project_dto(session: AsyncSession, project: Project, org_slug: str | None = None) -> ProjectDto:
    if org_slug is None:
        org = await session.get(Organization, project.org_id)
        org_slug = org.slug if org else ""
    workflow = await active_workflow(session, project.id)
    policy = await active_policy(session, project.id)
    return ProjectDto(
        id=project.id,
        slug=project.slug,
        org=org_slug,
        name=project.name,
        template_ref=project.template_ref,
        status=project.status,
        config=project.config,
        config_revision=config_revision(project.name, project.config),
        workflow_name=workflow.name if workflow else None,
        policy_name=policy.name if policy else None,
        stats=await project_stats(session, project),
        # The fields a plugin fills (`x-plugin`, ADR 0181 round D): `has_release_train` is
        # `software-delivery`'s; without its extension it keeps its default.
        **await project_figures(session, project, "Project"),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


async def project_stats(session: AsyncSession, project: Project) -> ProjectStats:
    since = utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    active = (
        await session.execute(
            select(func.count())
            .select_from(WorkItem)
            .where(WorkItem.project_id == project.id, WorkItem.closed_at.is_(None))
        )
    ).scalar_one()
    cost = (
        await session.execute(
            select(func.coalesce(func.sum(CostLedger.cost_eur), 0.0)).where(
                CostLedger.project_id == project.id, CostLedger.ts >= since
            )
        )
    ).scalar_one()
    return ProjectStats(
        active_work_items=int(active),
        cost_month_eur=round(float(cost), 4),
        first_pass_acceptance_rate=await first_pass_merge_rate(session, project.id),
        # `trains_pending` is the `software-delivery` plugin's (ADR 0181 round D).
        **await project_figures(session, project, "ProjectStats"),
    )


async def first_pass_merge_rate(session: AsyncSession, project_id: str) -> float | None:
    """Share of the closed work items whose implementation took a single attempt."""
    items = (
        (
            await session.execute(
                select(WorkItem.id).where(WorkItem.project_id == project_id, WorkItem.closed_at.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    if not items:
        return None
    good = 0
    for item_id in items:
        attempts = (
            await session.execute(
                select(func.max(Run.attempt)).where(
                    Run.work_item_id == item_id, Run.stage_role == "implement"
                )
            )
        ).scalar_one()
        if attempts is None or attempts <= 1:
            good += 1
    return round(good / len(items), 4)


async def active_project_slugs(*, keeping_memory: bool = False) -> list[str]:
    """The active projects' slugs, across organisations; with `keeping_memory`, only those whose
    policy keeps memory (S51-02)."""
    return [slug for slug, _ in await active_projects(keeping_memory=keeping_memory)]


async def active_projects(*, keeping_memory: bool = False) -> list[tuple[str, str]]:
    """`(slug, organisation id)` of the active projects, across organisations; with `keeping_memory`, only
    those whose policy keeps memory (S51-02). The orchestrator worker starts one loop per project, inside
    its organisation's scope (ADR 0224): this is its one entry into the API for both loops (ADR 0099
    counts each import the orchestrator makes of it)."""
    from ..db.session import session_scope

    async with session_scope(orgs="*") as session:
        rows = (
            (
                await session.execute(
                    select(Project.id, Project.slug, Project.org_id).where(Project.status == "active")
                )
            )
            .tuples()
            .all()
        )
        if not keeping_memory:
            return [(slug, org_id) for _, slug, org_id in rows]
        return [
            (slug, org_id)
            for project_id, slug, org_id in rows
            if (await policy_engine(session, project_id)).memory_enabled()
        ]
