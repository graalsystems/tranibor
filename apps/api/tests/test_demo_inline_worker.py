"""The inline demo worker (ADR 0212): the API alone, under the fakes, runs the workflow it starts.

A work item is created, the simulated agent takes it to a human gate, a person decides through the API,
and the item moves on, with no worker process and no Temporal server of the machine's. The mode never
activates implicitly and is refused outside development under the fakes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import AsyncClient

WORKFLOW = """
apiVersion: choregos/v1
kind: Workflow
metadata: { name: demo-release, version: 1 }
actors:
  analyst: { type: agent, role: triage }
  captain: { type: human, group: captains }
  builder: { type: agent, role: implement }
states:
  inbox: { display: To analyse, kind: wait }
  to_approve: { display: Waiting for the captain, kind: wait }
  building: { display: Building, kind: wait }
  shipped: { display: Shipped, terminal: true }
transitions:
  - { id: t-analyse, from: inbox, to: to_approve, by: analyst }
  - { id: t-approve, from: to_approve, to: building, by: captain }
  - { id: t-build, from: building, to: shipped, by: builder }
"""

BOUND_S = 60.0


async def _until(client: AsyncClient, item_id: str, wanted: str) -> dict[str, Any]:
    """The work item, once it is in `wanted`, or the failure naming where it stood."""
    deadline = time.monotonic() + BOUND_S
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = (await client.get(f"/api/v1/work-items/{item_id}")).json()
        if body.get("state") == wanted:
            return body
        await asyncio.sleep(0.2)
    raise AssertionError(f"the work item never reached `{wanted}` within {BOUND_S}s; it stood in {body}")


@pytest.fixture
async def inline_app(app: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """The application with its lifespan running, and the opt-in on."""
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_DEMO_INLINE_WORKER", "1")
    reset_settings_cache()
    async with app.router.lifespan_context(app):
        yield app
    reset_settings_cache()


async def test_a_decision_advances_the_work_item_with_the_api_alone(
    inline_app: Any, client: AsyncClient, org: str, admin: str
) -> None:
    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={
            "slug": "demo",
            "name": "Demo",
            "config": {
                "slug": "demo",
                "org": "northwind",
                "repo": {"url": "https://github.com/northwind/demo.git", "default_branch": "main"},
            },
        },
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    put = await client.put(f"/api/v1/projects/{pid}/workflow", json={"yaml": WORKFLOW})
    assert put.status_code == 200, put.text
    item = await client.post(f"/api/v1/projects/{pid}/work-items", json={"title": "Ship 1.2"})
    assert item.status_code == 201, item.text
    item_id = str(item.json()["id"])

    # The simulated agent moves the item to the human gate on its own...
    await _until(client, item_id, "to_approve")

    # ...where it stays: nothing but a person moves it on.
    await asyncio.sleep(1.0)
    assert (await client.get(f"/api/v1/work-items/{item_id}")).json()["state"] == "to_approve"

    # The decision is accepted and the workflow carries on to its end.
    deadline = time.monotonic() + BOUND_S
    while True:
        decided = await client.post(f"/api/v1/work-items/{item_id}/decisions", json={"kind": "approve"})
        if decided.status_code != 409 or time.monotonic() > deadline:  # 409: the request is not raised yet
            break
        await asyncio.sleep(0.2)
    assert decided.status_code == 202, decided.text
    done = await _until(client, item_id, "shipped")
    assert done["state"] == "shipped"


async def test_without_the_opt_in_nothing_runs_inline(app: Any, client: AsyncClient) -> None:
    """Never implicit: the fakes alone leave the in-memory `FakeTemporal`, which runs nothing."""
    from tranibor_api.config import get_settings
    from tranibor_api.temporal import FakeTemporal, get_temporal

    assert get_settings().demo_inline_worker is False
    async with app.router.lifespan_context(app):
        assert isinstance(get_temporal(), FakeTemporal)


@pytest.mark.parametrize(
    ("env", "fakes", "named"),
    [
        ("prod", "0", "TRANIBOR_ENV=prod"),
        ("staging", "0", "TRANIBOR_ENV=staging"),
        ("prod", "1", "TRANIBOR_FAKES"),  # the older refusal of the fakes in production fires first
        ("dev", "0", "TRANIBOR_FAKES is off"),
    ],
)
async def test_the_mode_is_refused_outside_development_under_the_fakes(
    app: Any, monkeypatch: pytest.MonkeyPatch, env: str, fakes: str, named: str
) -> None:
    from tranibor_api.config import reset_settings_cache
    from tranibor_core.production import DemoInlineWorkerRefusedError, FakesInProductionError

    monkeypatch.setenv("TRANIBOR_DEMO_INLINE_WORKER", "1")
    monkeypatch.setenv("TRANIBOR_ENV", env)
    monkeypatch.setenv("TRANIBOR_FAKES", fakes)
    monkeypatch.setenv("TRANIBOR_DEV_LOGIN_ENABLED", "false")  # production refuses the development sign-in
    reset_settings_cache()
    with pytest.raises((DemoInlineWorkerRefusedError, FakesInProductionError), match=named):
        async with app.router.lifespan_context(app):
            pytest.fail("the API started with the inline demo worker where it is refused")
    reset_settings_cache()


def test_the_refusal_names_the_setting_to_change() -> None:
    from tranibor_core.production import DemoInlineWorkerRefusedError, refuse_demo_inline_worker

    refuse_demo_inline_worker(environment="dev", fakes=True)
    refuse_demo_inline_worker(environment="test", fakes="1")
    with pytest.raises(DemoInlineWorkerRefusedError, match="TRANIBOR_ENV=dev"):
        refuse_demo_inline_worker(environment="prod", fakes=True)
    with pytest.raises(DemoInlineWorkerRefusedError, match="TRANIBOR_FAKES=1"):
        refuse_demo_inline_worker(environment="dev", fakes=False)
