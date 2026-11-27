"""A person's own outpost, enrolled and approved by its owner only (S87-15, ADR 0226 §1-2).

A personal runner is a personal outpost: an outpost of the organisation that one person owns
(`outposts.owner_user_id`), one per person and organisation, announcing `runs` and nothing else. Its owner
creates it, asks for the enrolment token and approves each machine against its fingerprint through
`/me/runners`; no other member sees it there. Every administrator route under
`/orgs/{org}/outposts/{slug}/` that reaches its instances, its enrolment tokens or its calls refuses it
(403 `personal_outpost`): an administrator who could enrol a machine of theirs into it would receive the
person's runs. The routes on the outpost itself (list, get, pause, revoke, delete) accept it, and a pause
is lifted by the side that made it. `TRANIBOR_PERSONAL_RUNNERS` gates `/me/runners`: on in the personal
posture, off elsewhere unless an operator turns it on. Row-level security, which hides the rows of a
person's runner from every other person, is `test_personal_outposts_postgres.py`'s.
"""

from __future__ import annotations

from typing import Any

import pytest
import tranibor_contracts as contracts
from httpx import ASGITransport, AsyncClient
from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .conftest import login
from .kits import outposts as kit
from .kits.outposts import BASE, ORG

ME = "/api/v1/me/runners"
ADA = "ada@northwind.dev"
BOB = "bob@northwind.dev"
FINGERPRINT = {"fingerprint": "SHA256:" + "a" * 43}

#: How every administrator route of `routers/outposts.py` treats a person's runner (ADR 0226 §2), by
#: operation: refused, because it reaches an instance, an enrolment token or a call of the runner; or
#: accepted, because it is a route on the outpost itself (or names none). A route added to the router and
#: left out of both sets turns `test_every_admin_route_is_classified_for_a_personal_outpost` red.
REFUSED = frozenset(
    {
        "createOutpostEnrolment",
        "listOutpostEnrolments",
        "cancelOutpostEnrolment",
        "listOutpostInstances",
        "approveOutpostInstance",
        "revokeOutpostInstance",
        "listOutpostCalls",
    }
)
ACCEPTED = frozenset(
    {
        "listOutposts",
        "createOutpost",
        "getOutpost",
        "pauseOutpost",
        "resumeOutpost",
        "revokeOutpost",
        "deleteOutpost",
    }
)


def _valid(name: str, body: Any) -> None:
    """The body is what the contract's schema of that name describes."""
    spec = contracts.load_openapi()
    schema = {"$ref": f"#/components/schemas/{name}", "components": spec["components"]}
    errors = [error.message for error in Draft202012Validator(schema).iter_errors(body)]
    assert not errors, f"not a contract `{name}`: {errors}"


