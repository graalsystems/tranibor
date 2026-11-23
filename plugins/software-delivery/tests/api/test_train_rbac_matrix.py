# SPDX-License-Identifier: Apache-2.0
"""The release train's rows of the RBAC matrix, generated like the core's (`apps/api/tests/
test_rbac_matrix.py`): the expectation is DEDUCED from `ROLE_PERMISSIONS` and from the permission each route
declares, for every role under both spellings. The train's routes are the `software-delivery` plugin's since
round D of ADR 0181.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from tranibor_contracts import Role
from tranibor_testkit.api import login

#: Every role, under its neutral name (ADR 0181 C1).
ROLES = [
    Role.VIEWER,
    Role.CONTRIBUTOR,
    Role.APPROVER,
    Role.PROJECT_OWNER,
    Role.ORG_ADMIN,
]


def _routes(project_id: str) -> list[tuple[str, str, str, Any]]:
    """(expected permission, method, path, VALID body): an invalid body would give a 422 before the RBAC."""
    return [
        ("train:operate", "POST", f"/api/v1/projects/{project_id}/trains/prod/freeze", {"reason": "test"}),
    ]


async def _member(email: str, role: Role) -> None:
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


@pytest.mark.parametrize("role", ROLES, ids=[str(r) for r in ROLES])
async def test_each_role_gets_exactly_its_train_permissions(
    client: AsyncClient, project: dict[str, Any], role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.rbac import ROLE_PERMISSIONS, Permission

    # the dev sign-in must neither demote nor promote: the role is set in the database, then we
    # sign in as that member (no dev admin, so the default `contributors` is ignored)
    monkeypatch.setenv("TRANIBOR_DEV_ADMIN_EMAILS", "admin@northwind.dev")
    reset_settings_cache()
    email = f"{role}@northwind.dev"
    await _member(email, role)
    async with AsyncClient(transport=ASGITransport(app=client._transport.app), base_url="http://test") as c:  # type: ignore[attr-defined]
        await login(c, email)
        me = (await c.get("/api/v1/me")).json()
        assert {m["role"] for m in me["memberships"]} == {str(role)}, "the dev sign-in touched the role"
        gaps: list[str] = []
        for permission, method, path, body in _routes(project["id"]):
            expected = Permission(permission) in ROLE_PERMISSIONS[role]
            response = await c.request(method, path, json=body)
            assert response.status_code != 404, f"{method} {path} is not served: {response.text[:200]}"
            refused = response.status_code == 403
            if refused == expected:
                gaps.append(
                    f"{method} {path} -> {response.status_code} (expected {'allowed' if expected else '403'})"
                )
            assert response.status_code != 422, f"invalid body for {method} {path}: {response.text[:200]}"
        assert not gaps, "\n".join(gaps)
    reset_settings_cache()
