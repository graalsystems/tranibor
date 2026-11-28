"""A ServiceNow Business Rule's delivery is authenticated, de-duplicated, routed to the project whose
ServiceNow tracker holds it, and the record re-read through that connector (ADR 0228).

The deliveries are the Business Rule's of the setup procedure; the instance is the scriptable fake. They
prove the platform's side of the exchange, not that a live instance sends exactly this.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from tranibor_adapters.fakes.servicenow import FakeServiceNowInstance
from tranibor_adapters.tracker.servicenow import ServiceNowTracker
from tranibor_adapters.tracker.servicenow_api import ServiceNowClient

PLATFORM_SECRET = "platform-s3cret"
DESK_SECRET = "desk-hook"
OPS_SECRET = "ops-hook"


@pytest.fixture(autouse=True)
def _secrets(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_api.config import get_settings

    monkeypatch.setattr(get_settings(), "generic_webhook_secret", PLATFORM_SECRET)
    monkeypatch.setenv("CONNECTOR_SN_DESK_HOOK", DESK_SECRET)
    monkeypatch.setenv("CONNECTOR_SN_OPS_HOOK", OPS_SECRET)
    monkeypatch.setenv("CONNECTOR_SN_CLIENT_SECRET", "client-secret")


@pytest.fixture
def instance(monkeypatch: pytest.MonkeyPatch) -> FakeServiceNowInstance:
    """The projects' connector reads this instance (under TRANIBOR_FAKES the registry would hand out the
    generic fake tracker, which knows no ServiceNow record)."""
    fake = FakeServiceNowInstance()
    from tranibor_api.db.models import Connector
    from tranibor_api.services import tickets

    async def project_tracker(session: Any, project: Any) -> tuple[Any, Any]:
        from sqlalchemy import select

        row = (
            await session.execute(
                select(Connector).where(Connector.project_id == project.id, Connector.kind == "tracker")
            )
        ).scalar_one_or_none()
        http = httpx.AsyncClient(transport=fake.transport())
        client = ServiceNowClient(
            "https://acme.service-now.com", username=fake.username, password=fake.password, client=http
        )
        return row, ServiceNowTracker(client, assignment_groups=["Service Desk"])

    monkeypatch.setattr(tickets, "project_tracker", project_tracker)
    return fake


async def _project(client: AsyncClient, slug: str, **config: Any) -> dict[str, Any]:
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={"slug": slug, "name": slug, "config": {"slug": slug, "org": "northwind", **config}},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _servicenow(
    client: AsyncClient, project: dict[str, Any], hook: str, *, instance: str = "acme", **extra: Any
) -> None:
    put = await client.put(
        f"/api/v1/projects/{project['id']}/connectors/tracker",
        json={
            "type": "servicenow",
            "config": {
                "instance_url": f"https://{instance}.service-now.com",
                "client_id": "tranibor",
                "assignment_groups": ["Service Desk"],
                **extra,
            },
            "secret_refs": {
                "client_secret": "env:CONNECTOR_SN_CLIENT_SECRET",
                "webhook_secret": f"env:{hook}",
            },
        },
    )
    assert put.status_code == 200, put.text


def rule(
    fake: FakeServiceNowInstance, number: str, *, operation: str = "insert", **extra: Any
) -> dict[str, Any]:
    record = fake.by_number(number)
    return {
        "table": "incident",
        "sys_id": record["sys_id"],
        "number": number,
        "sys_mod_count": int(record["sys_mod_count"]),
        "operation": operation,
        "state": record["state"],
        **extra,
    }


async def _deliver(client: AsyncClient, body: Any, secret: str = DESK_SECRET) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/webhooks/servicenow", json=body, headers={"X-Tranibor-Secret": secret}
    )
    assert response.status_code == 202, response.text
    return dict(response.json())


async def _items(client: AsyncClient, project: dict[str, Any]) -> list[dict[str, Any]]:
    page = (await client.get(f"/api/v1/projects/{project['id']}/work-items")).json()
    return list(page["items"])


INCIDENTS = {"start_rules": [{"item_type": "incident"}]}


async def test_a_delivery_needs_a_connectors_secret_or_the_platforms(
    client: AsyncClient, admin: str, instance: FakeServiceNowInstance
) -> None:
    project = await _project(client, "desk", **INCIDENTS)
    await _servicenow(client, project, "CONNECTOR_SN_DESK_HOOK")
    number = instance.seed("incident", "VPN drops")
    body = rule(instance, number)

    for headers in ({"X-Tranibor-Secret": "wrong"}, {}):
        refused = await client.post("/api/v1/webhooks/servicenow", json=body, headers=headers)
        assert refused.status_code == 401, headers
    assert (await _deliver(client, body, DESK_SECRET))["events"] == 1
    again = rule(instance, number, operation="update", sys_mod_count=1)
    assert (await _deliver(client, again, PLATFORM_SECRET))["accepted"] is True


async def test_an_incident_starts_a_work_item_with_what_the_instance_says(
    client: AsyncClient, admin: str, instance: FakeServiceNowInstance
) -> None:
    project = await _project(client, "desk", **INCIDENTS)
    await _servicenow(client, project, "CONNECTOR_SN_DESK_HOOK")
    number = instance.seed("incident", "VPN drops every hour", caller_id=instance.user("Abel Tuter"))

    ack = await _deliver(client, rule(instance, number, short_description="forged title"))

    assert ack["events"] == 1
    (item,) = await _items(client, project)
    assert item["tracker_key"] == number
    assert item["title"] == "VPN drops every hour", "re-read from the instance, never from the payload"


async def test_a_redelivery_is_not_dispatched_twice(
    client: AsyncClient, admin: str, instance: FakeServiceNowInstance
) -> None:
    project = await _project(client, "desk", **INCIDENTS)
    await _servicenow(client, project, "CONNECTOR_SN_DESK_HOOK")
    number = instance.seed("incident", "Printer")
    await _deliver(client, rule(instance, number))

    again = await _deliver(client, rule(instance, number))

    assert again["duplicate"] is True and again["events"] == 0
    assert [i["tracker_key"] for i in await _items(client, project)] == [number]


async def test_a_connectors_secret_routes_to_its_own_project_only(
    client: AsyncClient, admin: str, instance: FakeServiceNowInstance
) -> None:
    desk = await _project(client, "desk", **INCIDENTS)
    ops = await _project(client, "ops", **INCIDENTS)
    await _servicenow(client, desk, "CONNECTOR_SN_DESK_HOOK")
    await _servicenow(client, ops, "CONNECTOR_SN_OPS_HOOK")
    number = instance.seed("incident", "Disk full")

    await _deliver(client, rule(instance, number), OPS_SECRET)

    assert [i["tracker_key"] for i in await _items(client, ops)] == [number]
    assert await _items(client, desk) == []


async def test_under_the_platforms_secret_the_instance_tells_projects_apart_or_nothing_routes(
    client: AsyncClient, admin: str, instance: FakeServiceNowInstance
) -> None:
    acme = await _project(client, "acme-desk", **INCIDENTS)
    globex = await _project(client, "globex-desk", **INCIDENTS)
    await _servicenow(client, acme, "CONNECTOR_SN_DESK_HOOK", instance="acme")
    await _servicenow(client, globex, "CONNECTOR_SN_OPS_HOOK", instance="globex")
    first = instance.seed("incident", "A")
    second = instance.seed("incident", "B")

    ambiguous = await _deliver(client, rule(instance, first), PLATFORM_SECRET)
    named = await _deliver(client, rule(instance, second, instance="globex"), PLATFORM_SECRET)

    assert ambiguous["events"] == 0, "two projects, nothing to tell them apart: never routed"
    assert named["events"] == 1
    assert [i["tracker_key"] for i in await _items(client, globex)] == [second]
    assert await _items(client, acme) == []


async def test_a_table_the_connector_does_not_serve_is_not_routed(
    client: AsyncClient, admin: str, instance: FakeServiceNowInstance
) -> None:
    project = await _project(client, "desk", **INCIDENTS)
    await _servicenow(client, project, "CONNECTOR_SN_DESK_HOOK", tables=["sc_req_item"])
    number = instance.seed("incident", "Not ours")

    assert (await _deliver(client, rule(instance, number)))["events"] == 0
    assert await _items(client, project) == []


async def test_a_body_the_rule_does_not_post_is_a_400(
    client: AsyncClient, admin: str, instance: FakeServiceNowInstance
) -> None:
    project = await _project(client, "desk", **INCIDENTS)
    await _servicenow(client, project, "CONNECTOR_SN_DESK_HOOK")
    response = await client.post(
        "/api/v1/webhooks/servicenow",
        json={"table": "incident", "sys_id": "nope", "number": "INC0010001", "sys_mod_count": 1},
        headers={"X-Tranibor-Secret": DESK_SECRET},
    )
    assert response.status_code == 400, response.text
