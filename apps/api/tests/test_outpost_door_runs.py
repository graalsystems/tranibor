"""The door's runs lane: an offer claimed once, the run token minted at the claim (S87-16, ADR 0226 §3-5).

A run placed on a person's runner is a row of `outpost_runs`, an offer. A poll of door protocol 2 from an
instance of that person's runner, which announces `runs` and says what it takes (`runner`), claims offers
of a kind, a mode and a provider it announced, within its `run_capacity`, with `FOR UPDATE SKIP LOCKED` and
a compare-and-set: one claim, never handed over again, to any instance. Only then does the door mint the
run token, seal it with the assignment to the claiming instance's key under the run's id and sign the
envelope (`x-run-signature`); the row never holds the token. A poll that lists the run in `runs_running`
renews its lease, `runs_ended` records its exit code, and a lease that lapses, or a revocation, loses the
offer: the run is then closed through ADR 0143's closure, and its API answers 409 `run_closed`. An
organisation's outpost still announces no `runs`, and a daemon of protocol 1 is never handed an offer.
Row-level security of `outpost_runs` is the PostgreSQL cases' at the end (`TRANIBOR_TEST_DATABASE_URL`).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from tranibor_adapters import sealing
from tranibor_contracts import load_outpost_door
from tranibor_contracts.enums import RunCredential, SubscriptionProvider

from .conftest import login, without_postgres
from .kits import outpost_daemon as daemon_kit
from .kits import outpost_runs as runs_kit
from .kits import outposts as kit
from .kits.outpost_daemon import ENROL, POLL, SLUG, Daemon, refused
from .kits.outpost_runs import ADA, BOB, ME, RUNNER, aware

RUNS = "/api/v1/internal/runs"


@pytest.fixture(autouse=True)
def door(monkeypatch: pytest.MonkeyPatch) -> kit.Door:
    """Outposts and personal runners on, the door given its origin and its key, the deployment its key
    wrapper."""
    daemon_kit.key_wrapper(monkeypatch)
    door = kit.switch_on(monkeypatch)
    kit.configure(monkeypatch, personal_runners="on")
    return door


@pytest.fixture
async def ada(client: AsyncClient, org: str) -> str:
    """Ada, a member of the organisation, signed in."""
    await login(client, ADA)
    return ADA


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _is_run_closed(response: Any) -> bool:
    return response.status_code == 409 and response.json()["code"] == "run_closed"


# ───────────────────────── an offer claimed once ─────────────────────────


async def test_two_instances_one_claim(client: AsyncClient, ada: str) -> None:
    """Two of Ada's machines poll for the same offer: one is handed it, the other never is, and the one
    that holds it is not handed a second copy. Her list of runners shows the offer waiting, then run."""
    first, second = await runs_kit.runner_daemon(client), await runs_kit.runner_daemon(client)
    run = await runs_kit.a_run_of()
    placed = await runs_kit.offer(run)
    assert (placed.state, placed.created, placed.instance_id) == ("offered", True, None)
    (listed,) = (await client.get(ME)).json()["items"]
    assert listed["runs_waiting"] == 1 and [i["runs_running"] for i in listed["instances"]] == [0, 0]

    answer = await runs_kit.handed(first, client)
    (element,) = answer["runs"]
    assert (element["id"], element["kind"]) == (run.id, "stage_run")
    assert answer["cancel_runs"] == []
    assert runs_kit.opened(first, element)["run_id"] == run.id
    row = await runs_kit.offer_row(run.id, run.owner_id)
    assert (row.state, row.instance_id) == ("claimed", first.instance_id)
    assert aware(row.claimed_at) is not None and aware(row.lease_until) is not None

    assert (await runs_kit.handed(second, client))["runs"] == [], "the other machine is handed nothing"
    (listed,) = (await client.get(ME)).json()["items"]
    running = {instance["id"]: instance["runs_running"] for instance in listed["instances"]}
    assert listed["runs_waiting"] == 0 and running == {first.instance_id: 1, second.instance_id: 0}
    again = await runs_kit.handed(first, client, runs_running=[run.id])
    assert (again["runs"], again["cancel_runs"]) == ([], []), "the holder gets no second copy"
    # The offer was placed once: placing it again (a replayed activity) meets the first row.
    replayed = await runs_kit.offer(run)
    assert (replayed.created, replayed.state, replayed.instance_id) == (False, "claimed", first.instance_id)


async def test_a_claimed_offer_is_never_handed_over_again(client: AsyncClient, ada: str) -> None:
    """Not after its lease lapsed, not to another machine, and not even if a hand put its row back to
    `offered`: the platform cannot know whether a sleeping laptop started the agent (ADR 0149 §5)."""
    holder, other = await runs_kit.runner_daemon(client), await runs_kit.runner_daemon(client)
    run = await runs_kit.a_run_of()
    await runs_kit.offer(run)
    (element,) = (await runs_kit.handed(holder, client))["runs"]
    assert element["id"] == run.id
    await runs_kit.change_offer(run.id, run.owner_id, state="offered")
    for poller in (holder, other):
        assert (await runs_kit.handed(poller, client))["runs"] == [], (
            "a row ever claimed is not claimed again"
        )
    await runs_kit.change_offer(
        run.id, run.owner_id, state="claimed", lease_until=datetime.now(UTC) - timedelta(seconds=1)
    )
    for poller in (other, holder):
        assert (await runs_kit.handed(poller, client))["runs"] == []
    assert (await runs_kit.offer_row(run.id, run.owner_id)).state == "lost"


async def test_protocol_1_never_gets_a_run(client: AsyncClient, ada: str) -> None:
    """A machine of Ada's runner whose daemon speaks protocol 1 announces no `runs` (its schema refuses it)
    and is handed no offer, nor any field of the lane; the offer waits for a machine that takes it."""
    older = await runs_kit.runner_daemon(client, protocol=1, capabilities=["tools"])
    run = await runs_kit.a_run_of()
    await runs_kit.offer(run)
    response = await older.poll(client, protocol=1)
    assert response.status_code == 200, response.text
    assert "runs" not in response.json() and "cancel_runs" not in response.json()
    problem = refused(await older.poll(client, capabilities=["runs"], runner=RUNNER), "invalid_request", 422)
    assert ("capabilities",) in {tuple(error["loc"]) for error in problem["errors"]}
    assert (await runs_kit.offer_row(run.id, run.owner_id)).state == "offered"
    # A machine of protocol 2 takes it.
    newer = await runs_kit.runner_daemon(client)
    assert [element["id"] for element in (await runs_kit.handed(newer, client))["runs"]] == [run.id]


# ───────────────────────── the token, minted at the claim, sealed ─────────────────────────


async def test_the_token_is_only_in_the_sealed_envelope(client: AsyncClient, ada: str) -> None:
    """The offer's row holds no token before the claim and none after it; the token travels inside the
    envelope sealed to the claiming machine under the run's id, speaks for that run, and lapses with it."""
    from tranibor_api.security import verify_run_token

    daemon = await runs_kit.runner_daemon(client)
    run = await runs_kit.a_run_of()
    await runs_kit.offer(run, env={"TRANIBOR_ENV": "personal", "HTTPS_PROXY": "http://egress:3128"})
    response = await runs_kit.poll_runs(daemon, client)
    assert response.status_code == 200, response.text
    (element,) = response.json()["runs"]
    minted_after = datetime.now(UTC)
    opened = runs_kit.opened(daemon, element)
    token = opened["run_token"]
    assert token not in response.text, "the answer carries the token inside the envelope only"
    assert opened | {"run_token": None} == {
        "id": run.id,
        "kind": "stage_run",
        "run_id": run.id,
        "conversation_id": None,
        "turn_id": None,
        "api_url": runs_kit.API_URL,
        "run_token": None,
        "env": {"TRANIBOR_ENV": "personal", "HTTPS_PROXY": "http://egress:3128"},
        "credential": "subscription",
        "provider": "anthropic",
        "isolation": "container",
        "lease_s": 60,
        "max_minutes": 30,
    }
    claims = verify_run_token(token)
    assert (claims.run_id, claims.project_slug, claims.work_item_key) == (
        run.id,
        run.project_slug,
        run.work_item_key,
    )
    lapses = claims.expires_at - int(minted_after.timestamp())
    assert (30 + 15) * 60 - 60 <= lapses <= (30 + 15) * 60, "valid the run's maximum minutes plus 15"
    # Sealed under the run's id: moved to another offer, it does not open.
    with pytest.raises(Exception):  # noqa: B017 - the cipher's authentication error
        sealing.open_sealed(daemon.sealing, "another-run", element["sealed"])
    row = await runs_kit.offer_row(run.id, run.owner_id)
    held = {column.name: getattr(row, column.name) for column in row.__table__.columns}
    assert not [name for name, value in held.items() if value is not None and token in str(value)], held
    assert not [
        name for name, value in held.items() if isinstance(value, str) and "." in value and len(value) > 200
    ]


