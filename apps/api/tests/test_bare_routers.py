"""The routers nothing exercised: work items, runs, costs, webhooks, templates, platform.

Third slice of P1-3 (coverage 78% -> 80%). Each test talks to the API over HTTP, with the fakes;
what needs a row in the database (a run, a spend) inserts it through the model, as the orchestrator
would. A run's diff, the release train, the Tekton and Argo CD webhooks are the `software-delivery`
plugin's routes since round D of ADR 0181: their tests are `plugins/software-delivery/tests/api/
test_delivery_routers.py`.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from tranibor_core.domain import utcnow

from .conftest import login


async def _internal(client: AsyncClient, project: dict[str, Any]) -> dict[str, Any]:
    """The test project receives its requests IN Tranibor, and a first one is placed."""
    put = await client.put(
        f"/api/v1/projects/{project['id']}/connectors/tracker", json={"type": "internal", "config": {}}
    )
    assert put.status_code in {200, 201}, put.text
    created = await client.post(
        f"/api/v1/projects/{project['id']}/work-items",
        json={"title": "A project manager", "body": "6 months", "size": "M"},
    )
    assert created.status_code == 201, created.text
    return dict(created.json())


async def _run(project: dict[str, Any], item: dict[str, Any], **fields: Any) -> str:
    from tranibor_api.db.models import Run
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        run = Run(
            work_item_id=item["id"],
            project_id=project["id"],
            stage_role="implement",
            attempt=1,
            status=fields.pop("status", "succeeded"),
            backend="claude-code",
            model="platform/standard",
            **fields,
        )
        session.add(run)
        await session.flush()
        return str(run.id)


# ───────────────────────── work items ─────────────────────────


async def test_a_work_item_is_listed_read_and_tells_its_timeline(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    item = await _internal(client, project)
    page = (await client.get(f"/api/v1/projects/{project['id']}/work-items")).json()
    assert [i["tracker_key"] for i in page["items"]] == ["BILLING-API-1"]
    read_back = (await client.get(f"/api/v1/work-items/{item['id']}")).json()
    assert (
        read_back["title"] == "A project manager" and read_back["size"] == "M" and read_back["temporal_wf_id"]
    )
    timeline = await client.get(f"/api/v1/work-items/{item['id']}/timeline")
    assert timeline.status_code == 200 and isinstance(timeline.json()["items"], list)
    assert (await client.get("/api/v1/work-items/absent")).status_code == 404


async def test_a_dead_workflow_shows_on_the_work_item(client: AsyncClient, project: dict[str, Any]) -> None:
    from tranibor_api.temporal import WorkflowState, get_temporal

    item = await _internal(client, project)
    get_temporal().described[item["temporal_wf_id"]] = WorkflowState(status="FAILED")
    read_back = (await client.get(f"/api/v1/work-items/{item['id']}")).json()
    assert read_back["workflow_status"] == "FAILED"


async def test_the_control_actions_go_to_the_workflow_and_are_audited(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.temporal import get_temporal

    item = await _internal(client, project)
    for action, paused in (("pause", True), ("resume", False), ("stop", False)):
        response = await client.post(
            f"/api/v1/work-items/{item['id']}/actions", json={"action": action, "reason": "test"}
        )
        assert response.status_code == 202, response.text
        assert (await client.get(f"/api/v1/work-items/{item['id']}")).json()["paused"] is paused
    assert len(get_temporal().signals) >= 3
    audit = (await client.get("/api/v1/audit", params={"target_type": "work_item"})).json()
    assert {e["action"] for e in audit["items"]} >= {"workitem.pause", "workitem.resume", "workitem.stop"}
    assert (
        await client.post(f"/api/v1/work-items/{item['id']}/actions", json={"action": "mark_agent_ready"})
    ).status_code == 202


async def test_a_decision_settles_the_pending_human_request(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.db.models import HumanRequest
    from tranibor_api.db.session import session_scope

    item = await _internal(client, project)
    nothing_pending = await client.post(
        f"/api/v1/work-items/{item['id']}/decisions", json={"kind": "approve"}
    )
    assert nothing_pending.status_code == 409, "no request pending: nothing to settle"
    async with session_scope(orgs="*") as session:
        session.add(
            HumanRequest(
                work_item_id=item["id"],
                project_id=project["id"],
                kind="question",
                payload={"question": "in euros?"},
                requested_at=utcnow(),
            )
        )
    response = await client.post(
        f"/api/v1/work-items/{item['id']}/decisions", json={"kind": "answer", "answer": "yes, in euros"}
    )
    assert response.status_code == 202, response.text
    assert response.json()["decided_by"] == "admin@northwind.dev"
    again = await client.post(f"/api/v1/work-items/{item['id']}/decisions", json={"kind": "approve"})
    assert again.status_code == 409, "already settled"


# ───────────────────────── runs ─────────────────────────


async def test_a_run_is_read_with_its_events_its_transcript_and_its_accesses(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.db.models import CostLedger, RunEvent
    from tranibor_api.db.session import session_scope

    item = await _internal(client, project)
    run_id = await _run(
        project,
        item,
        allowed_paths=["src/**"],
        transcript_url="s3://choregos/runs/r/transcript.ndjson",
        result={
            "schema": "choregos/StageResult/v1",
            "status": "done",
            "summary": "ok",
            "artifacts": {
                "reports": {
                    "diff_files": json.dumps(
                        [
                            {"path": "src/a.py", "additions": 3, "deletions": 1},
                            {"path": "infra/main.tf", "status": "added"},
                        ]
                    )
                }
            },
        },
    )
    async with session_scope(orgs="*") as session:
        for seq, (type_, payload) in enumerate(
            [
                (
                    "session/request_permission",
                    {
                        "kind": "write",
                        "target": "/workspace/.env",
                        "allowed": False,
                        "reason": "sensitive file",
                    },
                ),
                (
                    "session/request_permission",
                    {"kind": "read", "target": "/workspace/src/a.py", "allowed": True},
                ),
                ("run.result", {"status": "done"}),
            ],
            start=1,
        ):
            session.add(RunEvent(run_id=run_id, seq=seq, type=type_, payload=payload, ts=utcnow()))
        session.add(
            CostLedger(
                project_id=project["id"],
                work_item_id=item["id"],
                run_id=run_id,
                kind="tool",
                provider="address",
                model="verify_address",
                cost_eur=0.002,
                ts=utcnow(),
            )
        )

    runs = (await client.get(f"/api/v1/work-items/{item['id']}/runs")).json()["items"]
    assert [r["id"] for r in runs] == [run_id]
    assert (await client.get(f"/api/v1/runs/{run_id}")).json()["stage_role"] == "implement"
    assert (await client.get("/api/v1/runs/absent")).status_code == 404

    events = (await client.get(f"/api/v1/runs/{run_id}/events")).json()["items"]
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert [
        e["seq"]
        for e in (await client.get(f"/api/v1/runs/{run_id}/events", params={"after_seq": 2})).json()["items"]
    ] == [3]

    access_report = (await client.get(f"/api/v1/runs/{run_id}/access")).json()
    assert access_report["refusals"] == 1 and access_report["tool_cost_eur"] == 0.002
    assert any(a["nature"] == "tool" for a in access_report["access"]), (
        "the catalogue call counts as an access"
    )

    assert (await client.get(f"/api/v1/runs/{run_id}/transcript")).json()[
        "content_type"
    ] == "application/x-ndjson"


async def test_a_run_without_a_transcript_answers_404(client: AsyncClient, project: dict[str, Any]) -> None:
    item = await _internal(client, project)
    run_id = await _run(project, item)
    assert (await client.get(f"/api/v1/runs/{run_id}/transcript")).status_code == 404


# ───────────────────────── costs ─────────────────────────


async def _spends(client: AsyncClient, project: dict[str, Any]) -> dict[str, Any]:
    from tranibor_api.db.models import CostLedger
    from tranibor_api.db.session import session_scope

    item = await _internal(client, project)
    run_id = await _run(project, item)
    async with session_scope(orgs="*") as session:
        now = utcnow()
        session.add(
            CostLedger(
                project_id=project["id"],
                work_item_id=item["id"],
                run_id=run_id,
                kind="model",
                provider="anthropic",
                model="claude",
                stage_role="implement",
                tokens_in=1000,
                tokens_out=200,
                cost_usd=0.5,
                cost_eur=0.46,
                ts=now,
            )
        )
        session.add(
            CostLedger(
                project_id=project["id"],
                work_item_id=item["id"],
                run_id=run_id,
                kind="model",
                provider="anthropic",
                model="claude",
                stage_role="verify",
                tokens_in=100,
                cost_usd=0.1,
                cost_eur=0.092,
                ts=now - timedelta(days=1),
            )
        )
        session.add(
            CostLedger(
                project_id=project["id"],
                work_item_id=item["id"],
                run_id=run_id,
                kind="tool",
                provider="address",
                model="verify_address",
                cost_usd=0.01,
                cost_eur=0.0092,
                ts=now,
            )
        )
        session.add(
            CostLedger(
                project_id=project["id"],
                kind="model",
                model="old",
                cost_usd=99,
                cost_eur=99,
                ts=now - timedelta(days=60),
            )
        )
    return item


async def test_the_cost_report_groups_and_bounds_its_window(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await _spends(client, project)
    by_kind = (
        await client.get(f"/api/v1/projects/{project['id']}/costs", params={"group_by": "kind"})
    ).json()
    assert {r["key"]: r["runs"] for r in by_kind["rows"]} == {"model": 2, "tool": 1}
    assert by_kind["total"]["cost_usd"] == pytest.approx(0.61), (
        "the spend of two months ago is out of the window"
    )
    by_stage = (
        await client.get(f"/api/v1/projects/{project['id']}/costs", params={"group_by": "stage"})
    ).json()
    assert {r["key"] for r in by_stage["rows"]} >= {"implement", "verify"}
    by_day = (await client.get(f"/api/v1/projects/{project['id']}/costs")).json()
    assert by_day["group_by"] == "day" and len(by_day["rows"]) == 2
    assert (
        await client.get(f"/api/v1/projects/{project['id']}/costs", params={"group_by": "planet"})
    ).status_code == 422
    since = utcnow().date().isoformat()
    assert (
        len(
            (await client.get(f"/api/v1/projects/{project['id']}/costs", params={"since": since})).json()[
                "rows"
            ]
        )
        == 1
    )


async def test_the_csv_carries_the_total_the_budget_and_the_bom_for_excel(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await _spends(client, project)
    response = await client.get(f"/api/v1/projects/{project['id']}/costs.csv", params={"group_by": "kind"})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    assert 'filename="couts-billing-api-kind-' in response.headers["content-disposition"]
    text = response.text
    assert text.startswith("\ufeff"), "the BOM, so that Excel reads the accents"
    header = text.lstrip("\ufeff").splitlines()[0]
    assert header == "kind;cout_usd;cout_eur;tokens_in;tokens_out;tokens_caches;runs;tokens_cache_write"
    assert any(line.startswith("total;") for line in text.splitlines())
    assert any(line.startswith("budget_quotidien_usd;") for line in text.splitlines())


async def test_the_costs_of_an_organisation_read_per_project(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await _spends(client, project)
    report = (await client.get("/api/v1/orgs/northwind/costs", params={"group_by": "project"})).json()
    assert [r["key"] for r in report["rows"]] == ["billing-api"], "the identifier is translated to a slug"
    assert (await client.get("/api/v1/orgs/unknown/costs")).status_code == 404


# ───────────────────────── webhooks ─────────────────────────


async def test_alertmanager_requires_the_shared_secret_then_signals(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core's half of the former Argo CD and Alertmanager test: the Argo CD webhook is the
    `software-delivery` plugin's since round D of ADR 0181 (`plugins/software-delivery/tests/api/`)."""
    from tranibor_api.config import get_settings
    from tranibor_api.temporal import get_temporal

    monkeypatch.setattr(get_settings(), "generic_webhook_secret", "s3cret")
    refusal = await client.post("/api/v1/webhooks/alertmanager", json={"alerts": []})
    assert refusal.status_code == 401
    alerts = {
        "alerts": [
            {
                "status": "firing",
                "fingerprint": "f-1",
                "labels": {"alertname": "ErrorBudget", "severity": "page", "project": "billing-api"},
            },
            {"status": "resolved", "fingerprint": "f-2", "labels": {"project": "unknown"}},
        ]
    }
    response = await client.post(
        "/api/v1/webhooks/alertmanager", json=alerts, headers={"X-Choregos-Secret": "s3cret"}
    )
    assert response.status_code == 202 and response.json()["events"] == 2
    assert any(wf == "mem-billing-api" and name == "alert" for wf, name, _ in get_temporal().signals)
    assert (
        await client.post(
            "/api/v1/webhooks/alertmanager", json=alerts, headers={"X-Choregos-Secret": "s3cret"}
        )
    ).json()["events"] == 0


