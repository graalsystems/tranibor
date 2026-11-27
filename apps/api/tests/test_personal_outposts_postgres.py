"""Row-level security of a person's runner, on a real PostgreSQL (S87-15, ADR 0226 §1).

A personal outpost's own rows (its instances, its enrolment tokens, its calls) are under a RESTRICTIVE
policy copied from `projects_personal_owner`: they exist for a session of their owner, for the door that
names the outpost (a session that names the outpost and no person), and under the platform scope; for no
other person, whatever the organisation scope and the outpost they name. The `outposts` row itself stays an
organisation row: its administrators list, pause, revoke and delete it. Runs only with
`TRANIBOR_TEST_DATABASE_URL`: the schema is the migration's, the role is not a superuser, and
`session_scope()` keeps its real fail-closed behaviour (`pg_app`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from .conftest import login, without_postgres
from .kits import outposts as kit

pytestmark = without_postgres

OWN_ROWS = ("outpost_instances", "outpost_enrolments", "outpost_calls")


async def _rows(session: Any, table: str) -> int:
    """How many rows a raw SELECT with no `WHERE` clause finds."""
    return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())


async def _person(session: Any, org_id: str, email: str) -> str:
    from tranibor_api.db.models import Membership, User

    user = User(email=email, display_name=email.split("@", maxsplit=1)[0])
    session.add(user)
    await session.flush()
    session.add(Membership(user_id=user.id, org_id=org_id, role="contributor"))
    return str(user.id)


@pytest.fixture
async def runner(pg_app: Any) -> dict[str, str]:
    """Organisation `a`, its members Ada and Bob, Ada's runner (one instance, one enrolment token, one call)
    and the organisation's outpost `north` (the same three rows). The ids, by name."""
    from tranibor_api.db.models import Organization, Outpost, OutpostCall, OutpostEnrolment, OutpostInstance
    from tranibor_api.db.session import name_the_outpost, session_scope

    ids: dict[str, str] = {}
    async with session_scope(orgs="*") as session:
        org = Organization(slug="a", name="A")
        session.add(org)
        await session.flush()
        ids["org"] = org.id
        ids["ada"] = await _person(session, org.id, "ada@a.test")
        ids["bob"] = await _person(session, org.id, "bob@a.test")
        for name, owner, capabilities in (("ada", ids["ada"], ["runs"]), ("north", None, ["tools"])):
            outpost = Outpost(
                org_id=org.id,
                slug=f"personal-{name}" if owner else name,
                display_name=name,
                capabilities=capabilities,
                owner_user_id=owner,
            )
            session.add(outpost)
            await session.flush()
            ids[f"{name}-outpost"] = outpost.id
            await name_the_outpost(session, outpost.id)
            session.add(
                OutpostInstance(
                    id=outpost.id.replace("-", "") + "0" * 32,
                    org_id=org.id,
                    outpost_id=outpost.id,
                    status="pending",
                    signing_key=kit.signing_key(),
                    sealing_key=kit.sealing_key(),
                    fingerprint="SHA256:" + "a" * 43,
                    daemon_version="0.27.0",
                )
            )
            session.add(
                OutpostEnrolment(
                    org_id=org.id,
                    outpost_id=outpost.id,
                    token_digest=(name.encode().hex() * 16)[:64],
                    expires_at=datetime.now(UTC) + timedelta(minutes=15),
                )
            )
            session.add(
                OutpostCall(
                    id=f"call-{name}",
                    org_id=org.id,
                    outpost_id=outpost.id,
                    kind="list_tools",
                    endpoint="tracker",
                    write=False,
                )
            )
            await session.flush()
            await name_the_outpost(session, None)
    return ids


async def _seen(*, orgs: Any, user: str | None, outpost: str) -> dict[str, int]:
    """What a raw SELECT of each of an outpost's own tables finds, in a session of that scope."""
    from tranibor_api.db.session import name_the_outpost, name_the_user, session_scope

    async with session_scope(orgs=orgs) as session:
        await name_the_user(session, user)
        await name_the_outpost(session, outpost)
        return {table: await _rows(session, table) for table in OWN_ROWS}


async def test_rls_hides_a_personal_runner_from_another_user(runner: dict[str, str]) -> None:
    """Bob names Ada's runner inside the organisation they share and finds none of its rows; Ada, the door
    and the platform scope find each of them; and an organisation's outpost is not narrowed."""
    from tranibor_api.db.session import name_the_user, session_scope

    ada, bob, hers, north = runner["ada"], runner["bob"], runner["ada-outpost"], runner["north-outpost"]
    one_each = dict.fromkeys(OWN_ROWS, 1)
    assert await _seen(orgs=["a"], user=bob, outpost=hers) == dict.fromkeys(OWN_ROWS, 0)
    assert await _seen(orgs=["a"], user=ada, outpost=hers) == one_each, "its owner"
    assert await _seen(orgs=["a"], user=None, outpost=hers) == one_each, "the door, which names no person"
    assert await _seen(orgs="*", user=bob, outpost=hers) == one_each, "the platform scope"
    assert await _seen(orgs=["a"], user=bob, outpost=north) == one_each, "an organisation's outpost"
    async with session_scope(orgs=["a"]) as session:
        await name_the_user(session, bob)
        slugs = set((await session.execute(text("SELECT slug FROM outposts"))).scalars())
        assert slugs == {"personal-ada", "north"}, "the outposts row stays the organisation's"


