"""The board reads a page of work items in a number of statements that does not grow with the page, and a
work item reads the same alone and in a page (#2285).

`GET /projects/{id}/work-items` built each entry with `work_item_dto` and `readable`, one work item at a
time: its workflow, its current run, its waiting request, its ledger, its repository, its estimate and its
mark each cost a query per work item (84 statements for 21 work items, about 200 for a page of 50), which
made the board the slowest route under load. The page now reads each kind once for all its work items.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import event

from .conftest import login
from .test_knowledge_rights import ORG, _as, _uid

READER, STRANGER = "reader@northwind.dev", "stranger@northwind.dev"
MEMBER = {"projects": {"billing-api": "contributor"}}

STAFFING = """apiVersion: choregos/v1
kind: Workflow
metadata: {name: staffing, version: 1}
actors:
  hr: {type: human, group: hr}
states:
  request: {display: Request, kind: wait}
  filled: {display: Filled, terminal: true}
transitions:
  - {id: t-fill, from: request, to: filled, by: hr}
"""

#: The shapes a page mixes, so that every per-item fact is read: a pinned workflow with runs and waiting
#: requests, a restricted mark with ledger lines, an external tracker's work item fixed to its repository,
#: a closed work item (a peer of the estimates).
SHAPES = ("pinned", "marked", "external", "closed")


async def _board(client: AsyncClient, project: dict[str, Any]) -> str:
    """`staffing` published then replaced by `blank` as the default; `hr` restricted, granted to READER.
    Gives the id of the `staffing` definition the pinned work items keep."""
    from sqlalchemy import select
    from tranibor_api.db.models import WorkflowDef
    from tranibor_api.db.session import session_scope
    from tranibor_core import template_yaml

    pid = project["id"]
    assert (await client.put(f"/api/v1/projects/{pid}/workflow", json={"yaml": STAFFING})).status_code == 200
    blank = await client.put(f"/api/v1/projects/{pid}/workflow", json={"yaml": template_yaml("blank")})
    assert blank.status_code == 200, blank.text
    created = await client.post(ORG, json={"slug": "hr", "name": "HR", "restricted": True})
    assert created.status_code == 201, created.text
    reader = await _uid(client, READER, **MEMBER)
    await _uid(client, STRANGER, **MEMBER)
    await login(client, "admin@northwind.dev")
    granted = await client.put(
        f"{ORG}/hr/grants", json={"grants": [{"subject_type": "user", "subject": reader, "role": "viewer"}]}
    )
    assert granted.status_code == 200, granted.text
    async with session_scope() as session:
        staffing = (
            await session.execute(select(WorkflowDef).where(WorkflowDef.name == "staffing"))
        ).scalar_one()
        return str(staffing.id)


async def _seed(project: dict[str, Any], staffing: str, prefix: str, count: int) -> None:
    """`count` work items titled `<prefix> <n>`, cycling over `SHAPES`."""
    from tranibor_api.db.models import CostLedger, HumanRequest, Run, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_core import utcnow
    from tranibor_core.repositories import FIXED_KEY

    pid = project["id"]
    now = utcnow()
    async with session_scope() as session:
        for n in range(count):
            shape = SHAPES[n % len(SHAPES)]
            item = WorkItem(
                project_id=pid,
                tracker_key=f"{prefix.upper()}-{n}",
                title=f"{prefix} {n}",
                state="ready",
                size="S" if shape != "marked" else "M",
                totals={"cost_usd": 1.0 + n},
                created_at=now - timedelta(minutes=count - n),
            )
            if shape == "pinned":
                item.workflow_def_id = staffing
                item.state = "request"
            elif shape == "marked":
                item.restricted_knowledge = ["hr"]
                item.reporter = "Jean Martin <jmartin@northwind.dev>"
            elif shape == "external":
                item.tracker_key = f"GH-{prefix}-{n}"
                item.url = f"https://github.com/northwind/billing-api/issues/{n}"
                item.pr_url = f"https://github.com/northwind/billing-api/pull/{n}"
                item.documents = {FIXED_KEY: "billing-api"}
                item.budget_usd = 25.0
            else:
                item.state = "done"
                item.closed_at = now - timedelta(days=1)
            session.add(item)
            await session.flush()
            if shape == "pinned":
                # The current run is the latest queued or running one: a finished run never is.
                for minutes, status in ((30, "queued"), (20, "running"), (10, "succeeded")):
                    session.add(
                        Run(
                            id=f"run-{prefix}-{n}-{status}",
                            work_item_id=item.id,
                            project_id=pid,
                            stage_role="implement",
                            status=status,
                            created_at=now - timedelta(minutes=minutes),
                        )
                    )
                # The waiting request is the latest undecided one: a decided request never is.
                for minutes, question, decided in (
                    (30, "older", False),
                    (20, "newer", False),
                    (10, "done", True),
                ):
                    session.add(
                        HumanRequest(
                            work_item_id=item.id,
                            project_id=pid,
                            transition_id="t-fill",
                            kind="approval",
                            payload={"question": f"{question} {n}"},
                            requested_at=now - timedelta(minutes=minutes),
                            decided_at=now if decided else None,
                        )
                    )
            if shape == "marked":
                for usd in (0.25, 0.5):
                    session.add(
                        CostLedger(
                            project_id=pid,
                            work_item_id=item.id,
                            kind="model",
                            cost_usd=usd,
                            cost_eur=usd,
                            ts=now,
                        )
                    )


async def _statements(client: AsyncClient, url: str) -> tuple[int, list[dict[str, Any]]]:
    from tranibor_api.db.session import get_engine

    seen: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        listed = await client.get(url)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert listed.status_code == 200, listed.text
    return len(seen), list(listed.json()["items"])


async def test_the_board_reads_a_page_in_statements_that_do_not_grow_with_it(
    client: AsyncClient, admin: str, project: dict[str, Any]
) -> None:
    staffing = await _board(client, project)
    await _seed(project, staffing, "small", 4)
    await _seed(project, staffing, "large", 40)
    url = f"/api/v1/projects/{project['id']}/work-items"
    for who in (None, STRANGER):  # an administrator reads every mark; a member's are decided on the bases
        if who is None:
            small, few = await _statements(client, f"{url}?q=small")
            large, many = await _statements(client, f"{url}?q=large")
        else:
            async with _as(client, who, **MEMBER) as member:
                small, few = await _statements(member, f"{url}?q=small")
                large, many = await _statements(member, f"{url}?q=large")
        assert (len(few), len(many)) == (4, 40)
        assert large == small, (
            f"a page of 40 work items read {large} statements and a page of 4 read {small}: "
            "something is still read once per work item (#2285)"
        )


async def test_a_work_item_reads_the_same_alone_and_in_a_page(
    client: AsyncClient, admin: str, project: dict[str, Any]
) -> None:
    staffing = await _board(client, project)
    await _seed(project, staffing, "item", 12)
    url = f"/api/v1/projects/{project['id']}/work-items?limit=50"
    page = await _alone_and_in_a_page(client, url)
    async with _as(client, READER, **MEMBER) as reader:
        assert await _alone_and_in_a_page(reader, url) == page, (
            "a reader of the mark reads what an admin reads"
        )
    by_title = {entry["title"]: entry for entry in page}
    pinned, marked, external, closed = (by_title[f"item {n}"] for n in range(4))
    assert (pinned["workflow_name"], pinned["state_display"]) == ("staffing", "Request")
    assert pinned["current_run"]["id"] == "run-item-0-running", "the latest queued or running run"
    assert pinned["pending_request"]["payload"]["question"] == "newer 0", "the latest undecided request"
    assert pinned["pending_request"]["assignee"] is not None, "the pinned workflow's actor names it"
    assert marked["workflow_name"] == "blank" and marked["current_run"] is None
    assert marked["pending_request"] is None and marked["redacted"] is False
    assert marked["totals"]["cost_usd"] == 0.75, "the ledger's sum replaces the counter when it has lines"
    assert external["totals"]["cost_usd"] == 3.0, "without a ledger line the counter stays"
    assert (external["workspace"], external["workspace_fixed"]) == ("billing-api", True)
    assert [link["url"] for link in external["links"]] == ["https://github.com/northwind/billing-api/pull/2"]
    assert closed["estimate"] is not None and marked["estimate"] is None, "peers of size S only"
    async with _as(client, STRANGER, **MEMBER) as stranger:
        hidden = {e["title"]: e for e in (await stranger.get(url)).json()["items"]}["item 1"]
    assert hidden["redacted"] is True and hidden["reporter"] is None


async def _alone_and_in_a_page(client: AsyncClient, url: str) -> list[dict[str, Any]]:
    """The page, after checking that each of its entries is what the work item's own route serves."""
    page = list((await client.get(url)).json()["items"])
    assert len(page) == 12
    for entry in page:
        alone = await client.get(f"/api/v1/work-items/{entry['id']}")
        assert alone.status_code == 200, alone.text
        assert alone.json() == entry, entry["title"]
    return page


