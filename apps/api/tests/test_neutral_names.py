"""Round C1 of ADR 0181 on the API, its window closed on 2026-11-23: only the neutral names are served.

A request naming a former role (`developer`, `release_captain`) is refused; a row is written with the
neutral spelling; the role groups `developers` and `release-captains` give no role any more; a response
carries `links`, `workspace`, `workspace_fixed`, `needs_workspace`, `writes_workspace`,
`in_change_request` and `first_pass_acceptance_rate`, and none of their former names. A stored workflow
version still reads a former approver role (`tranibor_contracts.recorded`).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from tranibor_contracts import Role

FORMER = ("pr_url", "repository", "repository_fixed")


async def _role_row(email: str) -> str:
    from sqlalchemy import select
    from tranibor_api.db.models import Membership, User
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        user = (await session.execute(select(User).where(User.email == email))).scalar_one()
        rows = (await session.execute(select(Membership.role).where(Membership.user_id == user.id))).scalars()
        return next(iter(rows))


def test_the_roles_are_the_five_neutral_ones_with_their_permissions_and_rank() -> None:
    from tranibor_api.rbac import ROLE_PERMISSIONS, Permission, rank_of

    assert [role.value for role in Role] == [
        "org_admin",
        "project_owner",
        "contributor",
        "approver",
        "viewer",
    ]
    assert set(ROLE_PERMISSIONS) == set(Role)
    assert Permission.TRAIN_APPROVE in ROLE_PERMISSIONS[Role.APPROVER]
    assert Permission.TRAIN_APPROVE not in ROLE_PERMISSIONS[Role.CONTRIBUTOR]
    ranks = [rank_of(role) for role in ("viewer", "contributor", "approver", "project_owner", "org_admin")]
    assert ranks == sorted(ranks) and len(set(ranks)) == 5
    assert rank_of("developer") == rank_of("release_captain") == rank_of(None) == -1


@pytest.mark.parametrize("sent", ["contributor", "approver"])
async def test_a_member_given_a_neutral_role_is_stored_under_it(
    client: AsyncClient, project: dict[str, Any], sent: str
) -> None:
    email = f"{sent}@northwind.dev"
    added = await client.post("/api/v1/orgs/northwind/members", json={"email": email, "role": sent})
    assert added.status_code in (200, 201), added.text
    assert added.json()["role"] == sent
    assert await _role_row(email) == sent


@pytest.mark.parametrize("former", ["developer", "release_captain"])
async def test_a_former_role_is_refused(client: AsyncClient, project: dict[str, Any], former: str) -> None:
    refused = await client.post(
        "/api/v1/orgs/northwind/members", json={"email": f"{former}@northwind.dev", "role": former}
    )
    assert refused.status_code == 422, refused.text


def test_the_former_role_groups_give_no_role() -> None:
    from tranibor_api.routers.auth import GROUP_ROLES, roles_from_groups

    assert roles_from_groups(["choregos:acme:contributors"], "") == {"acme": Role.CONTRIBUTOR}
    assert roles_from_groups(["choregos:acme:approvers"], "") == {"acme": Role.APPROVER}
    assert roles_from_groups(["choregos:acme:developers", "choregos:acme:release-captains"], "") == {}
    assert "developers" not in GROUP_ROLES and "release-captains" not in GROUP_ROLES


async def test_a_work_item_carries_only_the_neutral_names(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.db.models import WorkItem
    from tranibor_api.db.session import session_scope

    pid = project["id"]
    await client.put(f"/api/v1/projects/{pid}/connectors/tracker", json={"type": "internal", "config": {}})
    created = await client.post(f"/api/v1/projects/{pid}/work-items", json={"title": "T", "start": False})
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["workspace"] == "billing-api" and item["workspace_fixed"] is False and item["links"] == []
    assert not set(FORMER) & set(item)
    async with session_scope(orgs="*") as session:
        row = await session.get(WorkItem, item["id"])
        assert row is not None
        row.pr_url = "https://github.com/northwind/billing-api/pull/7"
    again = (await client.get(f"/api/v1/work-items/{item['id']}")).json()
    assert again["links"] == [
        {"rel": "change_request", "url": "https://github.com/northwind/billing-api/pull/7"}
    ]
    assert not set(FORMER) & set(again)


def test_the_dtos_have_only_the_neutral_rate() -> None:
    from tranibor_api.schemas import MemoryAbGroup
    from tranibor_api.schemas.projects import ProjectStats

    assert "first_pass_merge_rate" not in ProjectStats(first_pass_acceptance_rate=0.5).model_dump()
    assert "first_pass_merge_rate" not in MemoryAbGroup(first_pass_acceptance_rate=0.1).model_dump()


async def test_the_catalogue_carries_only_the_neutral_name(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kinds that need a workspace are declared by a plugin: here the agents' runtime stands in for one
    (`plugins/software-delivery/tests/api/test_delivery_neutral_names.py` checks the plugin's)."""
    from tranibor_api.services import workflow_catalogue

    monkeypatch.setattr(workflow_catalogue, "workspace_kinds", lambda: frozenset({"runtime"}))
    entries = (await client.get("/api/v1/orgs/northwind/workflow-catalogue?limit=200")).json()["items"]
    assert entries, "the catalogue lists the shipped workflows"
    assert not any("needs_repository" in e for e in entries)
    assert any(e["needs_workspace"] for e in entries) and not all(e["needs_workspace"] for e in entries)