def _coded(response: Any, code: str, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = dict(response.json())
    assert body["code"] == code, body
    return body


@pytest.fixture
def runners(monkeypatch: pytest.MonkeyPatch) -> kit.Door:
    """Outposts on with a door that has its keys, and personal runners on (`TRANIBOR_PERSONAL_RUNNERS`)."""
    door = kit.switch_on(monkeypatch)
    kit.configure(monkeypatch, personal_runners="on")
    return door


async def _enrol_my_machine(client: AsyncClient) -> Any:
    """The caller asks for an enrolment token of their runner, and a daemon enrols with it as the door
    lets it: a pending instance (`services.outposts.Enrolled`)."""
    issued = await client.post(f"{ME}/{ORG}/enrolments")
    assert issued.status_code == 201, issued.text
    _valid("OutpostEnrolmentCreated", issued.json())
    return await kit.enrol(issued.json()["token"])


async def _personal_rows() -> list[Any]:
    from tranibor_api.db.models import Outpost
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return list(
            (await session.execute(select(Outpost).where(Outpost.owner_user_id.is_not(None)))).scalars()
        )


async def _instance_status(slug: str, instance_id: str) -> str:
    from tranibor_api.db.models import OutpostInstance

    (row,) = [row for row in await kit.rows_of(slug, OutpostInstance) if row.id == instance_id]
    return str(row.status)


# ───────────────────────── the owner's flow ─────────────────────────


async def test_the_owners_whole_flow(client: AsyncClient, app: Any, org: str, runners: kit.Door) -> None:
    """Created on the first enrolment token, a machine enrolled, compared and approved, paused, resumed and
    revoked, by its owner and through `/me/runners` alone; each step answers what the contract says."""
    await login(client, ADA)
    assert (await client.get(ME)).json() == {"items": []}

    enrolled = await _enrol_my_machine(client)
    assert enrolled.outpost.capabilities == ["runs"], "a personal outpost announces runs and nothing else"
    assert enrolled.instance.status == "pending"
    listing = (await client.get(ME)).json()
    _valid("PersonalRunnerList", listing)
    (runner,) = listing["items"]
    assert runner | {"created_at": None, "instances": None} == {
        "org": ORG,
        "slug": enrolled.outpost.slug,
        "owner": ADA,
        "status": "active",
        "paused_by": None,
        "online": False,
        "last_seen_at": None,
        "instances": None,
        "runs_waiting": 0,
        "created_at": None,
    }
    (instance,) = runner["instances"]
    assert (instance["id"], instance["status"]) == (enrolled.instance.id, "pending")
    assert instance["fingerprint"] == enrolled.instance.fingerprint
    assert (await client.get(ME, params={"org": "elsewhere"})).json() == {"items": []}
    assert (await client.get(ME, params={"org": ORG})).json()["items"] == listing["items"]

    approval = f"{ME}/instances/{enrolled.instance.id}/approval"
    _coded(await client.post(approval, json=FINGERPRINT), "fingerprint_mismatch", 409)
    # An API token of the owner is not a person's session (ADR 0147 §4): it does not approve.
    token = (await client.post("/api/v1/me/tokens", json={"name": "ci", "scopes": ["*"]})).json()["token"]
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test", headers={"Authorization": f"Bearer {token}"}
    ) as machine:
        refused = await machine.post(approval, json={"fingerprint": instance["fingerprint"]})
        _coded(refused, "approval_requires_session", 403)
    approved = await client.post(approval, json={"fingerprint": instance["fingerprint"]})
    assert approved.status_code == 200, approved.text
    _valid("PersonalRunnerInstance", approved.json())
    assert approved.json()["status"] == "active" and approved.json()["approved_at"] is not None
    _coded(
        await client.post(approval, json={"fingerprint": instance["fingerprint"]}),
        "instance_not_pending",
        409,
    )

    paused = await client.post(f"{ME}/{ORG}/pause")
    assert paused.status_code == 200, paused.text
    _valid("PersonalRunner", paused.json())
    assert (paused.json()["status"], paused.json()["paused_by"]) == ("paused", "owner")
    resumed = (await client.post(f"{ME}/{ORG}/resume")).json()
    assert (resumed["status"], resumed["paused_by"]) == ("active", None)
    revoked = await client.post(f"{ME}/{ORG}/revocation")
    assert revoked.status_code == 200, revoked.text
    _valid("PersonalRunner", revoked.json())
    assert revoked.json()["status"] == "revoked"
    assert [i["status"] for i in revoked.json()["instances"]] == ["revoked"]
    assert (await client.post(f"{ME}/{ORG}/revocation")).json()["status"] == "revoked", "revoking is final"
    _coded(await client.post(f"{ME}/{ORG}/enrolments"), "outpost_revoked", 409)

    actions = [
        (entry.action, entry.target_type)
        for name in ("outpost.create", "outpost.approve", "outpost.pause", "outpost.resume", "outpost.revoke")
        for entry in await kit.audit_rows(name)
    ]
    assert actions == [
        ("outpost.create", "outpost"),
        ("outpost.approve", "outpost_instance"),
        ("outpost.pause", "outpost"),
        ("outpost.resume", "outpost"),
        ("outpost.revoke", "outpost"),
    ]
    issued = [e for e in await kit.audit_rows("outpost.enrol") if e.target_type == "outpost_enrolment"]
    assert len(issued) == 1 and issued[0].payload["outpost"] == enrolled.outpost.slug
    (approval_entry,) = await kit.audit_rows("outpost.approve")
    assert approval_entry.payload["fingerprint"] == instance["fingerprint"]


