"""The core's `tracker/plugin` adapter against the ServiceNow plugin's server and a REAL instance (P1).

What the core's fakes (`FakePluginTrackerServer`) cannot prove: that `PluginTracker`, through the platform's
`PluginServerClient`, carries a work item on a real ServiceNow through the plugin's MCP server
(`plugins/servicenow/server` in the plugin repository, `PUBLISHING.plugin_repository`), and that every
answer the real system gives fits the tracker profile's schema. The server runs on its own, pointed at
the instance with the integration user of the plugin's procedure:

    cd tranibor-agents/plugins/servicenow/server
    SERVICENOW_BASE_URL=https://<instance>.service-now.com SERVICENOW_AUTH=basic \\
    SERVICENOW_USERNAME=choregos.integration SERVICENOW_PASSWORD=… SERVICENOW_WEBHOOK_SECRET=… \\
    SERVICENOW_ASSIGNMENT_GROUPS="Service Desk" CHOREGOS_SERVER_TOKEN=… PORT=18080 uv run choregos-servicenow

    TRANIBOR_LIVE_SERVICENOW_MCP_URL=http://127.0.0.1:18080/mcp TRANIBOR_LIVE_SERVICENOW_MCP_TOKEN=… \\
    TRANIBOR_LIVE_SERVICENOW_WEBHOOK_SECRET=… uv run pytest tests/live/test_servicenow_live.py -m live

Every record it files carries `[choregos-live <run>]` in its title and is left resolved; the plugin's own
live suite (`server/tests/live`) deletes its records when given a cleanup user, this one does not hold
one. First run: a Personal Developer Instance of the Australia release, patch 7, 2026-11-21.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from tranibor_adapters.errors import UpstreamError
from tranibor_adapters.plugin_servers import PluginServerClient
from tranibor_adapters.tracker.plugin import PluginTracker
from tranibor_contracts import ProjectConfig
from tranibor_contracts.delivery import ProjectDelivery, RepoConfig
from tranibor_core.domain import NewItem, TrackerStateMapping
from tranibor_core.plugins import TRACKER_PROFILE

from .conftest import require

pytestmark = pytest.mark.live

RUN = uuid4().hex[:8]
TAG = f"[choregos-live {RUN}]"
GROUP = "Service Desk"


@pytest.fixture
async def server() -> AsyncIterator[PluginServerClient]:
    env = require("TRANIBOR_LIVE_SERVICENOW_MCP_URL", "TRANIBOR_LIVE_SERVICENOW_MCP_TOKEN")
    yield PluginServerClient(
        env["TRANIBOR_LIVE_SERVICENOW_MCP_URL"], token=env["TRANIBOR_LIVE_SERVICENOW_MCP_TOKEN"], timeout_s=60
    )


@pytest.fixture
def audited() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def tracker(server: PluginServerClient, audited: list[tuple[str, dict[str, Any]]]) -> PluginTracker:
    async def audit(tool: str, details: dict[str, Any]) -> None:
        audited.append((tool, details))

    return PluginTracker(server, "servicenow", "servicenow", audit=audit)


async def structured(server: PluginServerClient, tool: str, **arguments: Any) -> dict[str, Any]:
    """One of the server's own tools, called as the platform calls it."""
    result = await server.call_tool(tool, arguments)
    assert not result.get("isError"), result
    answer = result.get("structuredContent")
    if not isinstance(answer, dict):
        answer = json.loads("".join(p.get("text", "") for p in result.get("content") or []))
    return dict(answer)


def recent(moment: datetime | str | None) -> bool:
    assert moment is not None
    when = moment if isinstance(moment, datetime) else datetime.fromisoformat(str(moment))
    return abs(datetime.now(UTC) - when) < timedelta(minutes=10)


async def test_the_server_serves_the_tracker_profile(tracker: PluginTracker) -> None:
    answer = await tracker.test()
    assert answer["ok"] is True, answer
    assert set(TRACKER_PROFILE) <= {t.name for t in await tracker.client.list_tools()}  # type: ignore[attr-defined]


