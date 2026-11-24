"""The demonstration MCP server (ADR 0213): tools declared in a file, writes recorded, governed as ever.

A project's agent reaches a "dealer system" through the broker: the write tool is under the operation
policy `approval`, so the call waits for a person; once approved, the `ActionWorkflow`'s effect reaches
the server, which records it; a read tool returns the recorded write. The server is refused outside
development under the fakes, and refuses to declare a credential.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from .conftest import login

ORG = "/api/v1/orgs/northwind"
SAMPLE = pathlib.Path(__file__).resolve().parents[3] / "examples" / "demo-mcp" / "dealer-system.yaml"
DECISION = {"claim_id": "W-1042", "decision": "accepted", "amount_eur": 640.0, "reason": "within cover"}


@pytest.fixture
def dealer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """The sample file, loaded as the fakes' MCP server, as `TRANIBOR_DEMO_MCP_TOOLS` does."""
    import tranibor_adapters

    monkeypatch.setattr(tranibor_adapters, "FAKE_MCP", None)
    monkeypatch.setattr(tranibor_adapters, "FAKE_MCP_GATEWAY", None)
    monkeypatch.setenv("TRANIBOR_DEMO_MCP_TOOLS", str(SAMPLE))
    server = tranibor_adapters.fake_mcp_server()
    yield server
    monkeypatch.setattr(tranibor_adapters, "FAKE_MCP", None)


async def _prepare(client: AsyncClient, project: dict[str, Any]) -> tuple[str, dict[str, str]]:
    from tranibor_api.db.models import Run, WorkItem
    from tranibor_api.db.session import session_scope
    from tranibor_api.security import mint_run_token

    body = {"name": "dealer", "type": "mcp", "config": {"url": "https://dealer.test/mcp"}}
    assert (await client.post(f"{ORG}/connectors", json=body)).status_code == 201
    discovered = await client.post(f"{ORG}/connectors/dealer/discover")
    assert discovered.status_code == 200, discovered.text
    for operation, policy in (("get_claim", "allowed"), ("record_warranty_decision", "approval")):
        opened = await client.patch(
            f"{ORG}/connectors/dealer/operations/{operation}", json={"policy": policy}
        )
        assert opened.status_code == 200, opened.text
    spec = {"mcp_servers": [{"connector": "dealer", "tools": ["*"]}]}
    agent = await client.post(
        f"{ORG}/agents", json={"slug": "warranty", "display_name": "Warranty", "spec": spec}
    )
    assert agent.status_code == 201, agent.text
    async with session_scope() as session:
        item = WorkItem(project_id=project["id"], tracker_key="northwind/w#1", title="W", state="ready")
        session.add(item)
        await session.flush()
        session.add(
            Run(id="run-warranty-1", work_item_id=item.id, project_id=project["id"], stage_role="decide",
                transition_id="t", status="running", agent_slug="warranty", agent_version=1)
        )  # fmt: skip
    token = mint_run_token(
        "run-warranty-1", project_slug="billing-api", work_item_key="northwind/w#1", ttl_minutes=30
    )
    return "/api/v1/internal/runs/run-warranty-1/tools", {"Authorization": f"Bearer {token}"}


async def _play(action_id: str) -> dict[str, Any]:
    """What the `ActionWorkflow` does with the action's effect, here without Temporal."""
    from tranibor_api.db.models import Action, Project
    from tranibor_api.db.session import session_scope
    from tranibor_api.effects import EffectContext, effect

    async with session_scope() as session:
        action = await session.get(Action, action_id)
        assert action is not None
        project = await session.get(Project, action.project_id)
        assert project is not None
        result: dict[str, Any] = await effect("connector.call")(
            EffectContext(session, action, project), dict(action.effects[0]["with"])
        )
        return result


async def test_a_write_waits_for_a_person_then_reaches_the_demo_server_and_a_read_returns_it(
    client: AsyncClient, project: dict[str, Any], dealer: Any, app: Any
) -> None:
    from tranibor_api.effects import EffectRefused

    base, headers = await _prepare(client, project)
    tools = {t["name"]: t for t in (await client.get(base, headers=headers)).json()["tools"]}
    assert "dealer__record_warranty_decision" in tools and "dealer__get_claim" in tools
    assert tools["dealer__record_warranty_decision"]["inputSchema"]["required"] == ["claim_id", "decision"]
    assert "dealer__received_writes" not in tools, "discovered closed: a new tool is born forbidden"

    proposed = await client.post(f"{base}/dealer__record_warranty_decision", headers=headers, json=DECISION)
    assert proposed.json()["status_code"] == 202 and proposed.json()["result"]["status"] == "pending_approval"
    action_id = proposed.json()["result"]["action"]
    assert dealer.writes == [], "the call waits for a person: nothing reached the server"
    with pytest.raises(EffectRefused, match="not approved"):
        await _play(action_id)
    assert dealer.writes == []

    path = f"/api/v1/projects/{project['id']}/actions/{action_id}"
    own = await client.post(f"{path}/decision", json={"decision": "approve"})
    assert own.status_code == 422 and "separation of duties" in own.text, "the agent's owner does not decide"
    added = await client.post(
        f"{ORG}/members", json={"email": "owner@northwind.dev", "role": "project_owner"}
    )
    assert added.status_code in {200, 201}, added.text
    person = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    try:
        await login(person, "owner@northwind.dev")
        decided = await person.post(f"{path}/decision", json={"decision": "approve"})
    finally:
        await person.aclose()
    assert decided.status_code == 200 and decided.json()["status"] == "approved", decided.text
    assert dealer.writes == [], "approved is not played: the ActionWorkflow does that"

    played = await _play(action_id)
    assert played == {"status": "recorded", "recorded": 1}, played
    assert [(w["tool"], w["arguments"]) for w in dealer.writes] == [("record_warranty_decision", DECISION)]

    read = await client.post(f"{base}/dealer__get_claim", headers=headers, json={"claim_id": "W-1042"})
    answer = read.json()["result"]["structuredContent"]
    assert read.json()["status_code"] == 200 and answer["status"] == "awaiting decision"
    assert answer["recorded"] == [DECISION], "the read tool returns what the write recorded"
    other = await client.post(f"{base}/dealer__get_claim", headers=headers, json={"claim_id": "W-9"})
    assert other.json()["result"]["structuredContent"]["recorded"] == [], "only that claim's decisions"