WRITES = """\
apiVersion: choregos/v1
kind: Workflow
metadata: { name: writes, version: 1 }
actors:
  dev: { type: agent, role: implement, model: "profile:standard" }
  owner: { type: human, group: product-owners, sla_hours: 24 }
states:
  inbox: { display: To do, kind: wait }
  written: { display: Written }
  done: { display: Done, terminal: true }
transitions:
  - { id: t-write, from: inbox, to: written, by: dev, outputs: [report], gates: [GATE] }
  - { id: t-accept, from: written, to: done, by: owner, inputs: [report] }
"""


def test_the_core_views_carry_only_the_neutral_names() -> None:
    """A step writes the workspace when it carries a gate that reads it, which a plugin registers: here a
    test plugin registers one, and forgets it after."""
    import yaml
    from tranibor_contracts import Workflow
    from tranibor_core.dsl.flow import artifact_flow
    from tranibor_core.dsl.process import to_process
    from tranibor_core.gates import GateOutcome, forget, gate

    gate("probe_workspace", plugin="test-neutral-names", workspace=True)(
        lambda context: GateOutcome("probe_workspace", passed=True)
    )
    try:
        workflow = Workflow.model_validate(yaml.safe_load(WRITES.replace("GATE", "probe_workspace")))
        steps = to_process(workflow)
    finally:
        forget("test-neutral-names")
    assert any(s["writes_workspace"] for s in steps) and not all(s["writes_workspace"] for s in steps)
    assert not any("writes_branch" in s for s in steps)
    flows = artifact_flow(workflow)
    assert flows and not any("in_pull_request" in a for a in flows)


def test_a_stored_workflow_reads_a_former_approver_role_and_a_new_one_is_refused() -> None:
    from pydantic import ValidationError
    from tranibor_contracts.workflow import ActionApprover
    from tranibor_core.dsl.parser import parse_workflow

    assert (
        ActionApprover.model_validate({"role": "developer"}, context={"stored": True}).role == "contributor"
    )
    assert (
        ActionApprover.model_validate({"role": "release_captain"}, context={"stored": True}).role
        == "approver"
    )
    with pytest.raises(ValidationError):
        ActionApprover.model_validate({"role": "developer"})
    # The flag reaches the parser the API reads a stored version's YAML with.
    assert parse_workflow.__kwdefaults__ == {"strict": True, "stored": False}


async def test_an_approval_rule_ranks_only_the_neutral_roles() -> None:
    from types import SimpleNamespace

    from tranibor_api.services.actions import _rank_asked, rank

    assert _rank_asked({"role": "contributor"}) < _rank_asked({"role": "approver"})
    assert _rank_asked({"role": "nonsense"}) == _rank_asked({}) == _rank_asked({"role": "project_owner"})
    principal = SimpleNamespace(role_for=lambda org, project: Role.APPROVER, org_roles={"acme": Role.VIEWER})
    assert rank(principal, "acme", "p") == _rank_asked({"role": "approver"})