# ───────────────────────── the lease ─────────────────────────


async def test_a_poll_that_omits_a_run_lets_its_lease_lapse(client: AsyncClient, ada: str) -> None:
    """A poll that lists the run renews its lease, never past the claim plus the run's maximum minutes; one
    of another machine renews nothing and is told to stop it. A poll that omits the run lets the lease
    lapse: the offer is lost, the next poll carries it in `cancel_runs`, and the platform closes the run
    through ADR 0143's closure, after which its API answers 409 `run_closed`."""
    from tranibor_api.outposts import rendezvous

    daemon, other = await runs_kit.runner_daemon(client), await runs_kit.runner_daemon(client)
    run = await runs_kit.a_run_of()
    await runs_kit.offer(run, lease_s=60)
    (element,) = (await runs_kit.handed(daemon, client))["runs"]
    token = runs_kit.opened(daemon, element)["run_token"]
    assert (await client.get(f"{RUNS}/{run.id}/input", headers=_bearer(token))).status_code == 200

    # Forty seconds pass: a poll that lists the run moves its lease to sixty seconds from that poll.
    now = datetime.now(UTC)
    await runs_kit.change_offer(
        run.id, run.owner_id, claimed_at=now - timedelta(seconds=40), lease_until=now + timedelta(seconds=20)
    )
    told = await runs_kit.handed(other, client, runs_running=[run.id])
    assert told["cancel_runs"] == [run.id], "another machine's poll renews nothing: it is told to stop"
    row = await runs_kit.offer_row(run.id, run.owner_id)
    assert aware(row.lease_until) <= now + timedelta(seconds=21)
    assert (await runs_kit.handed(daemon, client, runs_running=[run.id]))["cancel_runs"] == []
    row = await runs_kit.offer_row(run.id, run.owner_id)
    assert now + timedelta(seconds=55) <= aware(row.lease_until) <= datetime.now(UTC) + timedelta(seconds=61)
    # Near the run's end the lease stops at the claim plus its maximum minutes.
    claimed = datetime.now(UTC) - timedelta(minutes=30) + timedelta(seconds=10)
    await runs_kit.change_offer(run.id, run.owner_id, claimed_at=claimed)
    await runs_kit.handed(daemon, client, runs_running=[run.id])
    row = await runs_kit.offer_row(run.id, run.owner_id)
    assert abs((aware(row.lease_until) - (claimed + timedelta(minutes=30))).total_seconds()) < 1

    # The lease lapses (the lid closed): the poll that omits the run finds it lost, and is told so.
    await runs_kit.change_offer(run.id, run.owner_id, lease_until=datetime.now(UTC) - timedelta(seconds=1))
    lapsed = await runs_kit.handed(daemon, client)
    assert (lapsed["runs"], lapsed["cancel_runs"]) == ([], [run.id])
    assert (await runs_kit.offer_row(run.id, run.owner_id)).state == "lost"
    assert (await runs_kit.handed(daemon, client))["cancel_runs"] == [], "told once"

    # The platform's sweep closes the run, as the orchestrator closes a run its executor lost.
    await rendezvous.sweep()
    closed = await runs_kit.run_row(run.id)
    assert (closed.status, closed.result["reason"], closed.ended_at is not None) == ("failed", "lost", True)
    assert _is_run_closed(await client.get(f"{RUNS}/{run.id}/input", headers=_bearer(token)))
    # A late renewal changes nothing.
    assert (await runs_kit.handed(daemon, client, runs_running=[run.id]))["cancel_runs"] == [run.id]
    assert (await runs_kit.offer_row(run.id, run.owner_id)).state == "lost"


