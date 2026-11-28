"""The ServiceNow tracker: what Tranibor sends to an instance, and what it understands in return (ADR 0228).

The tests speak the Table API, the Change Management API and the Business Rule's payload through a
scriptable in-memory instance (`FakeServiceNowInstance`). They prove the requests' shape and the tracker's
behaviour; not that a real instance accepts them.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from tranibor_adapters.errors import ConfigurationError, UpstreamError
from tranibor_adapters.fakes.servicenow import FakeServiceNowInstance, sys_id_of
from tranibor_adapters.tracker.servicenow import CLOSE_NOTES, STATUS_COMMENT_MARKER, ServiceNowTracker
from tranibor_adapters.tracker.servicenow_api import ServiceNowClient, ServiceNowOAuth
from tranibor_adapters.tracker.servicenow_events import ServiceNowEventError, parse_servicenow_event
from tranibor_contracts import InboundEventType, ProjectConfig, wire
from tranibor_core.domain import NewItem, TrackerStateMapping

BASE = "https://acme.service-now.com"
SECRET = "hook-secret"


def build(instance: FakeServiceNowInstance, *, oauth: bool = False, **options: Any) -> ServiceNowTracker:
    http = httpx.AsyncClient(transport=instance.transport())
    if oauth:
        client = ServiceNowClient(
            BASE, oauth=ServiceNowOAuth(instance.client_id, instance.client_secret), client=http
        )
    else:
        client = ServiceNowClient(BASE, username=instance.username, password=instance.password, client=http)
    options.setdefault("assignment_groups", ["Service Desk"])
    return ServiceNowTracker(client, webhook_secret=SECRET, **options)


def project() -> ProjectConfig:
    return ProjectConfig(slug="desk", org="acme")


def queries(instance: FakeServiceNowInstance, table: str) -> list[str]:
    return [
        str(r.url.params.get("sysparm_query"))
        for r in instance.requests
        if r.method == "GET" and r.url.path == f"/api/now/table/{table}"
    ]


# ───────────────────────────── reading ─────────────────────────────


async def test_fetch_item_maps_the_record_and_its_journal() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed(
        "incident",
        "VPN drops every hour",
        state="2",
        description="Since Monday the VPN drops.",
        caller_id=instance.user("Abel Tuter"),
        assigned_to=instance.user("Beth Anglin"),
    )
    tracker = build(instance)
    await tracker.comment(number, "Looked at the logs.")

    item = await tracker.fetch_item(number)

    assert item.key == number and item.title == "VPN drops every hour"
    assert item.body == "Since Monday the VPN drops."
    assert item.state == "In Progress" and item.item_type == "incident" and item.labels == []
    assert item.author == "Abel Tuter" and item.assignees == ["Beth Anglin"]
    sys_id = instance.by_number(number)["sys_id"]
    assert item.url == f"{BASE}/nav_to.do?uri=incident.do?sys_id={sys_id}"
    assert item.created_at is not None and item.created_at.tzinfo is not None
    tracker_block = item.fields["tracker"]
    assert tracker_block["kind"] == "servicenow" and tracker_block["reporter"]["display_name"] == "Abel Tuter"
    assert tracker_block["answers"]["assignment_group"] == "Service Desk"
    assert tracker_block["answers"]["priority"] == "5"
    assert item.fields["servicenow"] == {"table": "incident", "sys_id": sys_id, "state_value": "2"}
    assert [c.body.splitlines()[0] for c in item.comments] == ["Looked at the logs."]
    assert item.comments[0].marker is not None and item.comments[0].marker.startswith("[tranibor:note:")


async def test_a_journal_hidden_by_its_acl_is_read_from_the_record() -> None:
    """An `itil` user reads no `sys_journal_field` row (200, empty): the record's rendered journal is read
    instead, and a status note is still found, never written twice."""
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Printer")
    tracker = build(instance)
    await tracker.upsert_status_comment(number, "Stage: triage")
    instance.journal_readable = False

    await tracker.upsert_status_comment(number, "Stage: triage")
    item = await tracker.fetch_item(number)

    assert len(instance.journal(number)) == 1
    assert item.comments[-1].marker == STATUS_COMMENT_MARKER


async def test_an_unknown_number_is_refused_before_any_call() -> None:
    instance = FakeServiceNowInstance()
    with pytest.raises(ConfigurationError, match="INC…"):
        await build(instance).fetch_item("PRB0000001")
    assert instance.requests == []
    with pytest.raises(UpstreamError, match="no incident numbered INC0099999"):
        await build(instance).fetch_item("INC0099999")


async def test_oauth_fetches_a_token_once_and_renews_it_on_a_401() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Mail")
    tracker = build(instance, oauth=True)

    await tracker.fetch_item(number)
    instance.tokens.clear()  # the instance forgot the token: the next call is refused once
    await tracker.fetch_item(number)

    token_calls = [r for r in instance.requests if r.url.path == "/oauth_token.do"]
    assert len(token_calls) == 2


# ───────────────────────────── creating ─────────────────────────────


async def test_create_item_files_an_incident_once_per_text() -> None:
    instance = FakeServiceNowInstance()
    tracker = build(instance)
    data = NewItem(title="Badge reader broken", body="Door B", fields={"issue_type": "Bug", "impact": "2"})

    first = await tracker.create_item(data)
    again = await tracker.create_item(data)

    assert first == again and first.startswith("INC")
    record = instance.by_number(first)
    assert record["correlation_id"].startswith(f"{wire.NAME}:")
    assert record["impact"] == "2" and "issue_type" not in record
    assert len(instance.tables["incident"]) == 1


async def test_create_item_refuses_a_reference_that_is_not_a_sys_id() -> None:
    with pytest.raises(ConfigurationError, match="assignment_group"):
        await build(FakeServiceNowInstance()).create_item(
            NewItem(title="x", body="y", fields={"assignment_group": "Service Desk"})
        )


# ───────────────────────────── states ─────────────────────────────


async def test_set_state_moves_by_label_case_blind_and_once() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop")
    tracker = build(instance)

    await tracker.set_state(number, TrackerStateMapping(status="in progress", label="agent-ready"))
    writes = len([r for r in instance.requests if r.method == "PATCH"])
    await tracker.set_state(number, TrackerStateMapping(status="In Progress"))

    assert instance.by_number(number)["state"] == "2"
    assert len([r for r in instance.requests if r.method == "PATCH"]) == writes, "a replay writes nothing"


async def test_set_state_refuses_a_closing_state_and_an_unknown_one_naming_the_valid_ones() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop")
    tracker = build(instance)

    with pytest.raises(ConfigurationError, match="closes"):
        await tracker.set_state(number, TrackerStateMapping(status="Resolved"))
    with pytest.raises(ConfigurationError, match=r"states: New, In Progress, On Hold"):
        await tracker.set_state(number, TrackerStateMapping(status="Doing"))
    assert instance.by_number(number)["state"] == "1"


async def test_a_request_item_takes_the_states_of_task() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("sc_req_item", "New laptop")
    await build(instance).set_state(number, TrackerStateMapping(status="Work in Progress"))
    assert instance.by_number(number)["state"] == "2"


async def test_without_a_read_on_sys_choice_the_out_of_box_states_are_used() -> None:
    instance = FakeServiceNowInstance()
    instance.choices_readable = False
    number = instance.seed("incident", "Laptop")
    await build(instance).set_state(number, TrackerStateMapping(status="On Hold"))
    assert instance.by_number(number)["state"] == "3"


async def test_a_change_moves_only_to_a_state_its_model_reaches() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("change_request", "Patch the VPN", state="-5")
    tracker = build(instance)

    with pytest.raises(
        ConfigurationError, match=r'"Implement" is not reachable from "New".*Assess, Canceled'
    ):
        await tracker.set_state(number, TrackerStateMapping(status="Implement"))
    await tracker.set_state(number, TrackerStateMapping(status="Assess"))

    assert instance.by_number(number)["state"] == "-4"
    table_patches = [r for r in instance.requests if r.method == "PATCH" and "/api/now/table/" in r.url.path]
    assert table_patches == [], "a change's state never goes through the Table API"


async def test_a_change_api_without_its_role_is_said() -> None:
    instance = FakeServiceNowInstance()
    instance.change_api_readable = False
    number = instance.seed("change_request", "Patch", state="-5")
    with pytest.raises(UpstreamError, match="sn_change_read"):
        await build(instance).set_state(number, TrackerStateMapping(status="Assess"))


# ───────────────────────────── journal writes ─────────────────────────────


async def test_upsert_status_writes_a_work_note_once_per_changed_text() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop")
    tracker = build(instance)

    await tracker.upsert_status_comment(number, "Stage: triage", wire.marker("status"))
    await tracker.upsert_status_comment(number, "Stage: triage", wire.marker("status"))
    await tracker.upsert_status_comment(number, "Stage: fix", wire.marker("status"))

    notes = instance.journal(number)
    assert notes == [f"{STATUS_COMMENT_MARKER}\nStage: triage", f"{STATUS_COMMENT_MARKER}\nStage: fix"]
    assert "<!--" not in "".join(notes), "a work note is plain text: the marker is not an HTML comment"
    assert instance.journal(number, "comments") == [], "never a customer-visible comment"


async def test_comment_is_an_idempotent_work_note() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop")
    tracker = build(instance)

    first = await tracker.comment(number, "A question for the desk.")
    again = await tracker.comment(number, "A question for the desk.")

    assert first == again
    assert instance.journal(number) == [f"A question for the desk.\n\n{first}"]
    assert instance.journal(number, "comments") == []


async def test_link_writes_a_work_note_naming_the_other_item() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop")
    await build(instance).link(number, "INC0000042", "duplicate")
    assert instance.journal(number)[0].startswith("Duplicate of INC0000042")


async def test_close_resolves_an_incident_with_its_close_code_once() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop", state="2")
    tracker = build(instance)

    await tracker.close_item(number)
    writes = len([r for r in instance.requests if r.method == "PATCH"])
    await tracker.close_item(number)

    record = instance.by_number(number)
    assert record["state"] == "6" and record["close_code"] == "Solution provided"
    assert record["close_notes"] == CLOSE_NOTES
    assert len([r for r in instance.requests if r.method == "PATCH"]) == writes


async def test_close_a_request_item_complete_with_a_work_note() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("sc_req_item", "Laptop", state="2")
    await build(instance).close_item(number)
    assert instance.by_number(number)["state"] == "3"
    assert instance.journal(number) == [CLOSE_NOTES]


async def test_a_write_the_instance_keeps_silently_is_an_error() -> None:
    instance = FakeServiceNowInstance()
    instance.unwritable.add(("incident", "state"))
    number = instance.seed("incident", "Laptop")
    with pytest.raises(UpstreamError, match="did not apply state"):
        await build(instance).set_state(number, TrackerStateMapping(status="On Hold"))


# ───────────────────────────── fields ─────────────────────────────


async def test_set_fields_writes_the_updatable_fields_and_skips_the_bookkeeping() -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop")
    group = instance.group("Network")

    await build(instance).set_fields(
        number,
        {
            "Cost (€)": 1.5,
            "Size": "M",
            "Risk": "low",
            "Run": "https://x",
            "urgency": "1",
            "assignment_group": group,
        },
    )

    record = instance.by_number(number)
    assert record["urgency"] == "1" and record["assignment_group"] == group
    assert "Size" not in record and "Cost (€)" not in record


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"priority": "1"}, "priority is never written"),
        ({"short_description": "x"}, "not a field the tracker writes"),
        ({"impact": "9"}, "1 \\(high\\)"),
        ({"cmdb_ci": "web-01"}, "sys_id"),
    ],
)
async def test_set_fields_refuses_priority_and_the_unknown(fields: dict[str, Any], message: str) -> None:
    instance = FakeServiceNowInstance()
    number = instance.seed("incident", "Laptop")
    with pytest.raises(ConfigurationError, match=message):
        await build(instance).set_fields(number, fields)
    assert not [r for r in instance.requests if r.method == "PATCH"]


# ───────────────────────────── candidates ─────────────────────────────


async def test_list_candidates_reads_the_groups_queue_newest_first() -> None:
    instance = FakeServiceNowInstance()
    old = instance.seed("incident", "Old")
    other = instance.seed("incident", "Other team", group="Network")
    instance.seed("incident", "Resolved", state="6")
    request = instance.seed("sc_req_item", "Laptop request")
    newer = instance.seed("incident", "Newer")
    closed_change = instance.seed("change_request", "Done", state="3")

    keys = await build(instance).list_candidates(project())

    assert keys == [newer, request, old]
    assert other not in keys and closed_change not in keys
    assert "assignment_group.nameINService Desk" in queries(instance, "incident")[0]


async def test_list_candidates_by_sys_id_and_only_the_configured_tables() -> None:
    instance = FakeServiceNowInstance()
    desk = instance.group("Service Desk")
    incident = instance.seed("incident", "A")
    instance.seed("sc_req_item", "B")

    keys = await build(instance, assignment_groups=[desk], tables=["incident"]).list_candidates(project())

    assert keys == [incident]
    assert queries(instance, "sc_req_item") == []
    assert f"assignment_groupIN{desk}" in queries(instance, "incident")[0]


async def test_a_group_cannot_change_the_query() -> None:
    instance = FakeServiceNowInstance()
    instance.seed("incident", "A")
    with pytest.raises(ConfigurationError, match="javascript"):
        await build(instance, assignment_groups=["javascript:gs.getUser()"]).list_candidates(project())
    tracker = build(instance, assignment_groups=["Desk^ORactive=false"])
    assert await tracker.list_candidates(project()) == []
    assert "Desk^^ORactive=false" in queries(instance, "incident")[0]


# ───────────────────────────── the webhook ─────────────────────────────


def delivery(**overrides: Any) -> bytes:
    event = {
        "table": "incident",
        "sys_id": sys_id_of("INC0010001"),
        "number": "INC0010001",
        "sys_mod_count": 3,
        "operation": "update",
        "state": "2",
        **overrides,
    }
    return json.dumps(event).encode()


def test_verify_webhook_compares_the_shared_secret() -> None:
    tracker = build(FakeServiceNowInstance())
    assert tracker.verify_webhook({"X-Tranibor-Secret": SECRET}, b"{}")
    assert not tracker.verify_webhook({"X-Tranibor-Secret": "wrong"}, b"{}")
    assert not tracker.verify_webhook({}, b"{}")
    no_secret = build(FakeServiceNowInstance())
    no_secret.webhook_secret = ""
    assert not no_secret.verify_webhook({"X-Tranibor-Secret": ""}, b"{}")


def test_parse_webhook_keys_the_delivery_by_sys_id_and_mod_count() -> None:
    (event,) = build(FakeServiceNowInstance()).parse_webhook({}, delivery())
    assert event.type == InboundEventType.ITEM_UPDATED
    assert event.work_item_key == "INC0010001" and event.source == "servicenow"
    assert event.delivery_id == f"{sys_id_of('INC0010001')}:3"


@pytest.mark.parametrize(
    ("overrides", "kind"),
    [
        ({"state": "6"}, InboundEventType.ITEM_CLOSED),
        ({"operation": "insert"}, InboundEventType.ITEM_CREATED),
        ({"operation": "comment"}, InboundEventType.ITEM_COMMENTED),
    ],
)
def test_the_operation_and_a_closing_state_say_the_kind(overrides: dict[str, Any], kind: str) -> None:
    (event,) = parse_servicenow_event(delivery(**overrides))
    assert event.type == kind


def test_a_table_outside_the_connectors_is_skipped_and_a_malformed_event_refused() -> None:
    change = delivery(table="change_request", number="CHG0010001")
    assert build(FakeServiceNowInstance(), tables=["incident"]).parse_webhook({}, change) == []
    assert parse_servicenow_event(delivery(table="problem")) == []
    for body in (
        b"not json",
        delivery(sys_id="x"),
        delivery(number="RITM0010001"),
        delivery(sys_mod_count=-1),
    ):
        with pytest.raises(ServiceNowEventError):
            parse_servicenow_event(body)
    two = json.dumps([json.loads(delivery()), json.loads(delivery(sys_mod_count=4))]).encode()
    assert len(parse_servicenow_event(two)) == 2


async def test_the_connector_test_reads_the_table_and_its_states() -> None:
    instance = FakeServiceNowInstance()
    tracker = build(instance)
    assert await tracker.test() == {
        "ok": True,
        "table": "incident",
        "states": ["New", "In Progress", "On Hold", "Resolved", "Closed", "Canceled"],
    }
    instance.password = "rotated"
    refused = await tracker.test()
    assert refused["ok"] is False and refused["reason"].startswith("401")