async def test_one_runner_per_person_and_organisation(
    client: AsyncClient, org: str, runners: kit.Door
) -> None:
    """Every enrolment token of a person's machines is for the same runner, as `uq_projects_personal_owner`
    keeps one personal space; the database itself refuses a second."""
    from tranibor_api.db.models import Outpost
    from tranibor_api.db.session import session_scope

    await login(client, ADA)
    first = await _enrol_my_machine(client)
    second = await _enrol_my_machine(client)
    assert first.outpost.id == second.outpost.id
    (row,) = await _personal_rows()
    assert row.id == first.outpost.id and row.capabilities == ["runs"]
    assert len(await kit.audit_rows("outpost.create")) == 1
    listing = (await client.get(ME)).json()["items"]
    assert [len(runner["instances"]) for runner in listing] == [2]

    with pytest.raises(IntegrityError):
        async with session_scope(orgs="*") as session:
            session.add(
                Outpost(
                    org_id=row.org_id,
                    slug="another-runner",
                    display_name="another",
                    capabilities=["runs"],
                    owner_user_id=row.owner_user_id,
                )
            )
            await session.flush()


# ───────────────────────── another member, an administrator ─────────────────────────


async def test_a_member_never_sees_another_members_runner(
    client: AsyncClient, org: str, runners: kit.Door
) -> None:
    """Bob, a member of the same organisation, neither lists Ada's runner nor reaches its instance: 404, as
    for an instance that does not exist; and what he enrols is a runner of his own."""
    await login(client, ADA)
    ada = await _enrol_my_machine(client)

    await login(client, BOB)
    assert (await client.get(ME)).json() == {"items": []}
    assert (await client.get(ME, params={"org": ORG})).json() == {"items": []}
    stolen = await client.post(
        f"{ME}/instances/{ada.instance.id}/approval", json={"fingerprint": ada.instance.fingerprint}
    )
    assert stolen.status_code == 404, stolen.text
    for verb in ("pause", "resume", "revocation"):
        assert (await client.post(f"{ME}/{ORG}/{verb}")).status_code == 404, verb

    bob = await _enrol_my_machine(client)
    assert bob.outpost.id != ada.outpost.id and bob.outpost.slug != ada.outpost.slug
    (mine,) = (await client.get(ME)).json()["items"]
    assert mine["owner"] == BOB and [i["id"] for i in mine["instances"]] == [bob.instance.id]
    # Bob's own approval route does not reach Ada's instance either, whatever the id says.
    approved = await client.post(
        f"{ME}/instances/{ada.instance.id}/approval", json={"fingerprint": ada.instance.fingerprint}
    )
    assert approved.status_code == 404, approved.text
    assert await _instance_status(ada.outpost.slug, ada.instance.id) == "pending"

    await login(client, ADA)
    (hers,) = (await client.get(ME)).json()["items"]
    assert hers["owner"] == ADA and [i["id"] for i in hers["instances"]] == [ada.instance.id]


