"""A personal installation holds one person, and PostgreSQL holds it under the application role (S87-10).

ADR 0228 §2. PostgreSQL only (`TRANIBOR_TEST_DATABASE_URL`): the schema of `python -m tranibor_api.migrate`,
the non-superuser role the API connects as, which owns the tables, under forced row-level security. The
trigger `choregos_sole_member()` is what answers here, whoever writes: the members route, a raw `INSERT` sent
by the application's role from a session bounded to another organisation than the first person's, and two
writers at once. A machine account (ADR 0131) is not a person. The start-up check is run as `uvicorn` runs
it (`main.lifespan`), in the posture, and refuses each case the ADR names. The SQLite half, every path
through the routes, is `test_personal_single_member.py`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from .conftest import PG_URL, _administer, _app_url, login, without_postgres

pytestmark = without_postgres

#: The pg_app fixture's administrator (`TRANIBOR_DEV_ADMIN_EMAILS`) and organisation.
OWNER = "admin@a.test"


def _dsn(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


async def _as_the_application(*statements: str, scope: str = "*") -> None:
    """Raw statements sent by the API's own role, in one transaction, from a session bounded to `scope`."""
    import asyncpg

    connection = await asyncpg.connect(_dsn(_app_url(PG_URL)))
    try:
        async with connection.transaction():
            await connection.execute(f"SELECT set_config('app.current_orgs', '{scope}', true)")
            for statement in statements:
                await connection.execute(statement)
    finally:
        await connection.close()


async def _fetch(query: str) -> list[tuple[Any, ...]]:
    import asyncpg

    connection = await asyncpg.connect(_dsn(PG_URL))
    try:
        return [tuple(row) for row in await connection.fetch(query)]
    finally:
        await connection.close()


def _person(user_id: str, *, machine: bool = False) -> str:
    return (
        "INSERT INTO users (id, email, display_name, is_service, groups, created_at, updated_at) "
        f"VALUES ('{user_id}', '{user_id}@x.test', '', {str(machine).lower()}, '[]', now(), now())"
    )


def _membership(membership_id: str, user_id: str, org: str) -> str:
    return (
        "INSERT INTO memberships (id, user_id, org_id, role, source, created_at, updated_at) "
        f"SELECT '{membership_id}', '{user_id}', o.id, 'contributor', 'manual', now(), now() "
        f"FROM organizations o WHERE o.slug = '{org}'"
    )


async def _record(posture: str) -> None:
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import record_posture

    async with session_scope(orgs="*") as session:
        assert await record_posture(session, posture, environment=posture) == posture


@pytest.fixture
async def orgs(pg_app: Any) -> list[str]:
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        session.add_all([Organization(slug=slug, name=slug.upper()) for slug in ("a", "b")])
    return ["a", "b"]


@pytest.fixture
async def owner(pg_app: Any, orgs: list[str]) -> AsyncIterator[AsyncClient]:
    """The installation's one person, signed in, `org_admin` of `a`."""
    async with AsyncClient(transport=ASGITransport(app=pg_app), base_url="http://test") as client:
        await login(client, OWNER)
        yield client


# ───────────────────────── the trigger, under the application role ─────────────────────────


async def test_a_second_member_is_refused_through_the_route_naming_the_record(owner: AsyncClient) -> None:
    await _record("personal")
    refused = await owner.post(
        "/api/v1/orgs/a/members", json={"email": "second@a.test", "role": "contributor"}
    )
    assert refused.status_code == 409, refused.text
    problem = refused.json()
    assert problem["code"] == "one_person"
    assert "personal" in problem["detail"] and "`installation`" in problem["detail"], problem
    assert await _fetch(
        "SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id WHERE NOT u.is_service"
    ) == [(OWNER,)]


async def test_a_raw_insert_is_refused_by_the_trigger_from_any_scope(owner: AsyncClient) -> None:
    """The second person's row is written by the API's own role, from a session bounded to `b`, which
    cannot see the first person's membership in `a`: the trigger looks past the scope, and refuses."""
    import asyncpg

    await _record("personal")
    for scope in ("b", "*"):
        with pytest.raises(asyncpg.CheckViolationError, match="choregos_sole_member") as refused:
            await _as_the_application(_person("u-two"), _membership("m-two", "u-two", "b"), scope=scope)
        assert "ADR 0228" in str(refused.value)
    # A machine account is accepted, and the scope is the session's again once the trigger looked past
    # it: `b` sees nothing of `a`.
    connection = await asyncpg.connect(_dsn(_app_url(PG_URL)))
    try:
        async with connection.transaction():
            await connection.execute("SELECT set_config('app.current_orgs', 'b', true)")
            await connection.execute(_person("u-mach", machine=True))
            await connection.execute(_membership("m-mach", "u-mach", "b"))
            assert await connection.fetchval("SELECT current_setting('app.current_orgs')") == "b"
            assert await connection.fetchval("SELECT count(*) FROM memberships") == 1, "b's own row only"
    finally:
        await connection.close()
    assert await _fetch(
        "SELECT u.email, u.is_service FROM memberships m JOIN users u ON u.id = m.user_id ORDER BY 1"
    ) == [(OWNER, False), ("u-mach@x.test", True)]


