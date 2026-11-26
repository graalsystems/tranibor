# SPDX-License-Identifier: Apache-2.0
"""A profile entry of an agent's `mcp_servers` opens the instances that implement it (ADR 0224 §4, S86-04).

The notes plugin of the fixtures declares the profile `notes/notes@1` (get_provider, list_notes, add_note).
Two connectors stand for two instances of servers that implement it: they are plain MCP connectors that
carry the declaration a plugin's connector keeps (`plugin_server.implements`), so what is proven here is the
broker's resolution and the connector path it reuses. What S86-03 adds is making such instances from one
server; nothing here depends on how they were made."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient

ORG = "/api/v1/orgs/northwind"
PROFILE = "notes/notes@1"
FIXTURES = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "profiles"
TOOLS: list[dict[str, Any]] = [
    {
        "name": name,
        "description": name,
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }
    for name in ("get_provider", "list_notes", "add_note", "purge_notes")
]


@pytest.fixture
def notes_installed(tmp_path: Path) -> Iterator[None]:
    from tranibor_api.services.plugin_supply import OfflineDirectory, use_plugin_supply

    root = tmp_path / "plugins"
    shutil.copytree(FIXTURES / "notes", root / "notes" / "1.0.0")
    use_plugin_supply(OfflineDirectory(root))
    yield
    use_plugin_supply(None)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    import tranibor_adapters
    from tranibor_adapters.fakes.mcp import FakeMcpServer

    fake = FakeMcpServer(tools=[dict(t) for t in TOOLS], token="cle")
    monkeypatch.setattr(tranibor_adapters, "FAKE_MCP", fake)
    monkeypatch.setenv("CONNECTOR_TEST_CLE", "cle")
    yield fake


async def _instance(client: AsyncClient, name: str, *, implements: list[str] | None = None) -> None:
    """A connector that carries what a plugin server's connector keeps, its operations open."""
    from sqlalchemy import select
    from tranibor_api.db.models import OrgConnector
    from tranibor_api.db.session import session_scope

    body = {
        "name": name,
        "type": "mcp",
        "config": {"url": f"https://{name}.test/mcp"},
        "secret_refs": {"token": "env:CONNECTOR_TEST_CLE"},
    }
    assert (await client.post(f"{ORG}/connectors", json=body)).status_code == 201
    assert (await client.post(f"{ORG}/connectors/{name}/discover")).status_code == 200
    for tool in TOOLS:
        policy = "allowed"
        opened = await client.patch(
            f"{ORG}/connectors/{name}/operations/{tool['name']}", json={"policy": policy}
        )
        assert opened.status_code == 200, opened.text
    async with session_scope() as session:
        row = (await session.execute(select(OrgConnector).where(OrgConnector.name == name))).scalar_one()
        row.secret_refs = {**row.secret_refs, "_server_token": "env:CONNECTOR_TEST_CLE"}
        row.plugin_server = {
            "plugin": "notes",
            "url": f"https://{name}.test/mcp",
            "implements": [PROFILE] if implements is None else implements,
            "operations": {
                t["name"]: {"access": "write" if t["name"] in ("add_note", "purge_notes") else "read"}
                for t in TOOLS
            },
        }


async def _install_notes(client: AsyncClient) -> None:
    installed = await client.post(
        f"{ORG}/plugins/install", json={"source": "offline", "name": "notes", "version": "1.0.0"}
    )
    assert installed.status_code == 201, installed.text


async def _agent(client: AsyncClient, entries: list[dict[str, Any]]) -> None:
    created = await client.post(
        f"{ORG}/agents",
        json={"slug": "rangeur", "display_name": "Rangeur", "spec": {"mcp_servers": entries}},
    )
    assert created.status_code == 201, created.text


async def _run(project: dict[str, Any]) -> tuple[str, dict[str, str]]:
    from tranibor_api.db.models import Run, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.security import mint_run_token

    async with session_scope() as session:
        item = WorkItem(project_id=project["id"], tracker_key="northwind/x#1", title="T", state="ready")
        session.add(item)
        await session.flush()
        session.add(
            Run(
                id="run-profile-1",
                work_item_id=item.id,
                project_id=project["id"],
                stage_role="plan",
                transition_id="t-plan",
                status="running",
                agent_slug="rangeur",
                agent_version=1,
            )
        )
    token = mint_run_token(
        "run-profile-1", project_slug="billing-api", work_item_key="northwind/x#1", ttl_minutes=30
    )
    return "/api/v1/internal/runs/run-profile-1/tools", {"Authorization": f"Bearer {token}"}


