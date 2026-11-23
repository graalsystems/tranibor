# SPDX-License-Identifier: Apache-2.0
"""A seeded cost ledger, shared by the cost report's tests and the display currency's (S29-02, S29-05)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


async def seed_cost_ledger(project_id: str) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import AssistantInstallation, CostLedger, Organization, WorkItem
    from tranibor_api.db.session import session_scope

    now = datetime.now(UTC)
    async with session_scope() as session:
        one = WorkItem(project_id=project_id, tracker_key="B-1", title="One", state="ready")
        two = WorkItem(project_id=project_id, tracker_key="B-2", title="Two", state="ready")
        northwind = select(Organization.id).where(Organization.slug == "northwind")
        org_id = (await session.execute(northwind)).scalar_one()
        concierge, analyst = (
            AssistantInstallation(org_id=org_id, slug=slug, plugin="p", plugin_version="1",
                                  manifest_digest="d", assistant_name=slug)
            for slug in ("concierge", "analyst")
        )  # fmt: skip
        session.add_all([one, two, concierge, analyst])
        await session.flush()

        def line(item: Any, usd: float, **kw: Any) -> CostLedger:
            return CostLedger(
                project_id=project_id,
                work_item_id=item.id if item else None,
                ts=kw.pop("ts", now),
                cost_usd=usd,
                cost_eur=usd * 0.5,
                tokens_in=10,
                tokens_out=5,
                **kw,
            )

        session.add_all(
            [
                line(one, 4.0, kind="model", model="m-big", backend="claude-code", stage_role="implement",
                     size="M", agent_slug="coordinator", agent_version=1, workflow_name="delivery",
                     installation_id=concierge.id),
                line(one, 2.0, kind="model", model="m-small", backend="claude-code", stage_role="review",
                     size="M", agent_slug="reviewer", agent_version=1, workflow_name="delivery",
                     installation_id=analyst.id),
                line(two, 1.0, kind="tool", model="crm_export", stage_role="implement", size="S",
                     agent_slug="coordinator", agent_version=1, workflow_name="onboarding", origin="script"),
                line(None, 3.0, kind="model", model="m-big", stage_role="plan",
                     ts=now - timedelta(days=45)),  # outside the default 30 days
            ]
        )  # fmt: skip
