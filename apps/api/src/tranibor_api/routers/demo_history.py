# SPDX-License-Identifier: Apache-2.0
"""Take a demonstration history into a project (ADR 0216)."""

from __future__ import annotations

from fastapi import APIRouter
from tranibor_contracts import DemoHistory
from tranibor_core.production import demo_history_import_refusal

from ..config import get_settings
from ..deps import Db, ProjectCtx
from ..errors import conflict
from ..rbac import Permission
from ..schemas import DemoHistoryImport
from ..services.demo_history import import_history

router = APIRouter(tags=["projects"])


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
    ctx.require(Permission.DEMO_HISTORY_IMPORT)
    settings = get_settings()
    refusal = demo_history_import_refusal(environment=settings.env, enabled=settings.demo_history_import)
    if refusal is not None:
        raise conflict(refusal)
    return await import_history(session, ctx.principal, ctx.project, body)