async def _names(client: AsyncClient, base: str, headers: dict[str, str]) -> list[str]:
    listed = (await client.get(base, headers=headers)).json()["tools"]
    return sorted(o["name"] for o in listed if o["name"] != "web_fetch")


async def test_a_profile_entry_opens_every_instance_under_its_own_name_and_only_the_profile_tools(
    client: AsyncClient, project: dict[str, Any], server: Any, notes_installed: None
) -> None:
    await _install_notes(client)
    await _instance(client, "notes-a")
    await _instance(client, "notes-b")
    await _agent(client, [{"profile": PROFILE, "tools": ["*"]}])
    base, headers = await _run(project)
    assert await _names(client, base, headers) == [
        "notes-a__add_note",
        "notes-a__get_provider",
        "notes-a__list_notes",
        "notes-b__add_note",
        "notes-b__get_provider",
        "notes-b__list_notes",
    ], "`purge_notes` is the servers' own: the profile does not name it, `*` does not open it"


async def test_the_entry_lists_the_tools_it_opens(
    client: AsyncClient, project: dict[str, Any], server: Any, notes_installed: None
) -> None:
    await _install_notes(client)
    await _instance(client, "notes-a")
    await _instance(client, "notes-b")
    await _agent(client, [{"profile": PROFILE, "tools": ["list_*", "get_provider"]}])
    base, headers = await _run(project)
    assert await _names(client, base, headers) == [
        "notes-a__get_provider",
        "notes-a__list_notes",
        "notes-b__get_provider",
        "notes-b__list_notes",
    ]


async def test_an_instance_goes_through_the_connector_path_policy_and_availability(
    client: AsyncClient, project: dict[str, Any], server: Any, notes_installed: None
) -> None:
    """The reuse is the point: no second path decides what an instance's operation may do."""
    await _install_notes(client)
    await _instance(client, "notes-a")
    await _instance(client, "notes-b")
    closed = await client.patch(
        f"{ORG}/connectors/notes-b/operations/list_notes", json={"policy": "forbidden"}
    )
    assert closed.status_code == 200
    held = await client.patch(f"{ORG}/connectors/notes-a/operations/add_note", json={"policy": "approval"})
    assert held.status_code == 200
    await _agent(client, [{"profile": PROFILE, "tools": ["list_notes", "add_note"]}])
    base, headers = await _run(project)
    tools = {o["name"]: o for o in (await client.get(base, headers=headers)).json()["tools"]}
    assert "notes-b__list_notes" not in tools, "forbidden in that instance: it does not exist for the run"
    assert "needs a human approval" in tools["notes-a__add_note"]["description"]
    assert "notes-a__list_notes" in tools and "notes-b__add_note" in tools
    proposed = await client.post(f"{base}/notes-a__add_note", headers=headers, json={})
    assert proposed.json()["result"]["status"] == "pending_approval"
    assert server.calls == [], "the approval is the instance's own: nothing reached the server"


async def test_a_call_names_the_instance_in_the_ledger_and_the_audit(
    client: AsyncClient, project: dict[str, Any], server: Any, notes_installed: None
) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import CostLedger, Event
    from tranibor_api.db.session import session_scope

    await _install_notes(client)
    await _instance(client, "notes-a")
    await _instance(client, "notes-b")
    await _agent(client, [{"profile": PROFILE, "tools": ["list_notes"]}])
    base, headers = await _run(project)
    answered = await client.post(f"{base}/notes-b__list_notes", headers=headers, json={})
    # A plugin-server connector answers on the in-process fake of the harness (which serves the tracker
    # profile, not notes): the call reaches the instance and is refused there. What this test proves is
    # whose name the ledger and the audit carry, not what the server answers.
    assert answered.status_code == 200, answered.text
    async with session_scope() as session:
        (row,) = (
            await session.execute(select(CostLedger).where(CostLedger.run_id == "run-profile-1"))
        ).scalars()
        called = [
            e
            for e in (
                await session.execute(select(Event).where(Event.type == "choregos.tool.called"))
            ).scalars()
            if e.subject == "run-profile-1"
        ]
    assert (row.provider, row.model) == ("mcp:notes-b", "notes-b__list_notes")
    assert len(called) == 1 and "notes-b__list_notes" in str(called[0].payload)


