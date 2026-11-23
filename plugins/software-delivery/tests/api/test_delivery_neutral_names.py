# SPDX-License-Identifier: Apache-2.0
"""Round C1 of ADR 0181 with the `software-delivery` plugin: only the neutral names (the former ones left on
2026-11-23) on what the plugin serves or declares.

The delivery half of `apps/api/tests/test_neutral_names.py`: an `approver` row operates the release train (the
plugin's routes since round D of ADR 0181), the catalogue's `needs_workspace` on the plugin's own workspace
kinds (`scm`, `ci`, `cd`), and `dev-feature`'s process view on the plugin's diff gates.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tranibor_contracts import Role
from tranibor_testkit.api import login


async def _member(email: str, role: Role) -> None:
    """A row of the organisation `northwind` with the role given."""
    from sqlalchemy import select
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "northwind"))
        ).scalar_one()
        user = User(email=email, display_name=email.split("@", maxsplit=1)[0])
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=org.id, role=str(role)))


async def test_a_row_under_the_neutral_spelling_operates_the_train(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An `approver` row approves releases."""
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_DEV_ADMIN_EMAILS", "admin@northwind.dev")
    reset_settings_cache()
    await _member("neutral@northwind.dev", Role.APPROVER)
    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url="http://test") as c:  # type: ignore[attr-defined]
        await login(c, "neutral@northwind.dev")
        me = (await c.get("/api/v1/me")).json()
        assert {m["role"] for m in me["memberships"]} == {"approver"}, "the dev sign-in left the row alone"
        frozen = await c.post(
            f"/api/v1/projects/{project['id']}/trains/prod/freeze", json={"reason": "round C1"}
        )
        assert frozen.status_code not in (403, 422), frozen.text
    reset_settings_cache()


async def test_the_catalogue_carries_only_the_neutral_name_on_the_plugin_s_workspace_kinds(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    entries = (await client.get("/api/v1/orgs/northwind/workflow-catalogue?limit=200")).json()["items"]
    assert entries, "the catalogue lists the shipped workflows"
    assert not any("needs_repository" in e for e in entries)
    assert any(e["needs_workspace"] for e in entries) and not all(e["needs_workspace"] for e in entries)


def test_the_process_view_carries_only_the_neutral_names_on_the_plugin_s_diff_gates() -> None:
    import yaml
    from tranibor_contracts import Workflow
    from tranibor_core.dsl import template_yaml
    from tranibor_core.dsl.flow import artifact_flow
    from tranibor_core.dsl.process import to_process

    workflow = Workflow.model_validate(yaml.safe_load(template_yaml("dev-feature")))
    steps = to_process(workflow)
    assert not any("writes_branch" in s for s in steps)
    assert any(s["writes_workspace"] for s in steps)
    flows = artifact_flow(workflow)
    assert flows and not any("in_pull_request" in a for a in flows)
