# SPDX-License-Identifier: Apache-2.0
"""Take a demonstration history into a project, or remove it (ADR 0216)."""

from __future__ import annotations

from fastapi import APIRouter
from tranibor_contracts import DemoHistory
from tranibor_core.production import demo_history_import_refusal

from ..config import get_settings
from ..deps import Db, ProjectCtx
from ..errors import conflict
from ..rbac import Permission
from ..schemas import DemoHistoryImport, DemoHistoryRemoval
from ..services.demo_history import import_history, remove_history

router = APIRouter(tags=["projects"])


def _gate(ctx: ProjectCtx) -> None:
    """The organisation administrator only, on an instance that opted in and is not production."""
    ctx.require(Permission.DEMO_HISTORY_IMPORT)
    settings = get_settings()
    refusal = demo_history_import_refusal(environment=settings.env, enabled=settings.demo_history_import)
    if refusal is not None:
        raise conflict(refusal)


@router.post(
    "/projects/{id}/demo-history",
    response_model=DemoHistoryImport,
    operation_id="importDemoHistory",
)
async def import_demo_history(ctx: ProjectCtx, body: DemoHistory, session: Db) -> DemoHistoryImport:
    """A declared history of work items, runs, costs, decisions and findings, marked as imported.

    The organisation administrator only, and only on an instance that opted in and is not production:
    the gate is checked before the document is read (ADR 0216). Idempotent by the document's external ids.
    """
    _gate(ctx)
    return await import_history(session, ctx.principal, ctx.project, body)


@router.delete(
    "/projects/{id}/demo-history",
    response_model=DemoHistoryRemoval,
    operation_id="deleteDemoHistory",
)
async def delete_demo_history(ctx: ProjectCtx, session: Db) -> DemoHistoryRemoval:
    """Remove what imports wrote into the project: the rows marked `imported`, and nothing else.

    The import's gate: the organisation administrator, on an instance that opted in and is not production.
    Audited as `demo_history.delete`; a second call removes nothing.
    """
    _gate(ctx)
    return await remove_history(session, ctx.principal, ctx.project)