async def test_an_update_handing_a_membership_to_a_second_person_is_refused(owner: AsyncClient) -> None:
    import asyncpg

    await _record("personal")
    await _as_the_application(_person("u-mach", machine=True), _membership("m-mach", "u-mach", "a"))
    await _as_the_application(_person("u-two", machine=False))
    with pytest.raises(asyncpg.CheckViolationError, match="choregos_sole_member"):
        await _as_the_application("UPDATE memberships SET user_id = 'u-two' WHERE id = 'm-mach'")
    # A machine account's row changes as before.
    await _as_the_application("UPDATE memberships SET role = 'approver' WHERE id = 'm-mach'")


async def test_a_machine_account_is_accepted_through_its_route(owner: AsyncClient) -> None:
    await _record("personal")
    project = await owner.post(
        "/api/v1/orgs/a/projects",
        json={"slug": "mine", "name": "Mine", "config": {"slug": "mine", "org": "a"}},
    )
    assert project.status_code == 201, project.text
    machine = await owner.post(
        f"/api/v1/projects/{project.json()['id']}/machine-accounts", json={"slug": "ci"}
    )
    assert machine.status_code == 201, machine.text
    assert await _fetch(
        "SELECT count(*) FROM memberships m JOIN users u ON u.id = m.user_id WHERE u.is_service"
    ) == [(1,)]


async def test_two_persons_writing_at_once_are_put_in_a_line(pg_app: Any, orgs: list[str]) -> None:
    """Two first sign-ins at the same time: each alone would see nobody else. The trigger locks the
    installation's row, so the second waits for the first and then sees it."""
    import asyncpg

    await _record("personal")
    await _as_the_application(_person("u-one"), _person("u-two"))
    first = await asyncpg.connect(_dsn(_app_url(PG_URL)))
    second = await asyncpg.connect(_dsn(_app_url(PG_URL)))
    try:
        one = first.transaction()
        await one.start()
        await first.execute("SELECT set_config('app.current_orgs', 'a', true)")
        await first.execute(_membership("m-one", "u-one", "a"))

        async def the_other() -> None:
            async with second.transaction():
                await second.execute("SELECT set_config('app.current_orgs', 'b', true)")
                await second.execute(_membership("m-two", "u-two", "b"))

        waiting = asyncio.create_task(the_other())
        await asyncio.sleep(0.3)
        assert not waiting.done(), "the second writer did not wait for the first"
        await one.commit()
        with pytest.raises(asyncpg.CheckViolationError, match="choregos_sole_member"):
            await asyncio.wait_for(waiting, timeout=10)
    finally:
        await first.close()
        await second.close()
    assert await _fetch("SELECT user_id FROM memberships") == [("u-one",)]


async def test_a_machine_account_holding_a_membership_does_not_become_a_second_person(
    owner: AsyncClient,
) -> None:
    """`users.is_service` turned false makes a machine account a person: with a membership, beside the one
    person's, the trigger on `users` refuses it, from a session bounded to another organisation too."""
    import asyncpg

    await _record("personal")
    await _as_the_application(_person("u-mach", machine=True), _membership("m-mach", "u-mach", "b"))
    await _as_the_application(_person("u-idle", machine=True))
    for scope in ("b", "*"):
        with pytest.raises(asyncpg.CheckViolationError, match="choregos_sole_member") as refused:
            await _as_the_application("UPDATE users SET is_service = false WHERE id = 'u-mach'", scope=scope)
        assert "may not become a person" in str(refused.value)
    # Without a membership it is no member, and its other columns change as before.
    await _as_the_application(
        "UPDATE users SET is_service = false WHERE id = 'u-idle'",
        "UPDATE users SET display_name = 'ci', is_service = true WHERE id = 'u-mach'",
        scope="b",
    )
    assert await _fetch(
        "SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id WHERE NOT u.is_service"
    ) == [(OWNER,)]


