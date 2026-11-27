"""Row-level security of a person's runner, on a real PostgreSQL (S87-15, ADR 0226 §1).

A personal outpost's own rows (its instances, its enrolment tokens, its calls) are under a RESTRICTIVE
policy copied from `projects_personal_owner`: they exist for a session of their owner, for the door that
names the outpost (a session its own code marks `app.current_door = 'outposts'`, `mark_the_door`), and under
the platform scope; for no other person, whatever the organisation scope and the outpost they name, and for
no session that merely names no person. The `outposts` row itself stays an organisation row: its
administrators list, pause, revoke and delete it. Runs only with `TRANIBOR_TEST_DATABASE_URL`: the schema is
the migration's, the role is not a superuser, and `session_scope()` keeps its real fail-closed behaviour
(`pg_app`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from .conftest import login, without_postgres
from .kits import outpost_daemon as daemon_kit
from .kits import outposts as kit
from .kits.outpost_daemon import Daemon

pytestmark = without_postgres

OWN_ROWS = ("outpost_instances", "outpost_enrolments", "outpost_calls")
ME = "/api/v1/me/runners"
UNKNOWN_CALL = "8f0e6b1c-5a3d-4c57-9e21-7b2a4d9c0f13"


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


@asynccontextmanager
async def _session(
    *, orgs: Any, user: str | None, outpost: str, door: str | None = None
) -> AsyncIterator[Any]:
    """A session of that scope, naming that user and that outpost; `door` is what `app.current_door`
    holds, unset when `None` (`"outposts"` is what `mark_the_door` sets)."""
    from tranibor_api.db.session import name_the_outpost, name_the_user, session_scope

    async with session_scope(orgs=orgs) as session:
        await name_the_user(session, user)
        await name_the_outpost(session, outpost)
        if door is not None:
            await session.execute(text("SELECT set_config('app.current_door', :door, true)"), {"door": door})
        yield session


async def _seen(*, orgs: Any, user: str | None, outpost: str, door: str | None = None) -> dict[str, int]:
    """What a raw SELECT of each of an outpost's own tables finds, in a session of that scope."""
    async with _session(orgs=orgs, user=user, outpost=outpost, door=door) as session:
        return {table: await _rows(session, table) for table in OWN_ROWS}


async def test_rls_hides_a_personal_runner_from_another_user(runner: dict[str, str]) -> None:
    """Bob names Ada's runner inside the organisation they share and finds none of its rows; Ada, the door
    and the platform scope find each of them; and an organisation's outpost is not narrowed."""
    from tranibor_api.db.session import OUTPOSTS_DOOR, name_the_user, session_scope

    ada, bob, hers, north = runner["ada"], runner["bob"], runner["ada-outpost"], runner["north-outpost"]
    one_each = dict.fromkeys(OWN_ROWS, 1)
    assert await _seen(orgs=["a"], user=bob, outpost=hers) == dict.fromkeys(OWN_ROWS, 0)
    assert await _seen(orgs=["a"], user=ada, outpost=hers) == one_each, "its owner"
    assert await _seen(orgs=["a"], user=None, outpost=hers, door=OUTPOSTS_DOOR) == one_each, "the door"
    assert await _seen(orgs="*", user=bob, outpost=hers) == one_each, "the platform scope"
    assert await _seen(orgs=["a"], user=bob, outpost=north) == one_each, "an organisation's outpost"
    async with session_scope(orgs=["a"]) as session:
        await name_the_user(session, bob)
        slugs = set((await session.execute(text("SELECT slug FROM outposts"))).scalars())
        assert slugs == {"personal-ada", "north"}, "the outposts row stays the organisation's"


async def test_a_session_that_names_no_person_and_is_not_the_door_finds_none_of_a_personal_runner(
    runner: dict[str, str],
) -> None:
    """The door is known by its marker, never by the absence of a person: a session that names Ada's runner
    and neither a user nor the door (a request that forgot to name its user, a worker of the platform that is
    not the door) finds none of the runner's rows and writes none, as does a session whose marker names
    another door. The marker widens nothing else: the door still sees nothing outside the organisation scope
    and the outpost it names."""
    from tranibor_api.db.session import OUTPOSTS_DOOR

    hers, north = runner["ada-outpost"], runner["north-outpost"]
    none_of_them = dict.fromkeys(OWN_ROWS, 0)
    assert await _seen(orgs=["a"], user=None, outpost=hers) == none_of_them, "no person, no marker"
    assert await _seen(orgs=["a"], user=None, outpost=hers, door="") == none_of_them, "an empty marker"
    assert await _seen(orgs=["a"], user=None, outpost=hers, door="api") == none_of_them, "another door"
    assert await _seen(orgs=["b"], user=None, outpost=hers, door=OUTPOSTS_DOOR) == none_of_them, "elsewhere"
    assert await _seen(orgs=["a"], user=None, outpost=north) == dict.fromkeys(OWN_ROWS, 1), (
        "an organisation's outpost is not narrowed"
    )
    async with _session(orgs=["a"], user=None, outpost=hers) as session:
        moved = await session.execute(text("UPDATE outpost_instances SET status = 'active'"))
        assert moved.rowcount == 0  # type: ignore[attr-defined]
    with pytest.raises(DBAPIError, match="row-level security"):
        async with _session(orgs=["a"], user=None, outpost=hers) as session:
            await session.execute(
                text(
                    "INSERT INTO outpost_enrolments (id, org_id, outpost_id, token_digest, expires_at) "
                    "VALUES ('unmarked', :org, :outpost, :digest, now() + interval '15 minutes')"
                ),
                {"org": runner["org"], "outpost": hers, "digest": "e" * 64},
            )
    async with _session(orgs=["a"], user=None, outpost=hers, door=OUTPOSTS_DOOR) as session:
        status = (await session.execute(text("SELECT status FROM outpost_instances"))).scalar_one()
        assert status == "pending", "nothing moved"


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


