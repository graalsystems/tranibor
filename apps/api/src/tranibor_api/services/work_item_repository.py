# SPDX-License-Identifier: Apache-2.0
"""A work item's target repository, where a caller can be answered (ADR 0124).

`fields.repository` names one of the project's repositories; absent, the work item works on the
primary. A caller who gives it — a person, the API, a door, a trigger, a schedule, a fan-out — is
refused at once when it names none of them (422), and a person may not move a work item that is
fixed to its repository (409). A tracker label is copied as written: the tracker cannot be answered,
and the first stage that touches git refuses an unknown name (the orchestrator's `RepositoryRefused`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_contracts import ProjectConfig
from tranibor_core.domain import REPOSITORY_FIELD
from tranibor_core.repositories import asked, fixed, fixed_name, from_labels, unknown

from ..db.models import Project, Run, WorkItem
from ..errors import conflict, unprocessable


def config_of(project: Project) -> ProjectConfig | None:
    """The project's configuration as the model reads it; `None` for one stored before a field it no
    longer accepts (a work item is still readable then)."""
    try:
        return ProjectConfig.model_validate(project.config or {})
    except ValidationError:
        return None


def refuse_unknown_repository(project: Project, fields: Mapping[str, Any]) -> None:
    """A `fields.repository` that names no repository of the project: 422, with the names it holds."""
    if fields.get(REPOSITORY_FIELD) is None:
        return
    config = config_of(project)
    why = unknown(config, fields[REPOSITORY_FIELD]) if config is not None else None
    if why is not None:
        raise unprocessable(why, [{"loc": ["fields", REPOSITORY_FIELD], "msg": "unknown repository"}])


def birth_fields(project: Project, fields: Mapping[str, Any], labels: Iterable[str]) -> dict[str, Any]:
    """The fields a work item is born with: a given `repository` is checked; without one, a
    `repository:<name>` label names it, copied as written (ADR 0124 §2)."""
    refuse_unknown_repository(project, fields)
    if fields.get(REPOSITORY_FIELD) is None and (labelled := from_labels(labels)) is not None:
        # A project without a repository has nothing to choose: its label is a tag like any other.
        config = config_of(project)
        if config is not None and config.delivery_section.has_repo:
            return {**fields, REPOSITORY_FIELD: labelled}
    return dict(fields)


async def _cloned(session: AsyncSession, items: Sequence[WorkItem]) -> set[str]:
    """The ids of the work items one of whose runs cloned a repository (its recorded
    `StageInput.delivery.repo`): one query for all of them, none for none."""
    if not items:
        return set()
    rows = await session.execute(
        select(Run.work_item_id, Run.stage_input).where(Run.work_item_id.in_([item.id for item in items]))
    )
    return {
        str(work_item_id)
        for work_item_id, recorded in rows
        if isinstance(recorded, dict) and (recorded.get("delivery") or {}).get("repo")
    }


def _asks_the_runs(config: ProjectConfig, item: WorkItem) -> bool:
    """Whether only the work item's runs can still say it is fixed: nothing else said it, the project
    has a choice of repositories and no pull request names one."""
    return (
        fixed(item.documents) is None
        and config.delivery_section.has_repo
        and bool(config.delivery_section.repositories)
        and not item.pr_url
    )


def _fixed_with(config: ProjectConfig, item: WorkItem, *, cloned: bool) -> str | None:
    if (kept := fixed(item.documents)) is not None or not config.delivery_section.has_repo:
        return kept
    return fixed_name(config, item.fields, item.documents, has_pull_request=bool(item.pr_url), cloned=cloned)


async def fixed_repository(session: AsyncSession, config: ProjectConfig, item: WorkItem) -> str | None:
    """The repository the work item can no longer leave (ADR 0124 §3), `None` while it may change."""
    # A run that cloned before its project held a second repository fixes it too; asked only where a
    # choice exists and nothing else said it.
    cloned = _asks_the_runs(config, item) and item.id in await _cloned(session, [item])
    return _fixed_with(config, item, cloned=cloned)


async def refuse_repository_change(
    session: AsyncSession, project: Project, item: WorkItem, change: Mapping[str, Any]
) -> None:
    """A person's change of `fields.repository` (`PATCH`, a task's values): an unknown name is 422, and
    another repository than the one the work item is fixed to is 409."""
    if REPOSITORY_FIELD not in change:
        return
    refuse_unknown_repository(project, change)
    config = config_of(project)
    if config is None or not config.delivery_section.has_repo:
        return
    kept = await fixed_repository(session, config, item)
    wanted = change[REPOSITORY_FIELD] or config.delivery_section.primary_name
    if kept is not None and wanted != kept:
        raise conflict(
            f"the work item works on `{kept}` since its first step on a repository: one work item works on "
            "one repository; open another work item for the other one (ADR 0124)"
        )


async def repository_view(session: AsyncSession, project: Project, item: WorkItem) -> tuple[str | None, bool]:
    """What the work item's page says: the repository it works on, and whether that is fixed."""
    return (await repository_views(session, project, [item]))[item.id]


async def repository_views(
    session: AsyncSession, project: Project, items: Sequence[WorkItem]
) -> dict[str, tuple[str | None, bool]]:
    """`repository_view` of each work item of the project, by id: at most one query for a page (#2285)."""
    config = config_of(project)
    if config is None or not config.delivery_section.has_repo:
        return {item.id: (None, False) for item in items}
    cloned = await _cloned(session, [item for item in items if _asks_the_runs(config, item)])
    views: dict[str, tuple[str | None, bool]] = {}
    for item in items:
        kept = _fixed_with(config, item, cloned=item.id in cloned)
        views[item.id] = (
            kept or asked(item.fields) or config.delivery_section.primary_name,
            kept is not None,
        )
    return views


__all__ = [
    "birth_fields",
    "config_of",
    "fixed_repository",
    "refuse_repository_change",
    "refuse_unknown_repository",
    "repository_view",
    "repository_views",
]