async def test_an_unbound_profile_opens_nothing_and_says_why(
    client: AsyncClient, project: dict[str, Any], server: Any, notes_installed: None
) -> None:
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.broker import ProfileEntry, ProfileReport, tools_of_selection

    await _install_notes(client)
    await _instance(client, "elsewhere", implements=[])  # a connector that implements nothing
    await _agent(client, [{"profile": PROFILE, "tools": ["*"]}])
    base, headers = await _run(project)
    assert await _names(client, base, headers) == [], "the run starts and is offered nothing of the profile"
    from tranibor_api.db.models import Project

    note = ProfileReport()
    async with session_scope() as session:
        row = await session.get(Project, project["id"])
        assert row is not None
        assert (
            await tools_of_selection(session, {}, row, profiles=[ProfileEntry(PROFILE, ("*",))], report=note)
            == {}
        )
    assert note.unbound == {PROFILE: "no connector of the organisation implements it"}


async def test_an_entry_for_a_profile_nobody_declares_is_unbound_too(
    client: AsyncClient, project: dict[str, Any], server: Any
) -> None:
    from tranibor_api.db.models import Project
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.broker import ProfileEntry, ProfileReport, tools_of_selection

    await _instance(client, "notes-a")
    note = ProfileReport()
    async with session_scope() as session:
        row = await session.get(Project, project["id"])
        assert row is not None
        await tools_of_selection(session, {}, row, profiles=[ProfileEntry(PROFILE, ("*",))], report=note)
    assert list(note.unbound) == [PROFILE] and "not installed" in note.unbound[PROFILE]


async def test_the_cap_keeps_the_first_tools_and_names_the_ones_it_leaves_out(
    client: AsyncClient,
    project: dict[str, Any],
    server: Any,
    notes_installed: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tranibor_api import config
    from tranibor_api.db.models import Project
    from tranibor_api.db.session import session_scope
    from tranibor_api.services.broker import ProfileEntry, ProfileReport, tools_of_selection

    await _install_notes(client)
    for name in ("notes-a", "notes-b", "notes-c"):
        await _instance(client, name)
    assert config.get_settings().broker_profile_tool_cap == 16, "the default"
    monkeypatch.setenv("TRANIBOR_BROKER_PROFILE_TOOL_CAP", "4")
    config.get_settings.cache_clear()
    note = ProfileReport()
    async with session_scope() as session:
        row = await session.get(Project, project["id"])
        assert row is not None
        tools = await tools_of_selection(
            session, {}, row, profiles=[ProfileEntry(PROFILE, ("*",))], report=note
        )
    # The profile's order, then the instance: get_provider of the three, then list_notes of the first.
    assert sorted(tools) == [
        "notes-a__get_provider",
        "notes-a__list_notes",
        "notes-b__get_provider",
        "notes-c__get_provider",
    ]
    config.get_settings.cache_clear()
    assert note.left_out == {
        PROFILE: [
            "notes-b__list_notes",
            "notes-c__list_notes",
            "notes-a__add_note",
            "notes-b__add_note",
            "notes-c__add_note",
        ]
    }


async def test_the_run_start_journals_what_the_entries_left_out_once(
    client: AsyncClient, project: dict[str, Any], server: Any, notes_installed: None
) -> None:
    from sqlalchemy import select
    from tranibor_api.db.models import Project, RunEvent
    from tranibor_api.db.session import session_scope
    from tranibor_api.schemas.agents import AgentSpec
    from tranibor_core.run_journal import PROFILE_UNBOUND, platform_written
    from tranibor_orchestrator.activities.stage_tools import _journal_profile_lines

    await _install_notes(client)
    await _agent(client, [{"profile": PROFILE, "tools": ["*"]}])
    await _run(project)  # the run row
    agent = SimpleNamespace(
        spec=AgentSpec.model_validate({"mcp_servers": [{"profile": PROFILE, "tools": ["*"]}]})
    )
    async with session_scope() as session:
        row = await session.get(Project, project["id"])
        assert row is not None
        await _journal_profile_lines(session, "run-profile-1", row, agent)
        await session.flush()
        await _journal_profile_lines(session, "run-profile-1", row, agent)  # a replayed stage
        await session.flush()
        lines = (
            (await session.execute(select(RunEvent).where(RunEvent.run_id == "run-profile-1")))
            .scalars()
            .all()
        )
    (line,) = lines
    assert line.type == PROFILE_UNBOUND and line.seq < 0
    assert line.payload["profile"] == PROFILE and "no connector" in line.payload["reason"]
    assert platform_written(PROFILE_UNBOUND), "a run cannot write it itself"