async def _findings(project: dict[str, Any], start: int, stop: int) -> None:
    """Findings `start..stop`, each with its own origin (one in three marked `hr`) and created work item."""
    from tranibor_api.db.models import Finding, WorkItem
    from tranibor_api.db.session import session_scope

    async with session_scope() as session:
        for n in range(start, stop):
            origin = WorkItem(
                project_id=project["id"],
                tracker_key=f"ORIGIN-{n}",
                title=f"origin {n}",
                state="ready",
                restricted_knowledge=["hr"] if n % 3 == 0 else None,
            )
            created = WorkItem(
                project_id=project["id"], tracker_key=f"FIX-{n}", title=f"fix {n}", state="ready"
            )
            session.add_all([origin, created])
            await session.flush()
            session.add(
                Finding(
                    project_id=project["id"],
                    origin_work_item_id=origin.id,
                    created_work_item_id=created.id,
                    title=f"finding {n}",
                    type="docs",
                    severity="low",
                    evidence="seen",
                    status="ticket_created",
                )
            )


async def test_the_findings_page_reads_in_statements_that_do_not_grow_with_it(
    client: AsyncClient, admin: str, project: dict[str, Any]
) -> None:
    await _board(client, project)
    url = f"/api/v1/projects/{project['id']}/findings?limit=50"
    counts = []
    for start, stop in ((0, 3), (3, 30)):
        await _findings(project, start, stop)
        async with _as(client, STRANGER, **MEMBER) as stranger:
            statements, items = await _statements(stranger, url)
        assert len(items) == stop - (stop + 2) // 3, "a finding of a marked work item is left out"
        assert all(item["created_work_item_key"] == f"FIX-{item['title'].split()[1]}" for item in items)
        counts.append(statements)
    assert counts[1] == counts[0], (
        f"30 findings read {counts[1]} statements and 3 read {counts[0]}: something is still read once per "
        "finding or per marked origin (#2285)"
    )
