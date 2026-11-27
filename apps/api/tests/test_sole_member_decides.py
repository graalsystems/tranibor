"""The sole member decides alone, always freshly signed in, and the decision says so (S87-11, ADR 0228 §3).

A personal installation holds one person (S87-10). The rules that ask for another person would leave that
person unable to decide anything their own agents propose: separation of duties (`_one_of_its_approvers`, the
release approval of `services/agent_releases.py`), an approver group and a human step addressed to a group
(groups an identity provider would carry, which an owner-ticket session does not), an exclusion of ADR 0196
§2, and an approver count above one. When the `installation` row says `personal` and exactly one person holds
a membership, that person meets them alone, never without a fresh authentication (the rule's
`step_up_minutes`, or 10 minutes when it names none, a step's answer included), and the decision entry and its
audit row carry `sole_member: true` with what the rule asked.

Every case runs twice: in `personal`, the owner signed in with the owner key on the loopback door (S87-12),
and in `prod`, the same one person signed in, where each case keeps today's answer and message. A personal
installation with two persons, which S87-10 refuses to write, is set up underneath the trigger and decides
nothing alone either.
"""

from __future__ import annotations

import pathlib
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from tranibor_core.owner_ticket import generate_owner_key_pair, sign_owner_ticket

from .conftest import login

ME = "admin@northwind.dev"
ORG = "/api/v1/orgs/northwind"
LOOPBACK = {"X-Choregos-Door": "loopback"}
APPROVERS = "netops-approvers"


@dataclass(frozen=True)
class Bench:
    """One person, signed in, in a project without a repository (its workflow is `default-request`)."""

    env: str
    client: AsyncClient
    me_id: str
    project_id: str
    org_id: str

    @property
    def personal(self) -> bool:
        return self.env == "personal"


async def _record(posture: str) -> None:
    """The `installation` row, as the API's first start writes it in that posture."""
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import record_posture

    async with session_scope(orgs="*") as session:
        assert await record_posture(session, posture, environment=posture) == posture


async def _the_one_person() -> str:
    """The installation's one person, an administrator of its organisation, as the bootstrap writes it."""
    from tranibor_api.db.models import Membership, User
    from tranibor_api.db.session import session_scope

    org_id = await _org_id()
    async with session_scope(orgs="*") as session:
        user = User(email=ME, display_name="me")
        session.add(user)
        await session.flush()
        session.add(Membership(user_id=user.id, org_id=org_id, role="org_admin"))
        return str(user.id)


async def _org_id() -> str:
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return str(
            (
                await session.execute(select(Organization.id).where(Organization.slug == "northwind"))
            ).scalar_one()
        )


async def _user_id(email: str) -> str:
    from tranibor_api.db.models import User
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return str((await session.execute(select(User.id).where(User.email == email))).scalar_one())


def _switch(patch: pytest.MonkeyPatch, env: str) -> None:
    """The application, switched to `env` once built: the settings are read per request."""
    from tranibor_api.config import reset_settings_cache

    patch.setenv("TRANIBOR_ENV", env)
    # The development sign-in is refused in production (S87-09): the settings could not be built with it.
    patch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    reset_settings_cache()


@pytest.fixture(params=["personal", "prod"])
async def bench(
    request: pytest.FixtureRequest, app: Any, client: AsyncClient, org: str, tmp_path: pathlib.Path
) -> AsyncIterator[Bench]:
    from tranibor_api.config import reset_settings_cache

    env = str(request.param)
    with pytest.MonkeyPatch.context() as patch:
        if env == "personal":
            me_id = await _the_one_person()
            await _record("personal")
            private, public = generate_owner_key_pair()
            (tmp_path / "owner.pub").write_bytes(public)
            patch.setenv("TRANIBOR_OWNER_PUBLIC_KEY_FILE", str(tmp_path / "owner.pub"))
            _switch(patch, "personal")
            client.headers.update(LOOPBACK)
            # The owner key, on the loopback door: a session whose authentication time is now (S87-12).
            signed_in = await client.post("/api/v1/auth/owner", json={"ticket": sign_owner_ticket(private)})
            assert signed_in.status_code == 204, signed_in.text
        else:
            await _record("organisation")
            await login(client, ME)  # in the suite's environment, before production refuses that sign-in
            me_id = await _user_id(ME)
            _switch(patch, "prod")
        created = await client.post(
            f"{ORG}/projects",
            json={"slug": "notes", "name": "Notes", "config": {"slug": "notes", "org": "northwind"}},
        )
        assert created.status_code == 201, created.text
        yield Bench(env, client, me_id, str(created.json()["id"]), await _org_id())
    reset_settings_cache()