async def test_a_lost_offer_closes_the_run_and_its_api_answers_run_closed(
    client: AsyncClient, ada: str
) -> None:
    """What the outpost executor reads (S87-17): `status` says a lapsed offer is lost and closes its run with
    the reason `lost` (ADR 0143 §2), once; a result the runner had posted first is kept."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import outpost_runs

    daemon = await runs_kit.runner_daemon(client)
    run = await runs_kit.a_run_of()
    await runs_kit.offer(run)
    (element,) = (await runs_kit.handed(daemon, client))["runs"]
    token = runs_kit.opened(daemon, element)["run_token"]
    outpost_row = await runs_kit.runner_of(run.owner_id)
    async with session_scope(orgs="*") as session:
        seen = await outpost_runs.status(session, outpost_row.id, run.id)
    assert seen is not None and (seen.state, seen.instance_id) == ("claimed", daemon.instance_id)
    await runs_kit.change_offer(run.id, run.owner_id, lease_until=datetime.now(UTC) - timedelta(seconds=1))
    async with session_scope(orgs="*") as session:
        seen = await outpost_runs.status(session, outpost_row.id, run.id)
    assert seen is not None and seen.state == "lost"
    closed = await runs_kit.run_row(run.id)
    assert (closed.status, closed.result["reason"]) == ("failed", "lost")
    assert _is_run_closed(await client.get(f"{RUNS}/{run.id}/input", headers=_bearer(token)))


async def test_runs_ended_records_the_exit_code_and_cancel_runs_carries_a_withdrawn_offer(
    client: AsyncClient, ada: str
) -> None:
    """`runs_ended` ends the offer with its exit code. A claimed offer the platform withdraws is carried in
    `cancel_runs` to the machine that lists it; an offer nobody claimed is withdrawn without a word."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import outpost_runs

    daemon = await runs_kit.runner_daemon(client)
    finished, cancelled, waiting = [await runs_kit.a_run_of() for _ in range(3)]
    await runs_kit.offer(finished)
    assert [e["id"] for e in (await runs_kit.handed(daemon, client))["runs"]] == [finished.id]
    await runs_kit.offer(cancelled)
    assert [e["id"] for e in (await runs_kit.handed(daemon, client))["runs"]] == [cancelled.id]
    answer = await runs_kit.handed(
        daemon, client, runs_running=[cancelled.id], runs_ended=[{"id": finished.id, "exit_code": 3}]
    )
    assert (answer["runs"], answer["cancel_runs"]) == ([], [])
    row = await runs_kit.offer_row(finished.id, finished.owner_id)
    assert (row.state, row.exit_code) == ("ended", 3)

    outpost_row = await runs_kit.runner_of(cancelled.owner_id)
    await runs_kit.offer(waiting)
    async with session_scope(orgs="*") as session:
        assert await outpost_runs.withdraw(session, outpost_row.id, cancelled.id) == "cancelled"
        assert await outpost_runs.withdraw(session, outpost_row.id, waiting.id) == "cancelled"
        assert await outpost_runs.withdraw(session, outpost_row.id, finished.id) == "ended", "it ended first"
        assert await outpost_runs.withdraw(session, outpost_row.id, "never-offered") is None
    answer = await runs_kit.handed(daemon, client, runs_running=[cancelled.id])
    assert (answer["runs"], answer["cancel_runs"]) == ([], [cancelled.id]), "the withdrawn one, not waiting"
    await runs_kit.handed(daemon, client, runs_ended=[{"id": cancelled.id, "exit_code": 143}])
    row = await runs_kit.offer_row(cancelled.id, cancelled.owner_id)
    assert (row.state, row.exit_code) == ("cancelled", 143)