async def test_outside_the_personal_posture_a_machine_account_becomes_a_person(owner: AsyncClient) -> None:
    await _record("organisation")
    await _as_the_application(_person("u-mach", machine=True), _membership("m-mach", "u-mach", "b"))
    await _as_the_application("UPDATE users SET is_service = false WHERE id = 'u-mach'", scope="b")
    assert await _fetch("SELECT is_service FROM users WHERE id = 'u-mach'") == [(False,)]


@pytest.mark.parametrize("first", ["membership", "flip"])
async def test_a_membership_and_a_flip_written_at_once_are_put_in_a_line(
    owner: AsyncClient, first: str
) -> None:
    """A membership given to a machine account and that account turned into a person, at the same time:
    each alone passes (the account is no person yet, or holds no membership yet). Both triggers lock the
    installation's row before they read, so the second waits for the first, then reads it and refuses. Both
    writers are in the platform's scope: from a session bounded to `b`, the account is not visible (row-level
    security on `users`) until its membership there is committed, and the flip would change no row."""
    import asyncpg

    await _record("personal")
    await _as_the_application(_person("u-mach", machine=True))
    statements = {
        "membership": _membership("m-mach", "u-mach", "b"),
        "flip": "UPDATE users SET is_service = false WHERE id = 'u-mach'",
    }
    second = "flip" if first == "membership" else "membership"
    leading = await asyncpg.connect(_dsn(_app_url(PG_URL)))
    trailing = await asyncpg.connect(_dsn(_app_url(PG_URL)))
    try:
        one = leading.transaction()
        await one.start()
        await leading.execute("SELECT set_config('app.current_orgs', '*', true)")
        assert await leading.execute(statements[first]) in {"INSERT 0 1", "UPDATE 1"}

        async def the_other() -> None:
            async with trailing.transaction():
                await trailing.execute("SELECT set_config('app.current_orgs', '*', true)")
                await trailing.execute(statements[second])

        waiting = asyncio.create_task(the_other())
        await asyncio.sleep(0.3)
        assert not waiting.done(), f"the {second} did not wait for the {first}"
        await one.commit()
        with pytest.raises(asyncpg.CheckViolationError, match="choregos_sole_member"):
            await asyncio.wait_for(waiting, timeout=10)
    finally:
        await leading.close()
        await trailing.close()
    assert await _fetch(
        "SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id WHERE NOT u.is_service"
    ) == [(OWNER,)]


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch, pg_app: Any) -> Iterator[Any]:
    """`choregos-admin orgs bootstrap` in the personal posture: `bootstrap_installation(get_settings())`, on
    a database the API never started (the application is built, its start-up never run)."""
    from tranibor_api.bootstrap import bootstrap_installation
    from tranibor_api.config import get_settings, reset_settings_cache

    async def bootstrap(organisation: str, address: str) -> Any:
        monkeypatch.setenv("TRANIBOR_ENV", "personal")
        monkeypatch.setenv("TRANIBOR_FAKES", "0")
        monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
        monkeypatch.setenv("TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS", "true")
        monkeypatch.setenv("TRANIBOR_BOOTSTRAP_ORG", organisation)
        monkeypatch.setenv("TRANIBOR_BOOTSTRAP_ADMINS", address)
        reset_settings_cache()
        return await bootstrap_installation(get_settings())

    yield bootstrap
    reset_settings_cache()


async def test_two_cli_bootstraps_bring_one_person_to_a_database_never_started(cli: Any) -> None:
    """Each run names one address, as the posture allows, but not the same one. The first records the
    posture before its membership; the second is then refused by the trigger, and leaves nothing behind."""
    from tranibor_api.bootstrap import ADMINISTRATORS
    from tranibor_api.personal_installation import InstallationRefusedError

    assert await _fetch("SELECT count(*) FROM installation") == [(0,)], "never started"
    assert (await cli("me", "one@x.test"))[ADMINISTRATORS] == ["one@x.test"]
    assert await _fetch("SELECT posture, environment FROM installation") == [("personal", "personal")]
    with pytest.raises(InstallationRefusedError, match="one person"):
        await cli("another", "two@x.test")
    assert await _fetch("SELECT u.email FROM memberships m JOIN users u ON u.id = m.user_id") == [
        ("one@x.test",)
    ]
    assert await _fetch("SELECT slug FROM organizations ORDER BY slug") == [("me",)]