@pytest.fixture
def stale() -> Iterator[None]:
    """The person's sign-in, eleven minutes old: older than a rule's default of ten."""
    from tranibor_api import freshness

    later = time.time() + 11 * 60
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(freshness.time, "time", lambda: later)
        yield


async def _my_agent(bench: Bench, slug: str = "helper") -> str:
    """An agent the person owns: what it proposes, its owner may not approve outside this posture."""
    from tranibor_api.db.models import Agent
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        agent = Agent(org_id=bench.org_id, slug=slug, display_name=slug, owner_id=bench.me_id)
        session.add(agent)
        await session.flush()
        return f"agent:{slug}"


async def _action(bench: Bench, *, proposed_by: str = "agent:nobody", **approval: Any) -> str:
    """A governed action waiting for approval (a row: nothing reaches Temporal before it is approved)."""
    return await _action_in(bench.org_id, bench.project_id, proposed_by=proposed_by, **approval)


async def _action_in(org_id: str, project_id: str, *, proposed_by: str, **approval: Any) -> str:
    from tranibor_api.db.models import Action
    from tranibor_api.db.session import session_scope

    rule = {"approvers": [{"role": "project_owner", "min": 1}], "separation_of_duties": True, **approval}
    async with session_scope(orgs="*") as session:
        row = Action(
            org_id=org_id,
            project_id=project_id,
            origin="tool",
            kind="net.firewall_rule",
            title="Open port 443",
            effects=[],
            proposed_by={"kind": "agent", "id": proposed_by},
            approval=rule,
            status="pending_approval",
        )
        session.add(row)
        await session.flush()
        return str(row.id)


async def _decide(bench: Bench, action_id: str, decision: str = "approve", **extra: Any) -> Any:
    return await bench.client.post(
        f"/api/v1/projects/{bench.project_id}/actions/{action_id}/decision",
        json={"decision": decision, **extra},
    )


async def _audit(action: str) -> list[dict[str, Any]]:
    from tranibor_api.db.models import AuditLog
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.action == action))).scalars().all()
        return [dict(row.payload or {}) for row in rows]


async def _action_row(action_id: str) -> Any:
    from tranibor_api.db.models import Action
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        row = await session.get(Action, action_id)
        assert row is not None
        return row


async def _pending(bench: Bench) -> list[dict[str, Any]]:
    response = await bench.client.get("/api/v1/me/pending")
    assert response.status_code == 200, response.text
    return list(response.json()["items"])


# ───────────────────────── a governed action: separation of duties ─────────────────────────


async def test_an_action_the_person_s_own_agent_proposed(bench: Bench) -> None:
    action_id = await _action(bench, proposed_by=await _my_agent(bench))
    listed = {item["id"] for item in await _pending(bench) if item["kind"] == "governed_action"}
    decided = await _decide(bench, action_id)
    if not bench.personal:
        assert action_id not in listed, "not theirs to decide: their agent proposed it"
        assert decided.status_code == 422, decided.text
        assert "separation of duties" in decided.text
        assert (await _action_row(action_id)).status == "pending_approval"
        assert await _audit("action.decide") == []
        return
    assert action_id in listed, "the sole member's own agent's proposal waits for them"
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "approved"
    (entry,) = body["decisions"]
    assert entry["by"] == ME and entry["sole_member"] is True
    assert entry["rule_asked"] == ["separation_of_duties"]
    assert isinstance(entry["auth_age_seconds"], int) and entry["auth_age_seconds"] < 600
    (audited,) = await _audit("action.decide")
    assert audited["sole_member"] is True and audited["rule_asked"] == ["separation_of_duties"]