async def test_an_admin_cannot_approve_a_personal_instance(
    client: AsyncClient, org: str, runners: kit.Door
) -> None:
    """The organisation's administrator lists a person's runner, and cannot enrol a machine into it or
    approve one of its instances, however right the fingerprint: 403 `personal_outpost`."""
    await login(client, ADA)
    ada = await _enrol_my_machine(client)
    slug = ada.outpost.slug

    await login(client, "admin@northwind.dev")
    listed = (await client.get(BASE)).json()["items"]
    assert [(o["slug"], o["capabilities"]) for o in listed] == [(slug, ["runs"])]
    refused = await client.post(
        f"{BASE}/{slug}/instances/{ada.instance.id}/approve", json={"fingerprint": ada.instance.fingerprint}
    )
    body = _coded(refused, "personal_outpost", 403)
    assert ADA not in body["detail"], "the refusal names no person"
    _coded(await client.post(f"{BASE}/{slug}/enrolments"), "personal_outpost", 403)
    assert await _instance_status(slug, ada.instance.id) == "pending"
    assert await kit.audit_rows("outpost.approve") == []
    issued = [e for e in await kit.audit_rows("outpost.enrol") if e.target_type == "outpost_enrolment"]
    assert len(issued) == 1, "the administrator issued no enrolment token"


async def test_every_admin_route_is_classified_for_a_personal_outpost(
    client: AsyncClient, org: str, runners: kit.Door
) -> None:
    """Read from the route table of `routers/outposts.py`: each route is refused or accepted on a person's
    runner, and does what its class says. A route added later and classified nowhere fails here."""
    from tranibor_api.routers import outposts as admin_routes

    served = {route.operation_id for route in admin_routes.router.routes if hasattr(route, "operation_id")}
    assert not REFUSED & ACCEPTED
    unclassified, gone = sorted(served - REFUSED - ACCEPTED), sorted((REFUSED | ACCEPTED) - served)
    assert not unclassified and not gone, (
        f"classify each administrator route for a personal outpost: unclassified {unclassified}, gone {gone}"
    )

    await login(client, ADA)
    ada = await _enrol_my_machine(client)
    one = f"{BASE}/{ada.outpost.slug}"
    instance = ada.instance.id
    await login(client, "admin@northwind.dev")
    refused: dict[str, tuple[str, str, Any]] = {
        "createOutpostEnrolment": ("POST", f"{one}/enrolments", None),
        "listOutpostEnrolments": ("GET", f"{one}/enrolments", None),
        "cancelOutpostEnrolment": ("DELETE", f"{one}/enrolments/{instance}", None),
        "listOutpostInstances": ("GET", f"{one}/instances", None),
        "approveOutpostInstance": ("POST", f"{one}/instances/{instance}/approve", FINGERPRINT),
        "revokeOutpostInstance": ("POST", f"{one}/instances/{instance}/revoke", None),
        "listOutpostCalls": ("GET", f"{one}/calls", None),
    }
    assert set(refused) == REFUSED
    for operation, (method, path, body) in refused.items():
        answered = await client.request(method, path, json=body)
        _coded(answered, "personal_outpost", 403)
        assert "/me/runners" in answered.json()["detail"], operation
    assert await _instance_status(ada.outpost.slug, instance) == "pending", "nothing moved"

    # The routes on the outpost itself, in an order that ends with it gone.
    accepted: list[tuple[str, str, str, Any, int]] = [
        ("listOutposts", "GET", BASE, None, 200),
        ("getOutpost", "GET", one, None, 200),
        ("pauseOutpost", "POST", f"{one}/pause", None, 200),
        ("resumeOutpost", "POST", f"{one}/resume", None, 200),
        ("revokeOutpost", "POST", f"{one}/revoke", None, 200),
        ("deleteOutpost", "DELETE", one, None, 204),
        ("createOutpost", "POST", BASE, {"slug": "factory-floor"}, 201),
    ]
    assert {operation for operation, *_ in accepted} == ACCEPTED
    for operation, method, path, body, status in accepted:
        answered = await client.request(method, path, json=body)
        assert answered.status_code == status, (operation, answered.text)
    assert await _personal_rows() == [], "an administrator deletes a revoked runner"


