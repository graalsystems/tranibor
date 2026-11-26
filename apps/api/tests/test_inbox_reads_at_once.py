"""The inbox reads its waiting requests in a number of statements that does not grow with them, and lists
exactly the requests the decision route would accept (the load test's slowest route after #2285).

`GET /me/pending` asked, for each waiting request, who it is addressed to (the work item's workflow, read
and parsed again, and the project's slug) and whether the caller reads the work item's restricted mark:
a few statements and a workflow parse per request. Under load it became the slowest hot route
(`docs/load-testing.md`, the rerun on `main`). The requests are now filtered a slice at a time.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import event

from .test_knowledge_rights import ORG, _as, _uid

PENDING = "/api/v1/me/pending"
MEMBER_EMAIL, ELSEWHERE = "member@northwind.dev", "elsewhere@northwind.dev"
MEMBER = {"projects": {"billing-api": "contributor"}}

INBOX = f"""apiVersion: tranibor/v1
kind: Workflow
metadata: {{name: inbox, version: 1}}
actors:
  approver: {{type: human, user: {MEMBER_EMAIL}}}
states:
  request: {{display: Request, kind: wait}}
  done: {{display: Done, terminal: true}}
transitions:
  - {{id: t-approve, from: request, to: done, by: approver}}
"""

#: What each waiting request is, cycling: addressed by the workflow to the member; reassigned to the member
#: by an administrator; on a work item whose restricted mark the member does not read; reassigned to someone
#: else. The member may answer the first two only.
SHAPES = ("workflow", "reassigned", "marked", "elsewhere")
ANSWERABLE = {"workflow", "reassigned"}


async def _setup(client: AsyncClient, project: dict[str, Any]) -> None:
    """`inbox` as the project's workflow; `hr` restricted, granted to nobody; the member a contributor."""
    published = await client.put(f"/api/v1/projects/{project['id']}/workflow", json={"yaml": INBOX})
    assert published.status_code == 200, published.text
    created = await client.post(ORG, json={"slug": "hr", "name": "HR", "restricted": True})
    assert created.status_code == 201, created.text
    await _uid(client, MEMBER_EMAIL, **MEMBER)


async def _seed(project: dict[str, Any], prefix: str, count: int) -> dict[str, str]:
    """`count` work items in `request`, each with one waiting request; gives each request's shape by id."""
    from tranibor_api.db.models import HumanRequest, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_core import utcnow

    now = utcnow()
    shapes: dict[str, str] = {}
    async with session_scope() as session:
        for n in range(count):
            shape = SHAPES[n % len(SHAPES)]
            item = WorkItem(
                project_id=project["id"],
                tracker_key=f"{prefix.upper()}-{n}",
                title=f"{prefix} {n}",
                state="request",
                created_at=now - timedelta(minutes=count - n),
            )
            if shape == "marked":
                item.restricted_knowledge = ["hr"]
            session.add(item)
            await session.flush()
            request = HumanRequest(
                work_item_id=item.id,
                project_id=project["id"],
                transition_id="t-approve",
                kind="approval",
                payload={"question": f"{prefix} {n}?"},
                requested_at=now - timedelta(minutes=count - n),
            )
            if shape == "reassigned":
                request.assignee = {"user": MEMBER_EMAIL}
            elif shape == "elsewhere":
                request.assignee = {"user": ELSEWHERE}
            session.add(request)
            await session.flush()
            shapes[request.id] = shape
    return shapes


async def _statements(client: AsyncClient) -> tuple[int, list[dict[str, Any]]]:
    from tranibor_api.db.session import get_engine

    seen: list[str] = []

    def count(conn: Any, cursor: Any, statement: str, *rest: Any) -> None:
        seen.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        listed = await client.get(PENDING)
    finally:
        event.remove(engine, "before_cursor_execute", count)
    assert listed.status_code == 200, listed.text
    return len(seen), list(listed.json()["items"])


async def test_the_inbox_reads_its_requests_in_statements_that_do_not_grow_with_them(
    client: AsyncClient, admin: str, project: dict[str, Any]
) -> None:
    await _setup(client, project)
    shapes = await _seed(project, "few", 4)
    async with _as(client, MEMBER_EMAIL, **MEMBER) as member:
        few, listed_few = await _statements(member)
        first = dict(shapes)
        shapes |= await _seed(project, "many", 40)
        many, listed_many = await _statements(member)

    def expected(listed: list[dict[str, Any]], seeded: dict[str, str]) -> None:
        steps = [entry for entry in listed if entry["kind"] == "human_step"]
        assert {seeded[entry["id"]] for entry in steps} <= ANSWERABLE, [seeded[e["id"]] for e in steps]
        assert len(steps) == sum(1 for shape in seeded.values() if shape in ANSWERABLE), steps

    expected(listed_few, first)
    expected(listed_many, shapes)
    assert len(listed_few) == 2
    assert len(listed_many) == 22
    assert many == few, (
        f"an inbox of 44 waiting requests read {many} statements and one of 4 read {few}: "
        "something is still read once per request"
    )


async def test_the_inbox_lists_what_the_decision_route_accepts(
    client: AsyncClient, admin: str, project: dict[str, Any]
) -> None:
    """Each request is listed exactly when `may_answer` (the decision route's verdict, one request at a
    time) says `ok` and the member reads its work item: the batch decides as the route does."""
    from sqlalchemy import select
    from tranibor_api.db.models import HumanRequest, Organization, Project, User, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.deps import principal_for_user
    from tranibor_api.knowledge.marked import readable
    from tranibor_api.services.decision_drafts import may_answer

    await _setup(client, project)
    shapes = await _seed(project, "item", 12)
    async with _as(client, MEMBER_EMAIL, **MEMBER) as member:
        _, listed = await _statements(member)
    async with session_scope() as session:
        user = (await session.execute(select(User).where(User.email == MEMBER_EMAIL))).scalar_one()
        principal = await principal_for_user(session, user)
        one_at_a_time: set[str] = set()
        rows = await session.execute(
            select(HumanRequest, WorkItem, Project, Organization.slug)
            .join(WorkItem, HumanRequest.work_item_id == WorkItem.id)
            .join(Project, HumanRequest.project_id == Project.id)
            .join(Organization, Project.org_id == Organization.id)
        )
        for row, item, row_project, org in rows.all():
            verdict = await may_answer(session, principal, item, row_project, org, row)
            if verdict == "ok" and await readable(session, principal, item, row_project.org_id):
                one_at_a_time.add(row.id)
    assert {entry["id"] for entry in listed if entry["kind"] == "human_step"} == one_at_a_time
    assert {shapes[request_id] for request_id in one_at_a_time} == ANSWERABLE