@pytest.mark.parametrize("step_up_minutes", [10, None], ids=["the-rule-s-minutes", "a-rule-naming-none"])
async def test_a_stale_sign_in_decides_nothing_alone(
    bench: Bench, stale: None, step_up_minutes: int | None
) -> None:
    """A rule naming no `step_up_minutes` still asks 10 minutes of the sole member: freshness is what stands
    in for the second person."""
    action_id = await _action(bench, proposed_by=await _my_agent(bench), step_up_minutes=step_up_minutes)
    refused = await _decide(bench, action_id)
    if bench.personal:
        assert refused.status_code == 401, refused.text
        assert refused.json()["errors"][0]["error"] == "step_up_required"
    else:
        assert refused.status_code == 422 and "separation of duties" in refused.text, refused.text
    row = await _action_row(action_id)
    assert (row.status, row.decisions) == ("pending_approval", [])
    rejected = await _decide(bench, action_id, "reject", reason="not now")
    assert rejected.status_code == (401 if bench.personal else 422), rejected.text


async def _in_doubt(bench: Bench) -> str:
    """An approved action whose one write was sent and never answered (ADR 0149 §5), as its workflow closes
    it, proposed by the person's own agent under a rule that names no `step_up_minutes`."""
    from tranibor_api.db.models import Action, ActionEffect
    from tranibor_api.db.session import session_scope

    action_id = await _action(bench, proposed_by=await _my_agent(bench), step_up_minutes=None)
    async with session_scope(orgs="*") as session:
        row = await session.get(Action, action_id)
        assert row is not None
        row.status = "in_doubt"
        session.add(
            ActionEffect(
                org_id=bench.org_id,
                action_id=action_id,
                position=0,
                key=f"{action_id}:0",
                effect="connector.call",
                status="in_doubt",
                result={"in_doubt": [{"call_id": "c-1"}]},
            )
        )
    return action_id


@pytest.mark.parametrize("fresh", [True, False], ids=["fresh", "stale"])
async def test_an_action_in_doubt_is_settled_alone_freshly(bench: Bench, fresh: bool) -> None:
    """Settling is guarded as deciding is (S65-26): the same waivers, the same freshness, the same mark."""
    from tranibor_api import freshness

    action_id = await _in_doubt(bench)
    later = time.time() + (0 if fresh else 11 * 60)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(freshness.time, "time", lambda: later)
        settled = await bench.client.post(
            f"/api/v1/projects/{bench.project_id}/actions/{action_id}/settlement",
            json={"outcome": "not_made", "note": "nothing in the firewall's log"},
        )
    if not bench.personal:
        assert settled.status_code == 422 and "separation of duties" in settled.text, settled.text
        return
    if not fresh:
        assert settled.status_code == 401, settled.text
        assert settled.json()["errors"][0]["error"] == "step_up_required"
        assert (await _action_row(action_id)).status == "in_doubt"
        return
    assert settled.status_code == 200, settled.text
    settlement = settled.json()["settlement"]
    assert (settlement["sole_member"], settlement["rule_asked"]) == (True, ["separation_of_duties"])
    (audited,) = await _audit("action.settle")
    assert (audited["sole_member"], audited["rule_asked"]) == (True, ["separation_of_duties"])


# ───────────────────────── a group, an exclusion, a count ─────────────────────────