async def test_revocation_loses_the_runs_it_held_at_once(client: AsyncClient, ada: str) -> None:
    """Ada revokes her runner while one of its machines runs an offer: the offer is lost at once, with no
    poll, the run is closed with the reason `lost`, and the machine's next poll is refused."""
    daemon = await runs_kit.runner_daemon(client)
    run, waiting = await runs_kit.a_run_of(), await runs_kit.a_run_of()
    await runs_kit.offer(run)
    (element,) = (await runs_kit.handed(daemon, client))["runs"]
    token = runs_kit.opened(daemon, element)["run_token"]
    await runs_kit.offer(waiting)
    revoked = await client.post(f"{ME}/{kit.ORG}/revocation")
    assert revoked.status_code == 200 and revoked.json()["status"] == "revoked", revoked.text
    row = await runs_kit.offer_row(run.id, run.owner_id)
    assert (row.state, row.instance_id) == ("lost", daemon.instance_id)
    closed = await runs_kit.run_row(run.id)
    assert (closed.status, closed.result["reason"]) == ("failed", "lost")
    assert _is_run_closed(await client.get(f"{RUNS}/{run.id}/input", headers=_bearer(token)))
    refused(await runs_kit.poll_runs(daemon, client, runs_running=[run.id]), "instance_revoked", 403)
    assert (await runs_kit.offer_row(waiting.id, waiting.owner_id)).state == "offered", "nobody held it"
    assert (await runs_kit.run_row(waiting.id)).status == "queued"


