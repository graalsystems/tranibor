"""The fakes honour the Protocols and behave like the real adapters."""

from __future__ import annotations

import json

import pytest
from tranibor_adapters import (
    AdapterSet,
    Executor,
    GatewayAdapter,
    MemoryAdapter,
    Notifier,
    TrackerAdapter,
)
from tranibor_adapters.fakes import BudgetExceeded
from tranibor_contracts import Size, StageResult, StageStatus
from tranibor_core.domain import Fact, Message, NewItem, Provenance, StageJobSpec, TrackerStateMapping


def test_adapter_set_satisfies_protocols() -> None:
    adapters = AdapterSet.fakes()
    assert isinstance(adapters.tracker, TrackerAdapter)
    assert isinstance(adapters.executor, Executor)
    assert isinstance(adapters.memory, MemoryAdapter)
    assert isinstance(adapters.gateway, GatewayAdapter)
    assert isinstance(adapters.notify, Notifier)


async def test_tracker_lifecycle() -> None:
    tracker = AdapterSet.fakes().tracker
    key = tracker.seed("Fix the credit notes", "The total ignores the credit note", size=Size.M)  # type: ignore[attr-defined]
    item = await tracker.fetch_item(key)
    assert item.title == "Fix the credit notes"

    await tracker.set_state(key, TrackerStateMapping(status="In progress", label="choregos:in_progress"))
    item = await tracker.fetch_item(key)
    assert item.state == "In progress"
    assert "choregos:in_progress" in item.labels

    marker = "<!-- choregos:status -->"
    await tracker.upsert_status_comment(key, "v1", marker)
    await tracker.upsert_status_comment(key, "v2", marker)
    item = await tracker.fetch_item(key)
    assert len([c for c in item.comments if c.marker == marker]) == 1, "a single status comment"
    assert tracker.status_comment(key) == "v2"  # type: ignore[attr-defined]

    await tracker.set_fields(key, {"Cost (€)": 6.77, "Size": "M"})
    assert (await tracker.fetch_item(key)).fields["Cost (€)"] == 6.77

    child = await tracker.create_item(NewItem(title="Finding: N+1", body="…", labels=["finding"]))
    await tracker.link(child, key, "origin")
    assert (child, key, "origin") in tracker.links  # type: ignore[attr-defined]


async def test_tracker_webhook_and_move() -> None:
    tracker = AdapterSet.fakes().tracker
    key = tracker.seed("T")  # type: ignore[attr-defined]
    event = tracker.move(key, "Ready")  # type: ignore[attr-defined]
    assert event.type == "tracker.item.moved"
    assert event.payload["to_status"] == "Ready"
    body = json.dumps(event.model_dump(mode="json")).encode()
    assert tracker.verify_webhook({"X-Fake-Secret": "fake-secret"}, body)
    assert not tracker.verify_webhook({}, body)
    assert tracker.parse_webhook({}, body)[0].work_item_key == key


async def test_gateway_budget_is_a_hard_cap() -> None:
    gateway = AdapterSet.fakes().gateway
    key = await gateway.mint_key({"run_id": "r1"}, budget_usd=0.05, ttl_s=600, models=["platform/standard"])
    gateway.record_usage(key.key_id, tokens_in=1000, tokens_out=100)  # type: ignore[attr-defined]
    spend = await gateway.spend(key.key_id)
    assert spend.cost_usd == pytest.approx(0.003 + 0.0015)
    assert spend.requests == 1
    with pytest.raises(BudgetExceeded):
        gateway.record_usage(key.key_id, tokens_in=100_000, tokens_out=10_000)  # type: ignore[attr-defined]
    await gateway.revoke(key.key_id)
    assert key.key_id in gateway.revoked  # type: ignore[attr-defined]
    assert len(await gateway.list_models()) >= 3


async def test_executor_is_idempotent_by_run_id() -> None:
    executor = AdapterSet.fakes().executor
    executor.queue_result(StageResult(status=StageStatus.DONE, summary="ok"))  # type: ignore[attr-defined]
    spec = StageJobSpec(
        run_id="run-1",
        project_slug="demo",
        namespace="proj-demo-runners",
        runner_image="img",
        api_url="http://api/internal",
        run_token="tok",
    )
    first = await executor.start(spec)
    second = await executor.start(spec)
    assert first.run_id == second.run_id
    assert len(executor.started) == 1, "a single real start for one run_id"  # type: ignore[attr-defined]
    assert (await executor.status(first)).state == "succeeded"
    await executor.cancel(first)
    assert (await executor.status(first)).state == "cancelled"
    lines = [line async for line in executor.logs(first)]
    assert lines


async def test_memory_context_pack_respects_budget() -> None:
    memory = AdapterSet.fakes().memory
    memory.seed("demo", "decision", "decision:billing:rounding", "Totals are rounded at issue." * 20)  # type: ignore[attr-defined]
    memory.seed("demo", "incident", "incident:billing:2026-06", "Rollback on the credit notes." * 20)  # type: ignore[attr-defined]
    pack = await memory.context_pack("demo", "credit notes rounding totals", [], budget_tokens=200)
    assert pack.tokens_estimated <= 200
    assert pack.truncated or pack.memories or pack.incidents


async def test_memory_supersession_and_pending() -> None:
    memory = AdapterSet.fakes().memory
    await memory.write_fact("demo", Fact(kind="decision", subject="decision:x", content="v1"))
    await memory.write_fact("demo", Fact(kind="decision", subject="decision:x", content="v2"))
    actives = [m for m in memory.facts["demo"] if m.status == "active"]  # type: ignore[attr-defined]
    assert len(actives) == 1 and actives[0].content == "v2"

    pending_id = await memory.propose_fact(
        "demo",
        Fact(kind="convention", subject="convention:y", content="proposed"),
        Provenance(source="agent", run_id="run-1"),
    )
    assert memory.pending["demo"][0].id == pending_id  # type: ignore[attr-defined]
    assert await memory.accept_pending("demo", pending_id)  # type: ignore[attr-defined]
    assert any(m.subject == "convention:y" for m in await memory.search("demo", "convention", 5))


async def test_memory_ingest_is_idempotent() -> None:
    memory = AdapterSet.fakes().memory
    event = {"external_id": "gh:123", "kind": "ticket_summary", "subject": "ticket:123", "content": "summary"}
    await memory.ingest_events("demo", [event, event])
    assert len(memory.facts["demo"]) == 1  # type: ignore[attr-defined]
    await memory.ingest_events("demo", [{**event, "content": "summary v2"}])
    assert len([m for m in memory.facts["demo"] if m.status == "active"]) == 1  # type: ignore[attr-defined]


async def test_memory_failure_returns_empty_pack() -> None:
    memory = AdapterSet.fakes().memory
    memory.seed("demo", "decision", "d", "content")  # type: ignore[attr-defined]
    memory.fail = True  # type: ignore[attr-defined]
    pack = await memory.context_pack("demo", "whatever", [], 1000)
    assert pack.is_empty(), "a memory outage must never block a stage"


async def test_notifier() -> None:
    notifier = AdapterSet.fakes().notify
    await notifier.send("#choregos", Message(title="Approval required", severity="warning"))
    assert notifier.last().title == "Approval required"  # type: ignore[union-attr]
