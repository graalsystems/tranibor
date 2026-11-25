"""What a fuzz of the API without credentials, and with a second organisation's, found open.

Seven routes of the catalogues and the editor answered an anonymous caller (the contract's default is
"authenticated"), and a project id of another organisation was answered 403 naming the project's
`org/slug` instead of 404.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from .conftest import login

#: Every route that has no business answering without a session, and a request it would otherwise serve.
GUARDED: list[tuple[str, str, dict[str, Any] | None]] = [
    ("GET", "/api/v1/connectors/types", None),
    ("GET", "/api/v1/platform/models", None),
    ("GET", "/api/v1/templates", None),
    ("GET", "/api/v1/templates/github-aca", None),
    ("GET", "/api/v1/workflows/templates", None),
    ("POST", "/api/v1/workflows/validate", {"yaml": "x: 1"}),
    ("POST", "/api/v1/workflows/edit", {"yaml": "x: 1", "operations": [{}]}),
]


@pytest.mark.parametrize(("method", "path", "body"), GUARDED)
async def test_the_catalogues_and_the_editor_refuse_an_anonymous_caller(
    client: AsyncClient, method: str, path: str, body: dict[str, Any] | None
) -> None:
    anonymous = await client.request(method, path, json=body)
    assert anonymous.status_code == 401, anonymous.text


@pytest.mark.parametrize(("method", "path", "body"), GUARDED[:6])
async def test_a_signed_in_caller_still_reads_them(
    client: AsyncClient, admin: str, method: str, path: str, body: dict[str, Any] | None
) -> None:
    served = await client.request(method, path, json=body)
    assert served.status_code == 200, served.text


async def test_a_project_of_another_organisation_does_not_exist_for_a_caller_with_no_role_there(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        other = Organization(slug="acme", name="Acme")
        user = User(email="boss@acme.test", display_name="boss")
        session.add_all([other, user])
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=other.id, role="org_admin"))
    # The development sign-in grants a role in every organisation of the bench unless one is named.
    monkeypatch.setenv("TRANIBOR_OIDC_DEFAULT_ORG", "acme")
    reset_settings_cache()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=client._transport.app),  # type: ignore[attr-defined]
            base_url="http://test",
        ) as foreign:
            await login(foreign, "boss@acme.test")
            for suffix in ("", "/models", "/work-items"):
                answer = await foreign.get(f"/api/v1/projects/{project['id']}{suffix}")
                assert answer.status_code == 404, (suffix, answer.text)
                assert "northwind" not in answer.text and project["slug"] not in answer.text
    finally:
        reset_settings_cache()


async def test_a_member_of_the_organisation_without_the_right_still_gets_a_403(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        org = (
            await session.execute(select(Organization).where(Organization.slug == "northwind"))
        ).scalar_one()
        user = User(email="reader@northwind.dev", display_name="reader")
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=org.id, role="viewer"))
    async with AsyncClient(
        transport=ASGITransport(app=client._transport.app),  # type: ignore[attr-defined]
        base_url="http://test",
    ) as reader:
        await login(reader, "reader@northwind.dev")
        answer = await reader.put(f"/api/v1/projects/{project['id']}/models", json={"profiles": {}})
    assert answer.status_code == 403, answer.text
