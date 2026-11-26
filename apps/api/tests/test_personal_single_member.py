"""A personal installation holds one person, on every path that writes a membership (S87-10, ADR 0228 §2).

The suite's own database, SQLite, carries the rule as the migrations lay it there (`create_all` lays the
same triggers, a test below compares them), so every path is proven on every run. The posture is written
into the `installation` row as a first start writes it; the API is not started in `personal` here (SQLite
is refused in that posture, a test below), so the routes run in the suite's `test` environment with the
development sign-in: what is proven is that the record, and nothing else, decides. PostgreSQL's own
trigger, under the application role and across row-level security scopes, is
`test_personal_single_member_postgres.py`. In CI's second pass (`TRANIBOR_TEST_SUITE_ON_POSTGRES=1`) the
same tests run on a migrated PostgreSQL, where that trigger answers.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError

from .conftest import API, SUITE_ON_POSTGRES

ADMIN = "admin@northwind.dev"
SECOND = "second@northwind.dev"


async def _record(posture: str) -> None:
    """The `installation` row, as the API's first start writes it in that posture."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import record_posture

    async with session_scope(orgs="*") as session:
        assert await record_posture(session, posture, environment=posture) == posture


async def _people_with_a_membership() -> list[str]:
    """The persons who hold a membership, read without the module under test."""
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        rows = await session.execute(
            text(
                "SELECT DISTINCT u.email FROM memberships m JOIN users u ON u.id = m.user_id "
                "WHERE u.is_service = false ORDER BY u.email"
            )
        )
        return [str(email) for email in rows.scalars()]


def _refused_as_one_person(response: Any) -> None:
    assert response.status_code == 409, response.text
    problem = response.json()
    assert problem["code"] == "one_person", problem
    # The refusal names the record that decides it, and the way out.
    assert "personal" in problem["detail"] and "`installation`" in problem["detail"], problem
    assert "ADR 0228" in problem["detail"], problem


async def _sign_in(client: AsyncClient, email: str) -> Any:
    """The development sign-in, its callback's answer returned instead of asserted."""
    start = await client.get("/api/v1/auth/login", params={"as": email})
    assert start.status_code == 307, start.text
    return await client.get(start.headers["location"])


# ───────────────────────── personal: a second person is refused on every path ─────────────────────────


async def test_a_second_person_added_as_a_member_is_refused_naming_the_record(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await _record("personal")
    for body in (
        {"email": SECOND, "role": "contributor"},
        {"email": SECOND, "role": "contributor", "project_slug": project["slug"]},
    ):
        _refused_as_one_person(await client.post("/api/v1/orgs/northwind/members", json=body))
    assert await _people_with_a_membership() == [ADMIN]
    listed = (await client.get("/api/v1/orgs/northwind/members")).json()["items"]
    assert {member["email"] for member in listed} == {ADMIN}


async def test_a_second_person_signing_in_is_refused(client: AsyncClient, org: str) -> None:
    """The sign-in maps a person's groups to memberships (`routers/auth.py`): the one person signs in and
    becomes a member; a second one is refused, and nothing of theirs is kept."""
    await _record("personal")
    first = await _sign_in(client, ADMIN)
    assert first.status_code == 307, first.text
    client.cookies.clear()
    _refused_as_one_person(await _sign_in(client, SECOND))
    assert await _people_with_a_membership() == [ADMIN]


async def test_the_one_person_s_own_paths_and_a_machine_account_pass(client: AsyncClient, admin: str) -> None:
    """The rule counts persons, not memberships: the person's project (`routers/projects.py` adds them as
    its owner), a machine account (ADR 0131, `users.is_service`) and the bootstrap naming the same person
    all pass."""
    from tranibor_api.bootstrap import ADMINISTRATORS, ORGANISATIONS, bootstrap_installation
    from tranibor_api.config import get_settings

    await _record("personal")
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={"slug": "mine", "name": "Mine", "config": {"slug": "mine", "org": "northwind"}},
    )
    assert created.status_code == 201, created.text
    machine = await client.post(
        f"/api/v1/projects/{created.json()['id']}/machine-accounts", json={"slug": "ci"}
    )
    assert machine.status_code == 201, machine.text
    settings = get_settings().model_copy(update={"bootstrap_org": "second-space", "bootstrap_admins": ADMIN})
    created_now = await bootstrap_installation(settings)
    assert created_now == {ORGANISATIONS: ["second-space"], ADMINISTRATORS: [ADMIN]}
    assert await _people_with_a_membership() == [ADMIN]