RULES: dict[str, tuple[dict[str, Any], int, str, list[str]]] = {
    "an-approver-group": (
        {"approvers": [{"role": "project_owner", "group": APPROVERS, "min": 1}]},
        403,
        f"needs membership of the group {APPROVERS}",
        [f"group:{APPROVERS}"],
    ),
    "an-exclusion": (
        {"excluded": [ME.upper()]},
        403,
        "it names you among those who may not decide it",
        ["excluded"],
    ),
    "two-approvers": (
        {"approvers": [{"role": "project_owner", "min": 2}]},
        200,
        "pending_approval",
        ["approvers:2"],
    ),
}


@pytest.mark.parametrize("case", sorted(RULES))
async def test_what_a_rule_asks_of_other_people(bench: Bench, case: str) -> None:
    approval, prod_status, prod_says, asked = RULES[case]
    action_id = await _action(bench, **approval)
    decided = await _decide(bench, action_id)
    if not bench.personal:
        assert decided.status_code == prod_status, decided.text
        assert prod_says in decided.text
        decisions = (await _action_row(action_id)).decisions
        assert all("sole_member" not in entry for entry in decisions)
        return
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "approved"
    (entry,) = body["decisions"]
    assert (entry["sole_member"], entry["rule_asked"]) == (True, asked)
    (audited,) = await _audit("action.decide")
    assert (audited["sole_member"], audited["rule_asked"]) == (True, asked)


async def test_a_decision_nobody_waived_is_not_marked(bench: Bench) -> None:
    """The mark says what the person met alone: an action no rule asked another person for carries none."""
    action_id = await _action(bench)
    decided = await _decide(bench, action_id)
    assert decided.status_code == 200, decided.text
    (entry,) = decided.json()["decisions"]
    assert "sole_member" not in entry and "rule_asked" not in entry
    (audited,) = await _audit("action.decide")
    assert "sole_member" not in audited


# ───────────────────────── a human step addressed to a group ─────────────────────────


async def _waiting_brief(bench: Bench) -> tuple[str, str]:
    """A work item of `default-request` waiting at `t-approve-brief`, addressed to `product-owners`."""
    from tranibor_api.db.models import HumanRequest
    from tranibor_api.db.session import session_scope
    from tranibor_core import utcnow

    created = await bench.client.post(
        f"/api/v1/projects/{bench.project_id}/work-items", json={"title": "A brief to approve"}
    )
    assert created.status_code == 201, created.text
    item_id = str(created.json()["id"])
    async with session_scope(orgs="*") as session:
        row = HumanRequest(
            work_item_id=item_id,
            project_id=bench.project_id,
            transition_id="t-approve-brief",
            kind="approval",
            payload={"summary": "Approve the brief?"},
            requested_at=utcnow(),
        )
        session.add(row)
        await session.flush()
        request_id = str(row.id)
    shown = await bench.client.get(f"/api/v1/work-items/{item_id}")
    assert shown.status_code == 200, shown.text
    return item_id, request_id


async def _request_row(request_id: str) -> Any:
    from tranibor_api.db.models import HumanRequest
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        row = await session.get(HumanRequest, request_id)
        assert row is not None
        return row


async def test_a_group_addressed_step_of_default_request_is_listed_and_answered(bench: Bench) -> None:
    item_id, request_id = await _waiting_brief(bench)
    listed = {item["id"] for item in await _pending(bench) if item["kind"] == "human_step"}
    answered = await bench.client.post(f"/api/v1/work-items/{item_id}/decisions", json={"kind": "approve"})
    assert answered.status_code == 202, answered.text
    row = await _request_row(request_id)
    if not bench.personal:
        # Outside the group: not listed; an organisation administrator answers by today's audited override.
        assert request_id not in listed
        assert "sole_member" not in (row.decision or {})
        (override,) = await _audit("workitem.decision.group_override")
        assert override["group"] == "product-owners"
        assert all("sole_member" not in payload for payload in await _audit("workitem.decision"))
        return
    assert request_id in listed, "the product owners' step waits for the sole member"
    assert row.decision["sole_member"] is True
    assert row.decision["rule_asked"] == ["group:product-owners"]
    assert row.decision["decided_by"] == ME
    (audited,) = await _audit("workitem.decision")
    assert (audited["sole_member"], audited["rule_asked"]) == (True, ["group:product-owners"])
    assert await _audit("workitem.decision.group_override") == [], (
        "answered as the sole member, not overridden"
    )