async def test_a_pause_is_lifted_by_the_side_that_made_it(
    client: AsyncClient, org: str, runners: kit.Door
) -> None:
    """An administrator's pause is lifted by `resumeOutpost`, its owner's by `resumeMyRunner`, and neither
    lifts the other's; an administrator's pause over its owner's takes it over."""
    await login(client, ADA)
    ada = await _enrol_my_machine(client)
    one = f"{BASE}/{ada.outpost.slug}"

    await login(client, "admin@northwind.dev")
    assert (await client.post(f"{one}/pause")).json()["status"] == "paused"
    await login(client, ADA)
    assert (await client.get(ME)).json()["items"][0]["paused_by"] == "administrator"
    assert (await client.post(f"{ME}/{ORG}/pause")).json()["paused_by"] == "administrator", "changes nothing"
    refused = await client.post(f"{ME}/{ORG}/resume")
    assert refused.status_code == 403, refused.text
    await login(client, "admin@northwind.dev")
    assert (await client.post(f"{one}/resume")).json()["status"] == "active"

    await login(client, ADA)
    assert (await client.post(f"{ME}/{ORG}/pause")).json()["paused_by"] == "owner"
    await login(client, "admin@northwind.dev")
    _coded(await client.post(f"{one}/resume"), "personal_outpost", 403)
    assert (await client.post(f"{one}/pause")).status_code == 200
    await login(client, ADA)
    assert (await client.get(ME)).json()["items"][0]["paused_by"] == "administrator", "taken over"
    assert (await client.post(f"{ME}/{ORG}/resume")).status_code == 403
    await login(client, "admin@northwind.dev")
    assert (await client.post(f"{one}/resume")).json()["status"] == "active"
    await login(client, ADA)
    assert (await client.get(ME)).json()["items"][0] | {"instances": None, "created_at": None} == {
        "org": ORG,
        "slug": ada.outpost.slug,
        "owner": ADA,
        "status": "active",
        "paused_by": None,
        "online": False,
        "last_seen_at": None,
        "instances": None,
        "runs_waiting": 0,
        "created_at": None,
    }
    sides = [entry.payload.get("side") for entry in await kit.audit_rows("outpost.pause")]
    assert sides == ["administrator", "owner", "administrator"]


async def test_an_organisation_outpost_never_takes_a_personal_outposts_slug(
    client: AsyncClient, admin: str, runners: kit.Door
) -> None:
    """`personal-` names a person's runner: an administrator who took one first would leave that person
    without a runner, so the prefix is refused, as a personal space's is for projects."""
    refused = await client.post(BASE, json={"slug": "personal-0123456789ab"})
    assert refused.status_code == 422, refused.text
    assert (await client.get(BASE)).json()["items"] == []


# ───────────────────────── the switch ─────────────────────────


async def test_personal_runners_are_off_outside_the_personal_posture(
    client: AsyncClient, org: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`TRANIBOR_PERSONAL_RUNNERS` follows the posture unless an operator sets it: off, every route of
    `/me/runners` is a 404, as if it were not there."""
    from tranibor_api.config import Settings

    kit.switch_on(monkeypatch)
    await login(client, ADA)
    instance = "0" * 64
    routes = [
        ("GET", ME),
        ("POST", f"{ME}/{ORG}/enrolments"),
        ("POST", f"{ME}/instances/{instance}/approval"),
        ("POST", f"{ME}/{ORG}/pause"),
        ("POST", f"{ME}/{ORG}/resume"),
        ("POST", f"{ME}/{ORG}/revocation"),
    ]
    for method, path in routes:
        body = FINGERPRINT if path.endswith("/approval") else None
        assert (await client.request(method, path, json=body)).status_code == 404, path
    assert await _personal_rows() == []

    def posture(env: str, **settings: Any) -> bool:
        return Settings(env=env, dev_login_enabled=False, **settings).personal_runners_on  # type: ignore[arg-type]

    assert posture("personal") is True
    assert (posture("dev"), posture("test"), posture("staging"), posture("prod")) == (False,) * 4
    assert posture("prod", personal_runners=True) is True, "an operator may turn them on"
    assert posture("personal", personal_runners=False) is False, "and off"