async def test_another_user_writes_no_row_into_a_personal_runner(runner: dict[str, str]) -> None:
    """For writing as for reading: Bob's session that names Ada's runner moves none of its rows and puts
    none there."""
    from tranibor_api.db.session import name_the_outpost, name_the_user, session_scope

    hers = runner["ada-outpost"]
    async with session_scope(orgs=["a"]) as session:
        await name_the_user(session, runner["bob"])
        await name_the_outpost(session, hers)
        moved = await session.execute(text("UPDATE outpost_instances SET status = 'active'"))
        assert moved.rowcount == 0  # type: ignore[attr-defined]
        gone = await session.execute(text("DELETE FROM outpost_enrolments"))
        assert gone.rowcount == 0  # type: ignore[attr-defined]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with session_scope(orgs=["a"]) as session:
            await name_the_user(session, runner["bob"])
            await name_the_outpost(session, hers)
            await session.execute(
                text(
                    "INSERT INTO outpost_enrolments (id, org_id, outpost_id, token_digest, expires_at) "
                    "VALUES ('intruder', :org, :outpost, :digest, now() + interval '15 minutes')"
                ),
                {"org": runner["org"], "outpost": hers, "digest": "f" * 64},
            )
    seen = await _seen(orgs=["a"], user=runner["ada"], outpost=hers)
    assert seen == dict.fromkeys(OWN_ROWS, 1)
    async with session_scope(orgs=["a"]) as session:
        await name_the_user(session, runner["ada"])
        await name_the_outpost(session, hers)
        status = (await session.execute(text("SELECT status FROM outpost_instances"))).scalar_one()
        assert status == "pending"


async def test_one_runner_per_person_and_organisation(runner: dict[str, str]) -> None:
    from tranibor_api.db.models import Outpost
    from tranibor_api.db.session import session_scope

    with pytest.raises(IntegrityError):
        async with session_scope(orgs="*") as session:
            session.add(
                Outpost(
                    org_id=runner["org"],
                    slug="personal-ada-again",
                    display_name="again",
                    capabilities=["runs"],
                    owner_user_id=runner["ada"],
                )
            )


async def test_through_the_api_the_owner_enrols_and_approves_and_an_administrator_only_revokes(
    pg_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the migrated schema, as the application role: the owner's session reaches its runner's rows, the
    door enrols with no person named, another member and the administrator reach none of them, and the
    administrator's revocation still revokes every instance."""
    from tranibor_api.db.models import Organization, OutpostInstance
    from tranibor_api.db.session import name_the_outpost, session_scope

    kit.switch_on(monkeypatch)
    kit.configure(monkeypatch, personal_runners="on")
    async with session_scope(orgs="*") as session:
        session.add(Organization(slug="a", name="A"))
    me = "/api/v1/me/runners"
    async with AsyncClient(transport=ASGITransport(app=pg_app), base_url="http://test") as client:
        await login(client, "ada@a.test")
        issued = await client.post(f"{me}/a/enrolments")
        assert issued.status_code == 201, issued.text
        enrolled = await kit.enrol(issued.json()["token"])
        (runner,) = (await client.get(me)).json()["items"]
        (instance,) = runner["instances"]
        assert (instance["id"], instance["status"]) == (enrolled.instance.id, "pending")
        approval = f"{me}/instances/{enrolled.instance.id}/approval"
        approved = await client.post(approval, json={"fingerprint": instance["fingerprint"]})
        assert approved.status_code == 200 and approved.json()["status"] == "active", approved.text

        await login(client, "bob@a.test")
        assert (await client.get(me)).json() == {"items": []}
        assert (await client.post(approval, json={"fingerprint": instance["fingerprint"]})).status_code == 404

        await login(client, "admin@a.test")
        one = f"/api/v1/orgs/a/outposts/{runner['slug']}"
        (listed,) = (await client.get("/api/v1/orgs/a/outposts")).json()["items"]
        assert listed["slug"] == runner["slug"]
        assert listed["instances"] == {"pending": 0, "active": 0, "revoked": 0}, "its instances are hidden"
        refused = await client.post(f"{one}/instances/{enrolled.instance.id}/revoke", json=None)
        assert (refused.status_code, refused.json()["code"]) == (403, "personal_outpost")
        assert (await client.post(f"{one}/revoke")).json()["status"] == "revoked"

        async with session_scope(orgs="*") as session:
            await name_the_outpost(session, enrolled.outpost.id)
            row = (await session.execute(select(OutpostInstance))).scalar_one()
            assert (row.status, row.revoked_by) == ("revoked", "admin@a.test")

        # Deleted by the administrator, with the rows they could not see.
        assert (await client.delete(one)).status_code == 204
    async with session_scope(orgs="*") as session:
        await name_the_outpost(session, enrolled.outpost.id)
        assert {table: await _rows(session, table) for table in OWN_ROWS} == dict.fromkeys(OWN_ROWS, 0)