async def test_the_bootstrap_cannot_bring_a_second_person(client: AsyncClient, admin: str) -> None:
    from tranibor_api.bootstrap import bootstrap_installation
    from tranibor_api.config import get_settings
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import InstallationRefusedError

    await _record("personal")
    settings = get_settings().model_copy(update={"bootstrap_org": "second-space", "bootstrap_admins": SECOND})
    with pytest.raises(InstallationRefusedError, match="one person") as refused:
        await bootstrap_installation(settings)
    assert "`installation`" in str(refused.value)
    async with session_scope(orgs="*") as session:
        slugs = (await session.execute(select(Organization.slug))).scalars().all()
    assert slugs == ["northwind"], "the refused bootstrap left nothing behind"
    assert await _people_with_a_membership() == [ADMIN]


async def test_two_bootstrap_addresses_are_refused_in_the_personal_posture() -> None:
    from tranibor_api.bootstrap import bootstrap_installation
    from tranibor_api.config import get_settings
    from tranibor_api.personal_installation import InstallationRefusedError, refuse_what_the_settings_forbid

    settings = get_settings().model_copy(
        update={
            "env": "personal",
            "database_url": "postgresql+asyncpg://choregos@db/choregos",
            "bootstrap_org": "me",
            "bootstrap_admins": "one@example.org, two@example.org",
        }
    )
    with pytest.raises(InstallationRefusedError, match="TRANIBOR_BOOTSTRAP_ADMINS names 2 addresses"):
        refuse_what_the_settings_forbid(settings)
    with pytest.raises(InstallationRefusedError, match="TRANIBOR_BOOTSTRAP_ADMINS"):
        await bootstrap_installation(settings)
    # One address, or none, is the posture's bootstrap; outside it, any number.
    refuse_what_the_settings_forbid(settings.model_copy(update={"bootstrap_admins": "one@example.org"}))
    refuse_what_the_settings_forbid(settings.model_copy(update={"env": "prod"}))