# ───────────────────────── what the machine takes ─────────────────────────


async def test_an_instance_without_the_provider_never_claims_a_subscription_offer(
    client: AsyncClient, ada: str
) -> None:
    """A subscription offer goes to a machine that holds the provider's sign-in, a native offer to one that
    runs native, a turn to one that knows turns, and no more offers than a machine can take."""
    daemon = await runs_kit.runner_daemon(client)
    run = await runs_kit.a_run_of()
    await runs_kit.offer(run, provider=SubscriptionProvider.ANTHROPIC)
    for runner in (
        {"subscriptions": []},
        {"subscriptions": ["google"]},
        {"run_capacity": 0},
        {"offer_kinds": ["conversation_turn"]},
    ):
        assert (await runs_kit.handed(daemon, client, runner=runner))["runs"] == [], runner
    native = await runs_kit.a_run_of()
    await runs_kit.offer(native, isolation="process")
    docker_only = await runs_kit.handed(daemon, client, runner={"run_capacity": 2})
    assert [element["id"] for element in docker_only["runs"]] == [run.id], "docker takes `container` only"
    both = await runs_kit.handed(
        daemon, client, runs_running=[run.id], runner={"modes": ["docker", "native"]}
    )
    assert [element["id"] for element in both["runs"]] == [native.id]
    assert runs_kit.opened(daemon, both["runs"][0])["isolation"] == "process"
    # Three waiting offers, a capacity of two: two are handed, the oldest first.
    waiting = [await runs_kit.a_run_of() for _ in range(3)]
    for each in waiting:
        await runs_kit.offer(each)
    two = await runs_kit.handed(daemon, client, runs_running=[run.id, native.id], runner={"run_capacity": 2})
    assert [element["id"] for element in two["runs"]] == [waiting[0].id, waiting[1].id]