async def test_a_step_answered_alone_needs_a_fresh_sign_in(bench: Bench, stale: None) -> None:
    item_id, request_id = await _waiting_brief(bench)
    answered = await bench.client.post(f"/api/v1/work-items/{item_id}/decisions", json={"kind": "approve"})
    if bench.personal:
        assert answered.status_code == 401, answered.text
        assert answered.json()["errors"][0]["error"] == "step_up_required"
        assert (await _request_row(request_id)).decided_at is None
    else:
        assert answered.status_code == 202, answered.text  # the override asks nothing more, as today


# ───────────────────────── an agent release ─────────────────────────

APPROVAL_POLICY = {"on_publish": "candidate", "stable": {"approvals": 1}}


async def _waiting_release(bench: Bench, policy: dict[str, Any]) -> str:
    """The person's agent at v1 in production, its v2 asked for by the person and waiting for approval."""
    client = bench.client
    created = await client.post(
        f"{ORG}/agents",
        json={"slug": "reviewer", "display_name": "reviewer", "spec": {"instructions": "Review, v1."},
              "release_policy": policy},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    saved = await client.post(f"{ORG}/agents/reviewer/versions", json={"instructions": "Review, v2."})
    assert saved.status_code == 201, saved.text
    asked = await client.post(f"{ORG}/agents/reviewer/releases", json={"channel": "stable", "version": 2})
    assert asked.status_code == 201, asked.text
    assert asked.json()["status"] == "pending_approval"
    return str(asked.json()["id"])


@pytest.mark.parametrize("approvals", [1, 2])
async def test_a_release_the_person_asked_for(bench: Bench, approvals: int) -> None:
    release_id = await _waiting_release(
        bench, {"on_publish": "candidate", "stable": {"approvals": approvals}}
    )
    decided = await bench.client.post(
        f"{ORG}/agents/reviewer/releases/{release_id}/decision", json={"decision": "approve"}
    )
    if not bench.personal:
        assert decided.status_code == 422, decided.text
        assert "separation of duties" in decided.text
        assert await _audit("agent.release.decide") == []
        return
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["status"] == "done"
    asked = ["separation_of_duties", *([f"approvals:{approvals}"] if approvals > 1 else [])]
    (vote,) = body["decisions"]
    assert (vote["by"], vote["sole_member"], vote["rule_asked"]) == (ME, True, asked)
    (audited,) = await _audit("agent.release.decide")
    assert (audited["sole_member"], audited["rule_asked"]) == (True, asked)
    agent = (await bench.client.get(f"{ORG}/agents/reviewer")).json()
    assert agent["stable_version"] == 2


async def test_a_release_approved_alone_needs_a_fresh_sign_in(bench: Bench) -> None:
    from tranibor_api import freshness

    release_id = await _waiting_release(bench, APPROVAL_POLICY)
    later = time.time() + 11 * 60
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(freshness.time, "time", lambda: later)
        refused = await bench.client.post(
            f"{ORG}/agents/reviewer/releases/{release_id}/decision", json={"decision": "approve"}
        )
    assert refused.status_code == 401, refused.text
    assert refused.json()["errors"][0]["error"] == "step_up_required"
    agent = (await bench.client.get(f"{ORG}/agents/reviewer")).json()
    assert agent["stable_version"] == 1 and agent["pending_release"]["decisions"] == []


# ───────────────────────── who is the sole member ─────────────────────────


async def test_two_persons_in_the_personal_posture_decide_nothing_alone(app: Any, org: str) -> None:
    """S87-10's trigger refuses a second person once the posture is recorded; a database holding two before
    the record is what is left to try. Neither decides alone, and the refusal is today's."""
    from tranibor_api.db.models import Agent, Membership, Organization, Project, User
    from tranibor_api.db.session import session_scope
    from tranibor_api.errors import ApiError
    from tranibor_api.personal_installation import decides_alone, sole_member
    from tranibor_api.rbac import Principal, Role
    from tranibor_api.services import actions

    async with session_scope(orgs="*") as session:
        org_id = (await session.execute(select(Organization.id).where(Organization.slug == org))).scalar_one()
        # The other person in another organisation: a count read in this one's scope would see one person.
        elsewhere = Organization(slug="elsewhere", name="Elsewhere")
        people = [User(email=ME, display_name="me"), User(email="other@elsewhere.dev", display_name="other")]
        session.add_all([elsewhere, *people])
        await session.flush()
        session.add(Membership(user_id=people[0].id, org_id=org_id, role="org_admin"))
        session.add(Membership(user_id=people[1].id, org_id=elsewhere.id, role="org_admin"))
        project = Project(org_id=org_id, slug="notes", name="Notes", config={"slug": "notes", "org": org})
        session.add(project)
        session.add(Agent(org_id=org_id, slug="helper", display_name="helper", owner_id=people[0].id))
        await session.flush()
        me_id, project_id = str(people[0].id), str(project.id)
    await _record("personal")
    action_id = await _action_in(org_id, project_id, proposed_by="agent:helper")
    principal = Principal(
        user_id=me_id,
        email=ME,
        org_roles={org: Role.ORG_ADMIN},
        authenticated_at=int(time.time()),
        credential="cookie",
    )
    # A request's session sees its organisations only (row-level security, on PostgreSQL); the persons are
    # counted across the installation all the same.
    async with session_scope(org) as session:
        assert await sole_member(session, org_id) is False
        assert await decides_alone(session, org_id, principal) is False
        project_row = await session.get(Project, project_id)
        assert project_row is not None
        with pytest.raises(ApiError) as refused:
            await actions.decide(session, project_row, org, action_id, "approve", None, principal)
    assert refused.value.status_code == 422 and "separation of duties" in str(refused.value.detail)


async def test_a_machine_account_is_not_a_second_person_and_never_decides(app: Any, org: str) -> None:
    from tranibor_api.db.models import Membership, Organization, User
    from tranibor_api.db.session import session_scope
    from tranibor_api.personal_installation import decides_alone, sole_member
    from tranibor_api.rbac import Principal, Role

    me_id = await _the_one_person()
    async with session_scope(orgs="*") as session:
        org_id = (await session.execute(select(Organization.id).where(Organization.slug == org))).scalar_one()
        machine = User(email="ci@machines.local", display_name="ci", is_service=True)
        session.add(machine)
        await session.flush()
        session.add(Membership(user_id=machine.id, org_id=org_id, role="contributor"))
        machine_id = str(machine.id)
    async with session_scope(orgs="*") as session:
        assert await sole_member(session, org_id) is False, "no record yet: not the personal posture"
    await _record("personal")
    now = int(time.time())
    person = Principal(user_id=me_id, email=ME, org_roles={org: Role.ORG_ADMIN}, authenticated_at=now,
                       credential="cookie")  # fmt: skip
    by_token = Principal(user_id=me_id, email=ME, org_roles={org: Role.ORG_ADMIN}, credential="api_token")
    the_machine = Principal(user_id=machine_id, email="ci@machines.local", authenticated_at=now,
                            credential="cookie")  # fmt: skip
    async with session_scope(org) as session:
        assert await sole_member(session, org_id) is True
        assert await decides_alone(session, org_id, person) is True
        assert await decides_alone(session, org_id, by_token) is False, "a token never decides (ADR 0029 §5)"
        assert await decides_alone(session, org_id, the_machine) is False
        assert await sole_member(session, "another-organisation") is False
