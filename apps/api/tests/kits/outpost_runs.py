"""A person's runner as the runs lane's tests need one: machines its owner enrolled and approved through the
door and `/me/runners`, a run of the owner's personal space, an offer placed as the outpost executor will
place it (S87-17), and an offer checked and opened as a daemon does (`x-run-signature.checks`).

No key is written here: the daemons make theirs, the door's come from `kits.outposts.switch_on`."""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from httpx import AsyncClient, Response
from sqlalchemy import select
from tranibor_adapters import sealing
from tranibor_contracts import outpost

from . import outpost_daemon as daemon_kit
from . import outposts as kit
from .outpost_daemon import ENROL, POLL, Daemon

ME = "/api/v1/me/runners"
ADA = "ada@northwind.dev"
BOB = "bob@northwind.dev"
#: Where an offer tells the runner to call the run's API: a run door, here a loopback.
API_URL = "http://127.0.0.1:8000"
#: What a personal outpost's instance says it takes (the contract's example, `runner`).
RUNNER: dict[str, Any] = {
    "version": "0.30.0",
    "modes": ["docker"],
    "subscriptions": ["anthropic"],
    "offer_kinds": ["stage_run", "conversation_turn"],
    "run_capacity": 1,
}

_NUMBERS = itertools.count(1)