async def test_the_database_refuses_a_raw_insert_and_an_update_to_a_second_person(app: Any, org: str) -> None:
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import SOLE_MEMBER_MARK

    await _record("personal")
    async with session_scope(orgs="*") as session:
        org_id = (await session.execute(text("SELECT id FROM organizations"))).scalar_one()
        for user_id, email, machine in (("u-one", "one@x.test", False), ("u-two", "two@x.test", False)):
            await session.execute(
                text(
                    "INSERT INTO users (id, email, display_name, is_service, groups, created_at, updated_at) "
                    "VALUES (:id, :email, '', :machine, '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"id": user_id, "email": email, "machine": machine},
            )
    insert = (
        "INSERT INTO memberships (id, user_id, org_id, role, source, created_at, updated_at) "
        "VALUES (:id, :user, :org, 'contributor', 'manual', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    async with session_scope(orgs="*") as session:
        await session.execute(text(insert), {"id": "m-one", "user": "u-one", "org": org_id})
    with pytest.raises(IntegrityError, match=SOLE_MEMBER_MARK):
        async with session_scope(orgs="*") as session:
            await session.execute(text(insert), {"id": "m-two", "user": "u-two", "org": org_id})
    with pytest.raises(IntegrityError, match=SOLE_MEMBER_MARK):
        async with session_scope(orgs="*") as session:
            # A second row for the one person, then handed to another: the update is the same refusal.
            await session.execute(text(insert), {"id": "m-one-bis", "user": "u-one", "org": org_id})
            await session.execute(text("UPDATE memberships SET user_id = 'u-two' WHERE id = 'm-one-bis'"))
    assert await _people_with_a_membership() == ["one@x.test"]


async def test_the_posture_is_written_once(app: Any) -> None:
    """No conversion (ADR 0228 §9): the row is neither changed nor removed, and a second record keeps the
    first."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import record_posture, recorded_posture

    await _record("personal")
    for statement in ("UPDATE installation SET posture = 'organisation'", "DELETE FROM installation"):
        with pytest.raises(IntegrityError, match="written once"):
            async with session_scope(orgs="*") as session:
                await session.execute(text(statement))
    async with session_scope(orgs="*") as session:
        assert await record_posture(session, "organisation", environment="prod") == "personal"
        assert await recorded_posture(session) == "personal"


async def test_any_other_integrity_error_is_still_a_server_error(app: Any) -> None:
    """Only the sole-member refusal becomes a 409: another integrity error is answered as before, a 500
    carrying its correlation id and nothing of the database's message."""

    @app.get("/api/v1/test-only/integrity")
    async def _collides() -> None:
        raise IntegrityError("INSERT INTO users", {}, Exception("UNIQUE constraint failed: users.email"))

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        answer = await client.get("/api/v1/test-only/integrity")
    assert answer.status_code == 500, answer.text
    assert answer.json().get("correlation_id") and "users.email" not in answer.text


# ───────────────────────── outside the posture: nothing changes ─────────────────────────


@pytest.mark.parametrize("posture", [None, "organisation"])
async def test_outside_the_personal_posture_a_second_person_is_a_member_as_before(
    client: AsyncClient, admin: str, posture: str | None
) -> None:
    if posture:
        await _record(posture)
    added = await client.post("/api/v1/orgs/northwind/members", json={"email": SECOND, "role": "contributor"})
    assert added.status_code == 201, added.text
    client.cookies.clear()
    third = await _sign_in(client, "third@northwind.dev")
    assert third.status_code == 307, third.text
    assert await _people_with_a_membership() == sorted([ADMIN, SECOND, "third@northwind.dev"])


# ───────────────────────── the start-up ─────────────────────────


@pytest.fixture
def started(app: Any) -> Iterator[Any]:
    """`main.lifespan` on the test's database, as `uvicorn` runs it."""
    from tranibor_api import main

    yield lambda: main.lifespan(app)


async def test_the_first_start_records_the_posture_and_the_next_ones_keep_it(started: Any) -> None:
    from tranibor_api.db.models import Installation
    from tranibor_api.db.session import session_scope

    for _ in range(2):
        async with started():
            pass
    async with session_scope(orgs="*") as session:
        rows = (await session.execute(select(Installation))).scalars().all()
    assert [(row.posture, row.environment) for row in rows] == [("organisation", "test")]


async def test_a_personal_record_refuses_to_start_in_another_environment(started: Any) -> None:
    from tranibor_api.personal_installation import InstallationRefusedError

    await _record("personal")
    with pytest.raises(InstallationRefusedError) as refused:
        async with started():
            pass
    assert "recorded `personal`" in str(refused.value) and "TRANIBOR_ENV=test" in str(refused.value)


@pytest.mark.skipif(SUITE_ON_POSTGRES, reason="SQLite's own refusal")
async def test_the_personal_posture_refuses_to_start_on_sqlite(
    started: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.db.models import Installation
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import InstallationRefusedError

    monkeypatch.setenv("TRANIBOR_ENV", "personal")
    monkeypatch.setenv("TRANIBOR_FAKES", "0")
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    reset_settings_cache()
    with pytest.raises(InstallationRefusedError, match="SQLite"):
        async with started():
            pass
    async with session_scope(orgs="*") as session:
        assert (await session.execute(select(Installation))).scalars().all() == [], "nothing recorded"


# ───────────────────────── the migration lays what `create_all` lays ─────────────────────────


def _triggers(url: str) -> dict[str, str]:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND tbl_name IN ('memberships', 'installation')"
            ).fetchall()
    finally:
        engine.dispose()
    return {str(name): " ".join(str(sql).split()) for name, sql in rows}


@pytest.mark.skipif(SUITE_ON_POSTGRES, reason="SQLite's triggers")
def test_the_migrations_lay_on_sqlite_the_triggers_the_suite_runs_on(
    app: Any, tmp_path: pathlib.Path
) -> None:
    from tranibor_api.config import get_settings

    monkeypatch = pytest.MonkeyPatch()
    migrated = tmp_path / "migrated.db"
    config = Config(str(API / "alembic.ini"))
    config.set_main_option("script_location", str(API / "src" / "tranibor_api" / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{migrated}")
    monkeypatch.setenv("TRANIBOR_DATABASE_URL", f"sqlite+aiosqlite:///{migrated}")
    get_settings.cache_clear()
    try:
        command.upgrade(config, "heads")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    laid = _triggers(f"sqlite:///{migrated}")
    assert set(laid) == {
        "memberships_sole_member_insert",
        "memberships_sole_member_update",
        "installation_written_once_update",
        "installation_written_once_delete",
    }
    assert laid == _triggers(f"sqlite:///{tmp_path}/test.db")