async def test_the_posture_row_is_written_once(pg_app: Any) -> None:
    import asyncpg

    await _record("personal")
    for statement in ("UPDATE installation SET posture = 'organisation'", "DELETE FROM installation"):
        with pytest.raises(asyncpg.CheckViolationError, match="written once"):
            await _as_the_application(statement)
    with pytest.raises(asyncpg.InsufficientPrivilegeError):
        # A second row from a session bounded to an organisation: refused by the policy.
        await _as_the_application(
            "INSERT INTO installation (id, posture, environment, recorded_at) "
            "VALUES (2, 'personal', 'x', now())",
            scope="a",
        )
    assert await _fetch("SELECT id, posture FROM installation") == [(1, "personal")]


async def test_outside_the_personal_posture_nothing_changes(owner: AsyncClient) -> None:
    await _record("organisation")
    added = await owner.post("/api/v1/orgs/a/members", json={"email": "second@a.test", "role": "contributor"})
    assert added.status_code == 201, added.text
    await _as_the_application(_person("u-two"), _membership("m-two", "u-two", "b"), scope="b")
    assert await _fetch(
        "SELECT count(DISTINCT m.user_id) FROM memberships m JOIN users u ON u.id = m.user_id "
        "WHERE NOT u.is_service"
    ) == [(3,)]


# ───────────────────────── the start-up, in the posture ─────────────────────────


@pytest.fixture
def posture(monkeypatch: pytest.MonkeyPatch, pg_app: Any) -> Iterator[Any]:
    """`main.lifespan` with `TRANIBOR_ENV` set to `environment`, what production asks of it given."""
    from tranibor_api import main
    from tranibor_api.config import reset_settings_cache

    def start(environment: str, **variables: str) -> Any:
        monkeypatch.setenv("TRANIBOR_ENV", environment)
        monkeypatch.setenv("TRANIBOR_FAKES", "0")
        monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
        monkeypatch.setenv("TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS", "true")
        for name, value in variables.items():
            monkeypatch.setenv(name, value)
        reset_settings_cache()
        return main.lifespan(pg_app)

    yield start
    reset_settings_cache()


async def _refused(start: Any, *expected: str) -> None:
    from tranibor_api.personal_installation import InstallationRefusedError

    with pytest.raises(InstallationRefusedError) as refused:
        async with start:
            pass
    for part in expected:
        assert part in str(refused.value), str(refused.value)


async def test_a_posture_mismatch_refuses_to_start_either_way(posture: Any) -> None:
    await _record("personal")
    await _refused(posture("prod"), "recorded `personal`", "TRANIBOR_ENV=prod")
    await _administer(PG_URL, "TRUNCATE installation")
    await _record("organisation")
    await _refused(posture("personal"), "recorded `organisation`", "TRANIBOR_ENV=personal")


async def test_two_persons_refuse_to_start_and_nothing_is_recorded(posture: Any, orgs: list[str]) -> None:
    await _as_the_application(
        _person("u-one"),
        _person("u-two"),
        _membership("m-one", "u-one", "a"),
        _membership("m-two", "u-two", "b"),
    )
    await _refused(posture("personal"), "2 persons", "u-one@x.test", "u-two@x.test")
    assert await _fetch("SELECT count(*) FROM installation") == [(0,)], (
        "a refused first start records nothing"
    )
    # Recorded earlier (a raw writer, or a release before the trigger): refused just the same.
    await _record("personal")
    await _refused(posture("personal"), "2 persons")


async def test_two_bootstrap_addresses_refuse_to_start(posture: Any) -> None:
    await _refused(
        posture("personal", TRANIBOR_BOOTSTRAP_ORG="me", TRANIBOR_BOOTSTRAP_ADMINS="one@x.test,two@x.test"),
        "TRANIBOR_BOOTSTRAP_ADMINS names 2 addresses",
    )
    assert await _fetch("SELECT count(*) FROM installation") == [(0,)]


async def test_the_first_personal_start_records_the_posture_and_bootstraps_its_one_person(
    posture: Any,
) -> None:
    async with posture("personal", TRANIBOR_BOOTSTRAP_ORG="me", TRANIBOR_BOOTSTRAP_ADMINS="owner@x.test"):
        pass
    assert await _fetch("SELECT posture, environment FROM installation") == [("personal", "personal")]
    assert await _fetch("SELECT u.email, m.role FROM memberships m JOIN users u ON u.id = m.user_id") == [
        ("owner@x.test", "org_admin")
    ]
    async with posture("personal"):
        pass  # the next start finds its own record, and one person