# ───────────────────────── the door's own flows, marked; the platform's sweep ─────────────────────────


@pytest.fixture
def door_on(monkeypatch: pytest.MonkeyPatch) -> kit.Door:
    """Outposts and personal runners on, the door given its origin and its key, the deployment its key
    wrapper. Asked for before `pg_app`: the application mounts the door when it is built."""
    daemon_kit.key_wrapper(monkeypatch)
    door = kit.switch_on(monkeypatch)
    kit.configure(monkeypatch, personal_runners="on")
    return door


async def _call_state(outpost_id: str, call_id: str) -> str:
    from tranibor_api.db.models import OutpostCall
    from tranibor_api.db.session import name_the_outpost, session_scope

    async with session_scope(orgs="*") as session:
        await name_the_outpost(session, outpost_id)
        query = select(OutpostCall.state).where(OutpostCall.id == call_id)
        return str((await session.execute(query)).scalar_one())


@pytest.mark.parametrize("served", ["by the API", "alone"])
async def test_the_door_serves_a_personal_runner_s_machine_whether_the_api_serves_it_or_it_is_alone(
    door_on: kit.Door, pg_app: Any, served: str
) -> None:
    """The door's sessions are marked (`door_session`, `rendezvous.in_scope(..., door=True)`), in the API
    and in `python -m tranibor_api.outposts` alike: a machine of Ada's enrols through it with no person
    named, is told it is pending, polls once Ada approved it, and is told a call it names is not its
    runner's. Unmarked, the enrolment token, the instance and its slot are hidden from the door: the
    enrolment is refused, a signed request is not recognised, a look takes the instance for revoked."""
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope
    from tranibor_api.outposts.alone import create_app

    async with session_scope(orgs="*") as session:
        session.add(Organization(slug="a", name="A"))
    async with (
        AsyncClient(transport=ASGITransport(app=pg_app), base_url="http://test") as client,
        AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://door") as alone,
    ):
        door = client if served == "by the API" else alone
        await login(client, "ada@a.test")
        issued = await client.post(f"{ME}/a/enrolments")
        assert issued.status_code == 201, issued.text
        daemon = Daemon()
        enrolled = await daemon.enrol(door, issued.json()["token"])
        assert enrolled.status_code == 201, enrolled.text
        daemon_kit.refused(await daemon.poll(door), "instance_pending", 403)
        (runner,) = (await client.get(ME)).json()["items"]
        (instance,) = runner["instances"]
        approval = f"{ME}/instances/{daemon.instance_id}/approval"
        approved = await client.post(approval, json={"fingerprint": instance["fingerprint"]})
        assert approved.status_code == 200, approved.text
        # A runner is handed no tool call (it announces `runs`, a lane the door does not serve yet): what
        # the poll shows is that each of its looks read the instance, and that its slot was given back, or
        # the second poll would meet the first's.
        for _ in range(2):
            polled = await daemon.poll(door)
            assert polled.status_code == 200, polled.text
            assert (polled.json()["stop"], polled.json()["calls"]) == (False, [])
        daemon_kit.refused(await daemon.refuse(door, UNKNOWN_CALL), "not_found", 404)


async def test_the_platform_s_sweep_closes_what_ran_out_at_a_personal_runner(runner: dict[str, str]) -> None:
    """The sweep names no person and is not the door: it closes a personal runner's calls under the
    platform's scope, as it closes an organisation's outpost's."""
    from tranibor_api.db.session import name_the_outpost, session_scope
    from tranibor_api.outposts import rendezvous

    hers, north = runner["ada-outpost"], runner["north-outpost"]
    for outpost in (hers, north):
        async with session_scope(orgs="*") as session:
            await name_the_outpost(session, outpost)
            await session.execute(text("UPDATE outpost_calls SET expires_at = now() - interval '1 minute'"))
    assert await rendezvous.sweep() == 2, "the call of each outpost ran out"
    assert await _call_state(hers, "call-ada") == "expired"
    assert await _call_state(north, "call-north") == "expired"