async def test_an_argument_outside_the_declared_schema_never_reaches_the_server(
    client: AsyncClient, project: dict[str, Any], dealer: Any
) -> None:
    base, headers = await _prepare(client, project)
    refused = await client.post(
        f"{base}/dealer__record_warranty_decision",
        headers=headers,
        json={"claim_id": "W-1", "decision": "maybe"},
    )
    assert refused.status_code == 400, refused.text
    assert dealer.writes == []


def _write(tmp_path: pathlib.Path, text: str) -> pathlib.Path:
    path = tmp_path / "tools.yaml"
    path.write_text(text, "utf-8")
    return path


def test_the_server_serves_the_declared_tools_and_lists_what_it_received(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tranibor_adapters.fakes.demo_mcp import load_demo_mcp

    monkeypatch.setenv("TRANIBOR_FAKES", "1")
    monkeypatch.setenv("TRANIBOR_ENV", "dev")
    server = load_demo_mcp(SAMPLE)
    by_name = {t["name"]: t for t in server.tools}
    assert set(by_name) == {"get_claim", "record_warranty_decision", "received_writes"}
    assert by_name["get_claim"]["annotations"] == {"readOnlyHint": True}
    assert "annotations" not in by_name["record_warranty_decision"], "a write: no read-only declaration"
    server.gestures["record_warranty_decision"](DECISION)
    listed = server.gestures["received_writes"]({})["writes"]
    assert [(w["n"], w["tool"], w["arguments"]) for w in listed] == [
        (1, "record_warranty_decision", DECISION)
    ]


@pytest.mark.parametrize(
    ("env", "fakes", "named"),
    [
        ("prod", "1", "TRANIBOR_ENV=prod"),
        ("staging", "1", "TRANIBOR_ENV=staging"),
        ("dev", "0", "TRANIBOR_FAKES"),
    ],
)
def test_it_is_refused_outside_development_under_the_fakes(
    monkeypatch: pytest.MonkeyPatch, env: str, fakes: str, named: str
) -> None:
    from tranibor_adapters.fakes.demo_mcp import load_demo_mcp
    from tranibor_core.production import DemoMcpToolsRefusedError

    monkeypatch.setenv("TRANIBOR_ENV", env)
    monkeypatch.setenv("TRANIBOR_FAKES", fakes)
    with pytest.raises(DemoMcpToolsRefusedError, match=named):
        load_demo_mcp(SAMPLE)


@pytest.mark.parametrize(
    ("env", "fakes", "named"), [("prod", "0", "TRANIBOR_ENV=prod"), ("dev", "0", "TRANIBOR_FAKES")]
)
async def test_the_api_does_not_start_with_the_file_where_it_is_refused(
    app: Any, monkeypatch: pytest.MonkeyPatch, env: str, fakes: str, named: str
) -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_core.production import DemoMcpToolsRefusedError

    monkeypatch.setenv("TRANIBOR_DEMO_MCP_TOOLS", str(SAMPLE))
    monkeypatch.setenv("TRANIBOR_ENV", env)
    monkeypatch.setenv("TRANIBOR_FAKES", fakes)
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")
    monkeypatch.setenv("TRANIBOR_TEMPORAL_ALLOW_CLEAR_PAYLOADS", "true")  # reach this refusal, not ADR 0220's
    reset_settings_cache()
    with pytest.raises(DemoMcpToolsRefusedError, match=named):
        async with app.router.lifespan_context(app):
            pytest.fail("the API started with the demonstration MCP server where it is refused")
    reset_settings_cache()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("tools: []", "non-empty"),
        ("tools: [{name: a, kind: maybe}]", "kind: read"),
        ("tools: [{name: a, kind: read, colour: red}]", "unknown keys"),
        ("tools: [{name: a, kind: read}, {name: a, kind: write}]", "unique"),
        ("tools: [{name: a, kind: read, recorded_from: [b]}]", "not write tools"),
        ("tools: [{name: a, kind: write, answer: {api_key: sk-123}}]", "holds no credential"),
        (
            "tools: [{name: a, kind: write, input_schema: {properties: {password: {type: string}}}}]",
            "no credential",
        ),
    ],
)
def test_a_malformed_file_or_one_that_holds_a_credential_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, text: str, message: str
) -> None:
    from tranibor_adapters.errors import ConfigurationError
    from tranibor_adapters.fakes.demo_mcp import load_demo_mcp

    monkeypatch.setenv("TRANIBOR_FAKES", "1")
    monkeypatch.setenv("TRANIBOR_ENV", "dev")
    with pytest.raises(ConfigurationError, match=message):
        load_demo_mcp(_write(tmp_path, text))


def test_without_the_variable_the_fake_server_has_no_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    import tranibor_adapters

    monkeypatch.setattr(tranibor_adapters, "FAKE_MCP", None)
    monkeypatch.delenv("TRANIBOR_DEMO_MCP_TOOLS", raising=False)
    assert tranibor_adapters.fake_mcp_server().tools == []
    monkeypatch.setattr(tranibor_adapters, "FAKE_MCP", None)