async def test_a_work_item_carried_on_a_real_instance(
    tracker: PluginTracker, server: PluginServerClient, audited: list[tuple[str, dict[str, Any]]]
) -> None:
    by_name = [{"field": "name", "op": "eq", "value": GROUP}]
    groups = await structured(server, "search_records", table="sys_user_group", filters=by_name)
    group = groups["records"][0]["sys_id"]
    new = NewItem(
        title=f"{TAG} carried by the core's adapter",
        body=f"Filed by the core's live suite, run {RUN}.",
        fields={"assignment_group": group},
    )
    key = await tracker.create_item(new)
    assert key.startswith("INC")
    assert await tracker.create_item(new) == key  # a replayed activity files nothing more

    item = await tracker.fetch_item(key)
    assert (item.title, item.state, item.item_type) == (new.title, "New", "incident")
    assert recent(item.created_at)  # the server hands out UTC; ServiceNow's labels are local time
    assert item.fields["tracker"]["kind"] == "servicenow"
    assert item.fields["assignment_group"] == {"value": group, "display": GROUP}

    await tracker.set_state(key, TrackerStateMapping(status="In Progress"))
    status = "### Tranibor\n\n| Stage | Status |\n| --- | --- |\n| triage | ✅ |"
    for _ in range(2):
        await tracker.upsert_status_comment(key, status, "<!-- choregos:status -->")
    # An accent, an emoji and a caret: the encoding is the point.
    text = f"Run {RUN}: the triage started — é, ✅, ^ORactive=false."  # lang: fr-ok
    first = await tracker.comment(key, text)
    again = await tracker.comment(key, text)
    assert first == again

    config = ProjectConfig(
        slug="itsm",
        org="acme",
        delivery=ProjectDelivery(repo=RepoConfig(url="https://example.invalid/x/y.git")),
    )
    assert key in await tracker.list_candidates(config)

    item = await tracker.fetch_item(key)
    assert item.state == "In Progress"
    notes = [c for c in item.comments if RUN in c.body or "[choregos-status:" in c.body]
    assert len(notes) == 2, [c.body for c in item.comments]  # one status, one comment: neither written twice
    # The status marker is Tranibor's `<!-- choregos:status -->` reduced to the profile's alphabet.
    comment_marker = first.removeprefix("[choregos:").removesuffix("]")
    assert {c.marker for c in notes} == {"choregos:status", comment_marker}
    assert all(recent(c.ts) for c in notes)

    await tracker.close_item(key)
    closed = await tracker.fetch_item(key)
    assert closed.state == "Resolved"
    assert key not in await tracker.list_candidates(config)
    assert [tool for tool, _ in audited] == [
        "tracker_create_item", "tracker_create_item", "tracker_set_state", "tracker_upsert_status",
        "tracker_upsert_status", "tracker_comment", "tracker_comment", "tracker_close",
    ]  # fmt: skip


async def test_a_delivery_shaped_as_the_business_rule_sends_it(
    tracker: PluginTracker, server: PluginServerClient
) -> None:
    """The procedure's rule posts `{table, sys_id, number, sys_mod_count, operation, state}` (read from a
    real instance's outbound log): the server verifies its secret and the adapter hands out one event."""
    secret = require("TRANIBOR_LIVE_SERVICENOW_WEBHOOK_SECRET")["TRANIBOR_LIVE_SERVICENOW_WEBHOOK_SECRET"]
    key = await tracker.create_item(NewItem(title=f"{TAG} a delivery", body=f"Run {RUN}."))
    record = await structured(server, "get_record", table="incident", number=key)
    body = json.dumps(
        {
            "table": "incident",
            "sys_id": record["sys_id"],
            "number": key,
            "sys_mod_count": int(record["fields"]["sys_mod_count"]),
            "operation": "insert",
            "state": record["fields"]["state"],
        },
        separators=(",", ":"),
    ).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Choregos-Secret": secret,
        "User-Agent": "ServiceNow/1.0",
    }
    verified, events = await tracker.parse_event(headers, body)
    assert verified is True
    assert events == [
        {
            "kind": "tracker.item.created",
            "item_key": key,
            "delivery_id": f"{record['sys_id']}:{record['fields']['sys_mod_count']}",
        }
    ]
    assert await tracker.parse_event({**headers, "X-Choregos-Secret": "x" * 48}, body) == (False, [])
    await tracker.close_item(key)


async def test_a_refusal_of_the_instance_is_an_upstream_error(tracker: PluginTracker) -> None:
    with pytest.raises(UpstreamError) as raised:
        await tracker.fetch_item("INC9999999")
    assert "tracker_fetch_item" in str(raised.value)