def aware(moment: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; PostgreSQL returns UTC ones."""
    return moment if moment is None or moment.tzinfo is not None else moment.replace(tzinfo=UTC)


async def runner_daemon(
    client: AsyncClient, door: AsyncClient | None = None, *, org: str = kit.ORG, **enrolment: Any
) -> Daemon:
    """A machine of the signed-in person, enrolled through the door with protocol 2 and `runs`, and approved
    by that person against its fingerprint through `/me/runners`."""
    door = door or client
    issued = await client.post(f"{ME}/{org}/enrolments")
    assert issued.status_code == 201, issued.text
    daemon = Daemon()
    body = daemon.enrolment(issued.json()["token"], **({"protocol": 2, "capabilities": ["runs"]} | enrolment))
    enrolled = await daemon.send(door, ENROL, body, kid=None)
    assert enrolled.status_code == 201, enrolled.text
    daemon.instance_id = enrolled.json()["instance_id"]
    listing = (await client.get(ME)).json()["items"]
    fingerprint = next(
        instance["fingerprint"]
        for runner in listing
        for instance in runner["instances"]
        if instance["id"] == daemon.instance_id
    )
    approval = f"{ME}/instances/{daemon.instance_id}/approval"
    approved = await client.post(approval, json={"fingerprint": fingerprint})
    assert approved.status_code == 200, approved.text
    return daemon


def runs_body(daemon: Daemon, **changes: Any) -> dict[str, Any]:
    """A poll of protocol 2 that announces `runs` and takes no call: what a runner sends."""
    runner = RUNNER | changes.pop("runner", {})
    return daemon.poll_body(
        protocol=2, capabilities=["runs"], endpoints=[], capacity=0, runner=runner, **changes
    )


async def poll_runs(daemon: Daemon, client: AsyncClient, **changes: Any) -> Response:
    return await daemon.send(client, POLL, runs_body(daemon, **changes))


async def handed(daemon: Daemon, client: AsyncClient, **changes: Any) -> dict[str, Any]:
    """Poll as a runner, and return the answer, checked against the contract."""
    response = await poll_runs(daemon, client, **changes)
    assert response.status_code == 200, response.text
    answer = dict(response.json())
    daemon_kit.is_a("PollAnswer", answer)
    outpost.OutpostPollAnswer.model_validate(answer)
    return answer


def run_verifies(daemon: Daemon, offer: dict[str, Any], pinned: list[str]) -> bool:
    """What `x-run-signature.checks` asks of a daemon before it opens an offer, in its order: the header is
    the run's and its `kid` a pinned key, the signature verifies under that key, and its claims are this
    offer, this instance, this envelope, and no other."""
    signature = offer.get("signature")
    if not isinstance(signature, str):
        return False
    try:
        header = jwt.get_unverified_header(signature)
    except jwt.InvalidTokenError:
        return False
    key = header.get("kid")
    if header != outpost.run_signature_header(str(key)) or key not in pinned:
        return False
    try:
        claims = jwt.decode(
            signature,
            Ed25519PublicKey.from_public_bytes(daemon_kit.unb64(str(key))),
            algorithms=[outpost.SIGNATURE_ALGORITHM],
        )
    except jwt.InvalidTokenError:
        return False
    expected = outpost.run_signature_claims(
        run_id=offer["id"], instance_id=str(daemon.instance_id), sealed=offer["sealed"]
    )
    return bool(claims == expected)


def opened(daemon: Daemon, offer: dict[str, Any]) -> dict[str, Any]:
    """The run inside an element of a poll's `runs`: verified under the door's key in service, then opened
    with this instance's sealing key under the offer's id, and what the contract says an offer is."""
    from tranibor_api.config import get_settings

    daemon_kit.is_a("SealedRun", offer)
    assert run_verifies(daemon, offer, get_settings().outposts_door_keys[:1]), "the run's signature"
    assert not daemon.verifies(offer, get_settings().outposts_door_keys[:1]), "a run's signature is no call's"
    run = sealing.open_sealed(daemon.sealing, offer["id"], offer["sealed"])
    daemon_kit.is_the_schema("outpost-run.schema.json", run)
    assert (run["id"], run["kind"]) == (offer["id"], offer["kind"])
    outpost.OutpostRun.model_validate(run)
    return dict(run)


@dataclass(frozen=True)
class PersonalRun:
    """A run of a person's personal space, queued, as the orchestrator leaves one before it is offered."""

    id: str
    project_id: str
    project_slug: str
    work_item_key: str
    owner_id: str


async def a_run_of(email: str = ADA, org: str = kit.ORG) -> PersonalRun:
    """A queued run of the person's personal space (ADR 0134), created as the platform."""
    import tranibor_contracts
    from tranibor_api.db.models import Organization, Run, User, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.personal_spaces import personal_space

    number = next(_NUMBERS)
    example = tranibor_contracts.schemas_dir() / "examples" / "stage-input.example.json"
    async with session_scope(orgs="*") as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        organisation = (
            await session.execute(select(Organization).where(Organization.slug == org))
        ).scalar_one()
        space = await personal_space(session, organisation, user)
        item = WorkItem(
            project_id=space.id,
            tracker_key=f"personal#{number}",
            title="Tidy the notes of the week",
            state="in_progress",
        )
        session.add(item)
        await session.flush()
        run_id = f"{space.slug}-{number}-t-implement-1"
        session.add(
            Run(
                id=run_id,
                work_item_id=item.id,
                project_id=space.id,
                stage_role="implement",
                transition_id="t-implement",
                attempt=1,
                status="queued",
                stage_input={**json.loads(example.read_text(encoding="utf-8")), "run_id": run_id},
                allowed_paths=[],
            )
        )
        await session.flush()
        return PersonalRun(
            id=run_id,
            project_id=str(space.id),
            project_slug=str(space.slug),
            work_item_key=str(item.tracker_key),
            owner_id=str(user.id),
        )


async def runner_of(owner_id: str) -> Any:
    """The person's runner (`outposts` row), read as the platform."""
    from tranibor_api.db.models import Outpost
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return (await session.execute(select(Outpost).where(Outpost.owner_user_id == owner_id))).scalar_one()


async def offer(run: PersonalRun, **changes: Any) -> Any:
    """Place the run's offer on its owner's runner as the outpost executor will (S87-17): under the
    platform's scope, as the orchestrator works."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import outpost_runs
    from tranibor_contracts.enums import RunCredential, SubscriptionProvider

    arguments: dict[str, Any] = {
        "offer_id": run.id,
        "project_id": run.project_id,
        "project_slug": run.project_slug,
        "work_item_key": run.work_item_key,
        "api_url": API_URL,
        "credential": RunCredential.SUBSCRIPTION,
        "provider": SubscriptionProvider.ANTHROPIC,
        "isolation": "container",
        "env": {"TRANIBOR_ENV": "personal"},
        "max_minutes": 30,
        "lease_s": 60,
    } | changes
    outpost_row = await runner_of(run.owner_id)
    async with session_scope(orgs="*") as session:
        return await outpost_runs.offer_run(session, outpost_row, **arguments)


async def offer_row(run_id: str, owner_id: str) -> Any:
    """The offer's row, read under its outpost's name as the platform."""
    from tranibor_api.db.models import OutpostRun

    outpost_row = await runner_of(owner_id)
    rows = [row for row in await kit.rows_of(outpost_row.slug, OutpostRun) if row.id == run_id]
    assert len(rows) == 1, f"{len(rows)} rows for the offer {run_id}"
    return rows[0]


async def change_offer(run_id: str, owner_id: str, **values: Any) -> None:
    """Write the offer's row by hand: what time, or a crash, would have done."""
    from tranibor_api.db.models import OutpostRun

    outpost_row = await runner_of(owner_id)
    await daemon_kit.change(OutpostRun, run_id, outpost_row.slug, **values)


async def run_row(run_id: str) -> Any:
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        run = await session.get(Run, run_id)
        assert run is not None
        return run