async def test_offer_run_refuses_what_a_runner_must_never_receive(client: AsyncClient, ada: str) -> None:
    """An offer carries no secret: a subscription credential or a visible secret in its environment is
    refused, as an envelope above the door's bound; and in phase A an offer is a subscription run, never a
    gateway one, whose key a sleeping laptop would hold (ADR 0226 §9)."""
    from tranibor_api.services import outpost_runs

    await runs_kit.runner_daemon(client)
    run = await runs_kit.a_run_of()
    for env in (
        {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-" + "a" * 80},
        {"OPENAI_API_KEY": "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"},
        {f"PAD_{index}": "x" * 4000 for index in range(20)},
    ):
        with pytest.raises(outpost_runs.NotOffered):
            await runs_kit.offer(run, env=env)
    with pytest.raises(outpost_runs.NotOffered):
        await runs_kit.offer(run, credential=RunCredential.GATEWAY, provider=None)
    with pytest.raises(outpost_runs.NotOffered):
        await runs_kit.offer(run, lease_s=10)
    assert (await runs_kit.offer(run)).created, "nothing was written by the refusals"


# ───────────────────────── an organisation's outpost ─────────────────────────


async def test_an_organisation_outpost_is_still_refused(client: AsyncClient, admin: str) -> None:
    """An organisation's outpost announces `tools` and nothing else (ADR 0226 §1): an enrolment or a poll of
    protocol 2 that announces `runs` there is refused, the contract taking the body and the door refusing
    it; and no offer is placed on it."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.services import outpost_runs

    daemon = await daemon_kit.approved(client)
    body = daemon.poll_body(protocol=2, capabilities=["runs"], runner=RUNNER)
    assert not daemon_kit.errors_against(load_outpost_door()["components"]["schemas"]["PollRequest"], body)
    problem = refused(await daemon.send(client, POLL, body), "invalid_request", 422)
    assert ("capabilities", "0") in {tuple(error["loc"]) for error in problem["errors"]}
    assert "organisation" in problem["detail"], problem
    token = await kit.token_for(client, SLUG)
    newcomer = Daemon()
    problem = refused(
        await newcomer.send(
            client, ENROL, newcomer.enrolment(token, protocol=2, capabilities=["runs"]), kid=None
        ),
        "invalid_request",
        422,
    )
    assert ("capabilities", "0") in {tuple(error["loc"]) for error in problem["errors"]}
    served = await daemon.poll(client, protocol=2, runs_running=["5d7a2c19-8e4b-4f31-b6a0-3c9e1f72d845"])
    assert served.status_code == 200 and "runs" not in served.json() and "cancel_runs" not in served.json()
    organisation_outpost = await kit.outpost_row(SLUG)
    async with session_scope(orgs="*") as session:
        with pytest.raises(outpost_runs.NotOffered):
            await outpost_runs.offer_run(
                session,
                organisation_outpost,
                offer_id="a-run-of-a-team-project",
                project_slug="billing-api",
                work_item_key="northwind/billing-api#1",
                api_url=runs_kit.API_URL,
                credential=RunCredential.SUBSCRIPTION,
                provider=SubscriptionProvider.ANTHROPIC,
                isolation="container",
                max_minutes=30,
            )


# ───────────────────────── the door served alone ─────────────────────────


def test_a_door_served_alone_holds_the_run_token_key_where_personal_runners_are_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The door mints the run tokens of personal runners: served alone in production without the key the
    API verifies them with, it would mint tokens no API accepts, so it does not start (`alone.create_app`
    asks `check_the_runs_lane`). Outside production, or with personal runners off, nothing is asked of it."""
    from tranibor_api.config import Settings, get_settings
    from tranibor_api.outposts import alone
    from tranibor_api.outposts.runs import check_the_runs_lane

    def settings(**values: Any) -> Settings:
        # The values alone, unvalidated: the suite's environment is a development one.
        return Settings.model_construct(**values)

    for refused_here in (settings(env="prod", personal_runners=True), settings(env="personal")):
        with pytest.raises(RuntimeError, match="TRANIBOR_RUN_TOKEN_PRIVATE_KEY"):
            check_the_runs_lane(refused_here)
    for served in (
        settings(env="prod", personal_runners=True, run_token_private_key="a PEM"),
        settings(env="prod", personal_runners=False),
        settings(env="dev", personal_runners=True),
    ):
        check_the_runs_lane(served)
    asked: list[Settings] = []

    def refuses(given: Settings) -> None:
        asked.append(given)
        raise RuntimeError("TRANIBOR_RUN_TOKEN_PRIVATE_KEY")

    monkeypatch.setattr(alone, "check_the_runs_lane", refuses)
    with pytest.raises(RuntimeError, match="TRANIBOR_RUN_TOKEN_PRIVATE_KEY"):
        alone.create_app()
    assert asked == [get_settings()]


# ───────────────────────── row-level security, on PostgreSQL ─────────────────────────


@pytest.fixture
async def pg_client(
    door: kit.Door, pg_app: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """The application on the migrated schema, as the application role; the organisation `northwind`."""
    from tranibor_api.config import reset_settings_cache
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope

    monkeypatch.setenv("TRANIBOR_OIDC_DEFAULT_ORG", kit.ORG)
    reset_settings_cache()
    async with session_scope(orgs="*") as session:
        session.add(Organization(slug=kit.ORG, name="Northwind"))
    async with AsyncClient(transport=ASGITransport(app=pg_app), base_url="http://test") as http:
        yield http


async def _rows_seen(*, orgs: Any, user: str | None, outpost: str, door: str | None = None) -> int:
    from tranibor_api.db.session import name_the_outpost, name_the_user, session_scope

    async with session_scope(orgs=orgs) as session:
        await name_the_user(session, user)
        await name_the_outpost(session, outpost)
        if door is not None:
            await session.execute(text("SELECT set_config('app.current_door', :door, true)"), {"door": door})
        return int((await session.execute(text("SELECT count(*) FROM outpost_runs"))).scalar_one())


@without_postgres
@pytest.mark.parametrize("served", ["by the API", "alone"])
async def test_on_postgres_the_door_claims_a_personal_runner_s_offer_under_row_level_security(
    pg_client: AsyncClient, served: str
) -> None:
    """The offers of Ada's runner exist for Ada, for the door that names her runner and marks its session,
    and under the platform's scope; for Bob, for a session that names no person and is not the door, for
    nobody else, and Bob writes none. The door, served by the API or alone, claims the offer through its
    marked sessions; the sweep closes the run of the lapsed lease under the platform's scope."""
    from tranibor_api.db.models import User
    from tranibor_api.db.session import OUTPOSTS_DOOR, name_the_outpost, name_the_user, session_scope
    from tranibor_api.outposts import rendezvous
    from tranibor_api.outposts.alone import create_app

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://door") as alone:
        door = pg_client if served == "by the API" else alone
        await login(pg_client, BOB)
        await login(pg_client, ADA)
        daemon = await runs_kit.runner_daemon(pg_client, door)
        run = await runs_kit.a_run_of()
        await runs_kit.offer(run)
        (element,) = (await runs_kit.handed(daemon, door))["runs"]
        token = runs_kit.opened(daemon, element)["run_token"]
        assert (await runs_kit.offer_row(run.id, run.owner_id)).instance_id == daemon.instance_id

        hers = (await runs_kit.runner_of(run.owner_id)).id
        async with session_scope(orgs="*") as session:
            bob = (await session.execute(select(User.id).where(User.email == BOB))).scalar_one()
        assert await _rows_seen(orgs=[kit.ORG], user=bob, outpost=hers) == 0, "another member"
        assert await _rows_seen(orgs=[kit.ORG], user=None, outpost=hers) == 0, "no person, not the door"
        assert await _rows_seen(orgs=[kit.ORG], user=None, outpost=hers, door="api") == 0, "another door"
        assert await _rows_seen(orgs=["elsewhere"], user=None, outpost=hers, door=OUTPOSTS_DOOR) == 0
        assert await _rows_seen(orgs=[kit.ORG], user=run.owner_id, outpost=hers) == 1, "its owner"
        assert await _rows_seen(orgs=[kit.ORG], user=None, outpost=hers, door=OUTPOSTS_DOOR) == 1, "the door"
        assert await _rows_seen(orgs="*", user=bob, outpost=hers) == 1, "the platform's scope"
        assert await _rows_seen(orgs=[kit.ORG], user=run.owner_id, outpost=None) == 0, "no outpost named"
        async with session_scope(orgs=[kit.ORG]) as session:
            await name_the_user(session, bob)
            await name_the_outpost(session, hers)
            moved = await session.execute(text("UPDATE outpost_runs SET state = 'offered'"))
            assert moved.rowcount == 0  # type: ignore[attr-defined]
        with pytest.raises(DBAPIError, match="row-level security"):
            async with session_scope(orgs=[kit.ORG]) as session:
                await name_the_user(session, bob)
                await name_the_outpost(session, hers)
                await session.execute(
                    text(
                        "INSERT INTO outpost_runs (id, org_id, outpost_id, kind, state, credential, "
                        "isolation, env, api_url, token_subject, project_slug, work_item_key, max_minutes, "
                        "lease_s, expires_at) SELECT 'intruder', org_id, id, 'stage_run', 'offered', "
                        "'gateway', "
                        "'container', '{}', 'http://x', 'intruder', 'p', 'w', 1, 30, now() FROM outposts "
                        "WHERE id = :outpost"
                    ),
                    {"outpost": hers},
                )

        await runs_kit.change_offer(
            run.id, run.owner_id, lease_until=datetime.now(UTC) - timedelta(seconds=1)
        )
        assert (await runs_kit.handed(daemon, door))["cancel_runs"] == [run.id]
        await rendezvous.sweep()
        assert (await runs_kit.run_row(run.id)).result["reason"] == "lost"
        assert _is_run_closed(await pg_client.get(f"{RUNS}/{run.id}/input", headers=_bearer(token)))