async def test_github_without_a_secret_is_tolerated_outside_production(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    response = await client.post(
        "/api/v1/webhooks/github",
        json={"zen": "ping"},
        headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": "p-1"},
    )
    assert response.status_code == 202 and response.json()["events"] == 0


# ───────────────────────── templates ─────────────────────────


async def test_the_repository_templates_are_listed_and_read(client: AsyncClient, admin: str) -> None:
    listing = (await client.get("/api/v1/templates")).json()
    assert listing, "the repository ships at least one template"
    first = listing[0]
    detail = (await client.get(f"/api/v1/templates/{first['name']}")).json()
    assert detail["manifest"]["metadata"]["name"] == first["name"]
    assert (await client.get("/api/v1/templates/nonexistent")).status_code == 404


async def test_each_shipped_template_passes_the_manifest_schema(client: AsyncClient, admin: str) -> None:
    """A template read from the repository is served without validation; the same one, sent through
    the API, was refused: `github-aca` declared `requires.azure_capabilities`, which the schema did
    not know (found by this slice of tests). The schema now carries it, and this test keeps a
    shipped template from drifting from the contract."""
    for summary in (await client.get("/api/v1/templates")).json():
        manifest = (await client.get(f"/api/v1/templates/{summary['name']}")).json()["manifest"]
        manifest["metadata"] = {**manifest["metadata"], "name": f"copy-{summary['name']}"}
        response = await client.post("/api/v1/templates", json={"manifest": manifest})
        assert response.status_code in {200, 201}, f"{summary['name']} : {response.text}"


async def test_a_template_is_created_and_updated_by_an_administrator(client: AsyncClient, admin: str) -> None:
    # A valid manifest is that of a shipped template, renamed: the schema requires
    # `requires`, `defaults`, `inputs` and `steps`, and a test that invents them would age badly.
    shipped = (await client.get("/api/v1/templates")).json()[0]
    manifest = dict((await client.get(f"/api/v1/templates/{shipped['name']}")).json()["manifest"])
    manifest["metadata"] = {
        **manifest["metadata"],
        "name": "house",
        "version": "1.0.0",
        "display": "House",
    }
    created = await client.post("/api/v1/templates", json={"manifest": manifest, "is_published": False})
    assert created.status_code in {200, 201}, created.text
    assert created.json()["name"] == "house" and created.json()["is_published"] is False
    manifest["metadata"] = {**manifest["metadata"], "version": "1.1.0"}
    updated = await client.put("/api/v1/templates/house", json={"manifest": manifest, "is_published": True})
    assert (
        updated.status_code == 200
        and updated.json()["version"] == "1.1.0"
        and updated.json()["is_published"] is True
    )
    assert any(t["name"] == "house" for t in (await client.get("/api/v1/templates")).json())
    invalid = await client.post("/api/v1/templates", json={"manifest": {"kind": "Other"}})
    assert invalid.status_code == 422


# ───────────────────────── platform ─────────────────────────


async def test_backends_and_executors_are_listed_and_set(client: AsyncClient, admin: str) -> None:
    backends = (await client.get("/api/v1/platform/backends")).json()
    assert {b["name"] for b in backends} >= {"claude-code", "codex"} and next(
        b for b in backends if b["name"] == "claude-code"
    )["enabled"]
    switched_off = await client.put(
        "/api/v1/platform/backends",
        json={"name": "codex", "enabled": False, "disabled_reason": "compliance KO"},
    )
    assert switched_off.status_code == 200 and switched_off.json()["enabled"] is False
    assert (
        next(b for b in (await client.get("/api/v1/platform/backends")).json() if b["name"] == "codex")[
            "disabled_reason"
        ]
        == "compliance KO"
    )
    executors = (await client.get("/api/v1/platform/executors")).json()
    assert {e["kind"] for e in executors} == {"tekton", "k8s_job", "local_docker", "aca", "agent_sandbox"}
    # S42-07b: the executors are the deployment's answer; there is no PUT to store one any more
    for method in ("PUT", "POST", "PATCH", "DELETE"):
        gone = await client.request(
            method, "/api/v1/platform/executors", json={"kind": "aca", "enabled": True}
        )
        assert gone.status_code == 405, method
    assert (await client.get("/api/v1/platform/gateway/keys")).json()["items"] == []


async def test_the_platform_settings_are_reserved_to_administrators(client: AsyncClient, org: str) -> None:
    await login(client, "dev@northwind.dev")
    assert (
        await client.put("/api/v1/platform/backends", json={"name": "codex", "enabled": False})
    ).status_code == 403
    assert (await client.get("/api/v1/platform/gateway/keys")).status_code == 403
    assert (await client.get("/api/v1/platform/backends")).status_code == 200, "reading stays open"


# ───────────── the full timeline, provisioning, the internal API, the CLI ─────────────


async def test_the_timeline_tells_runs_decisions_states_and_findings(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.db.models import Event, Finding, HumanRequest
    from tranibor_api.db.session import session_scope

    item = await _internal(client, project)
    run_id = await _run(project, item, result={"summary": "three profiles"}, cost_usd=0.4)
    async with session_scope(orgs="*") as session:
        session.add(
            HumanRequest(
                work_item_id=item["id"],
                project_id=project["id"],
                kind="approval",
                payload={"summary": "validate?"},
                requested_at=utcnow(),
                decided_at=utcnow(),
                decided_by="marie@northwind.dev",
                decision={"kind": "approve", "reason": "ok"},
            )
        )
        session.add(
            Event(
                project_id=project["id"],
                work_item_id=item["id"],
                type="choregos.workitem.state_changed",
                subject="BILLING-API-1",
                payload={"from": "request", "to": "sourcing", "by": "orchestrator"},
                ts=utcnow(),
            )
        )
        session.add(
            Finding(
                project_id=project["id"],
                origin_work_item_id=item["id"],
                origin_run_id=run_id,
                title="Profile base missing",
                type="docs",
                severity="medium",
                evidence="no base",
                status="pending",
            )
        )
    timeline = (await client.get(f"/api/v1/work-items/{item['id']}/timeline")).json()["items"]
    kinds_seen = [e["kind"] for e in timeline]
    assert {"run", "decision", "state_change", "finding"} <= set(kinds_seen)
    assert kinds_seen.count("decision") == 2, "the request and the decision are two entries"
    run_entry = next(e for e in timeline if e["kind"] == "run")
    assert run_entry["detail"] == "three profiles" and run_entry["cost_usd"] == 0.4
    assert next(e for e in timeline if e["kind"] == "state_change")["title"] == "request → sourcing"


async def test_provisioning_starts_a_workflow_and_exposes_its_state(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.temporal import get_temporal

    dry_run = await client.post(
        f"/api/v1/projects/{project['id']}/provision", json={"dry_run": True, "inputs": {"env": "dev"}}
    )
    assert dry_run.status_code == 202 and dry_run.json()["status"] == "running"
    assert not get_temporal().started, "a dry run starts nothing"
    real = await client.post(f"/api/v1/projects/{project['id']}/provision", json={"inputs": {"region": "eu"}})
    assert real.status_code == 202
    ((workflow_id, payload),) = get_temporal().started.items()
    assert workflow_id == real.json()["workflow_id"] and payload["inputs"] == {
        "env": "dev",
        "region": "eu",
    }, "the inputs accumulate from one call to the next"
    state = (await client.get(f"/api/v1/projects/{project['id']}/provision")).json()
    assert state["status"] == "running" and state["workflow_id"] == workflow_id
    assert (await client.get(f"/api/v1/projects/{project['id']}")).json()["status"] == "provisioning"


async def test_a_project_without_template_or_repository_is_activated_without_a_workflow(
    client: AsyncClient, admin: str
) -> None:
    from tranibor_api.temporal import get_temporal

    created = await client.post(
        "/api/v1/orgs/northwind/projects",
        json={"slug": "hr-desk", "name": "HR desk", "config": {"slug": "hr-desk", "org": "northwind"}},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["status"] == "draft"
    assert project["workflow_name"] == "default-request", "no repository: a workflow that delivers no code"
    readiness = (await client.get(f"/api/v1/projects/{project['id']}/readiness")).json()
    ids = {item["id"] for item in readiness["items"]}
    assert not ids & {"repository", "connector-scm", "connector-cd"}, ids
    dry = await client.post(f"/api/v1/projects/{project['id']}/provision", json={"dry_run": True})
    assert dry.status_code == 202 and dry.json()["status"] == "pending"
    assert (await client.get(f"/api/v1/projects/{project['id']}")).json()["status"] == "draft"
    done = await client.post(f"/api/v1/projects/{project['id']}/provision", json={})
    assert done.status_code == 202 and done.json()["status"] == "succeeded"
    assert list(get_temporal().started) == ["reconcile-hr-desk", "mem-hr-desk"], (
        "no GitHub · Tekton · Argo steps for a project with no repository; only its tracker catch-up "
        "and its memory loop"
    )
    assert (await client.get(f"/api/v1/projects/{project['id']}")).json()["status"] == "active"


async def test_the_internal_api_serves_the_agent_with_its_run_token(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.config import get_settings
    from tranibor_api.security import mint_run_token
    from tranibor_api.temporal import get_temporal

    item = await _internal(client, project)
    run_id = await _run(
        project,
        item,
        status="running",
        context_pack={
            "query": "credit notes",
            "paths": [],
            "kinds": [],
            "budget_tokens": 100,
            "tokens_estimated": 0,
            "truncated": False,
            "memories": [],
            "incidents": [],
            "related_items": [],
            "generated_at": "2026-09-25T00:00:00Z",
        },
    )
    run_token = mint_run_token(
        run_id,
        project_slug="billing-api",
        work_item_key="BILLING-API-1",
        ttl_minutes=5,
        settings=get_settings(),
    )
    headers = {"Authorization": f"Bearer {run_token}"}
    client.cookies.clear()  # the agent has no session: only the run token authenticates it

    fetched = await client.get(f"/api/v1/internal/runs/{run_id}/ticket", headers=headers)
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["key"] == "BILLING-API-1" and fetched.json()["body"] == "6 months"
    assert (await client.get(f"/api/v1/internal/runs/{run_id}/context", headers=headers)).json()[
        "query"
    ] == "credit notes"

    batch = {
        "events": [
            {"seq": 1, "type": "session/update", "payload": {"text": "hello"}},
            {"seq": 2, "type": "session/update", "payload": {}},
        ]
    }
    assert (
        await client.post(f"/api/v1/internal/runs/{run_id}/events", json=batch, headers=headers)
    ).json() == {"accepted": 2}
    assert (
        await client.post(f"/api/v1/internal/runs/{run_id}/events", json=batch, headers=headers)
    ).json() == {"accepted": 0}, "replaying the batch writes nothing"

    question = await client.post(
        f"/api/v1/internal/runs/{run_id}/question",
        json={"text": "in euros?", "options": ["yes", "no"]},
        headers=headers,
    )
    assert question.status_code == 202 and question.json()["request_id"]
    assert any(name == "question_asked" for _, name, _ in get_temporal().signals)

    anonymous = await client.get(f"/api/v1/internal/runs/{run_id}/ticket")
    assert anonymous.status_code == 401
    other = mint_run_token(
        "other-run",
        project_slug="billing-api",
        work_item_key="BILLING-API-1",
        ttl_minutes=5,
        settings=get_settings(),
    )
    assert (
        await client.get(
            f"/api/v1/internal/runs/{run_id}/ticket", headers={"Authorization": f"Bearer {other}"}
        )
    ).status_code in {403, 404}, "a token holds only for its run"


async def test_choregos_admin_mints_a_token_and_replays_the_bootstrap(
    app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from tranibor_api.admin_cli import app as cli
    from tranibor_api.config import get_settings
    from tranibor_api.db import session as db_session
    from typer.testing import CliRunner

    runner = CliRunner()

    async def invoke(*args: str) -> Any:
        # The CLI calls `asyncio.run()`: in a separate thread, with a fresh engine; the one of the
        # test is bound to pytest's loop, and an engine is not shared between loops.
        db_session._engine = None
        db_session._sessionmaker = None
        return await asyncio.to_thread(runner.invoke, cli, list(args))

    minted = await invoke(
        "tokens", "create", "--email", "Ops@Northwind.dev", "--name", "ci", "--expires-in-days", "7"
    )
    assert minted.exit_code == 0, minted.output
    token = minted.output.strip()
    assert token and len(token) > 20, "the clear text is shown once"
    monkeypatch.setattr(get_settings(), "bootstrap_org", "acme")
    monkeypatch.setattr(get_settings(), "bootstrap_org_name", "ACME")
    monkeypatch.setattr(get_settings(), "bootstrap_admins", "ops@northwind.dev")
    bootstrapped = await invoke("orgs", "bootstrap")
    assert bootstrapped.exit_code == 0, bootstrapped.output
    assert "acme" in bootstrapped.output and "ops@northwind.dev" in bootstrapped.output
    again = await invoke("orgs", "bootstrap")
    assert "none" in again.output, "idempotent: the second time creates nothing"
    assert (await invoke()).exit_code != 0, "without a command, the help"
    db_session._engine = None
    db_session._sessionmaker = None
