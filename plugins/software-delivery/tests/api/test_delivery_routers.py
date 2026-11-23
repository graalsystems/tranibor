# SPDX-License-Identifier: Apache-2.0
"""The delivery half of `apps/api/tests/test_bare_routers.py`: a run's diff, the release train, the Tekton and
Argo CD webhooks, the software project's default workflow. These routes are the `software-delivery` plugin's
since round D of ADR 0181 (S83-05c): without its extension the API serves none of them, so their tests run
here, with the plugin, on the API suite's fixtures."""

from __future__ import annotations

import json
from typing import Any

import pytest
from httpx import AsyncClient


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


async def test_a_run_s_diff_says_which_files_are_in_scope(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    item = await _internal(client, project)
    run_id = await _run(
        project,
        item,
        allowed_paths=["src/**"],
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
    diff = (await client.get(f"/api/v1/runs/{run_id}/diff")).json()
    assert {f["path"]: f["in_scope"] for f in diff["files"]} == {"src/a.py": True, "infra/main.tf": False}


async def test_a_run_without_a_diff_report_has_no_files(client: AsyncClient, project: dict[str, Any]) -> None:
    item = await _internal(client, project)
    run_id = await _run(project, item)
    assert (await client.get(f"/api/v1/runs/{run_id}/diff")).json()["files"] == []


async def test_the_train_state_comes_from_the_workflow_when_it_answers(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.temporal import get_temporal, train_id

    get_temporal().queries[f"{train_id('billing-api', 'prod')}:status_query"] = {
        "status": "collecting",
        "batch_size": 2,
        "pending_items": ["BILLING-API-1", "BILLING-API-2"],
        "frozen": True,
        "freeze_reason": "rollback",
        "window_open": False,
    }
    state = (await client.get(f"/api/v1/projects/{project['id']}/trains/prod")).json()
    assert (
        state["batch_size"] == 2
        and state["frozen"]
        and state["freeze_reason"] == "rollback"
        and not state["window_open"]
    )
    unstarted = (await client.get(f"/api/v1/projects/{project['id']}/trains/staging")).json()
    assert unstarted["status"] == "collecting" and unstarted["batch_size"] == 0


async def test_departures_freezes_and_unfreezes_are_traced_signals(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    from tranibor_api.temporal import get_temporal

    base = f"/api/v1/projects/{project['id']}/trains/prod"
    assert (await client.post(f"{base}/depart")).status_code == 202
    assert (await client.post(f"{base}/freeze", json={"reason": "x"})).status_code == 422, (
        "a reason of less than three characters is not a reason"
    )
    assert (await client.post(f"{base}/freeze", json={"reason": "incident in progress"})).status_code == 202
    assert (await client.post(f"{base}/unfreeze")).status_code == 202
    names = [name for wf, name, _ in get_temporal().signals if wf == "train-billing-api-prod"]
    assert names == ["depart_now", "freeze", "unfreeze"]
    audit = (await client.get("/api/v1/audit", params={"target_type": "train"})).json()
    assert {e["action"] for e in audit["items"]} == {"train.depart", "train.freeze", "train.unfreeze"}


async def test_a_release_is_approved_and_aborted(client: AsyncClient, project: dict[str, Any]) -> None:
    from tranibor_api.db.models import Release
    from tranibor_api.db.session import session_scope
    from tranibor_api.temporal import get_temporal

    async with session_scope(orgs="*") as session:
        release = Release(
            project_id=project["id"],
            env="prod",
            batch_no=3,
            status="awaiting_approval",
            items=[{"work_item_key": "BILLING-API-1", "sha": "abc123"}],
        )
        session.add(release)
        await session.flush()
        release_id = str(release.id)
    page = (await client.get(f"/api/v1/projects/{project['id']}/releases", params={"env": "prod"})).json()
    assert [r["id"] for r in page["items"]] == [release_id]
    lue = (await client.get(f"/api/v1/releases/{release_id}")).json()
    assert lue["batch_no"] == 3 and lue["project_slug"] == "billing-api"
    assert (
        await client.post(f"/api/v1/releases/{release_id}/approve", json={"note": "ok"})
    ).status_code == 202
    assert (await client.get(f"/api/v1/releases/{release_id}")).json()["approved_by"] == "admin@northwind.dev"
    assert (
        await client.post(f"/api/v1/releases/{release_id}/abort", json={"reason": "smoke KO"})
    ).status_code == 202
    names = [name for wf, name, _ in get_temporal().signals if wf == "train-billing-api-prod"]
    assert names == ["approve", "abort"]
    assert (await client.get("/api/v1/releases/missing")).status_code == 404


async def test_a_tekton_cloudevent_is_routed_only_once(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import get_settings

    # This test assumes an empty webhook secret (test environment): it says so, instead of
    # depending on what another part of the session may have set (#259).
    monkeypatch.setattr(get_settings(), "generic_webhook_secret", "")
    body = {
        "pipelineRun": {
            "metadata": {
                "name": "run-1",
                "labels": {
                    "choregos/project": "billing-api",
                    "choregos/work-item": "BILLING-API-1",
                    "choregos/sha": "abc",
                },
            }
        }
    }
    headers = {"ce-type": "dev.tekton.event.pipelinerun.successful.v1", "ce-id": "ev-1"}
    first = await client.post("/api/v1/webhooks/tekton", json=body, headers=headers)
    assert first.status_code == 202 and first.json()["duplicate"] is False
    second = await client.post("/api/v1/webhooks/tekton", json=body, headers=headers)
    assert second.json()["duplicate"] is True, "same ce-id: nothing is routed twice"


async def test_argocd_requires_the_shared_secret_then_signals(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import get_settings
    from tranibor_api.temporal import get_temporal

    monkeypatch.setattr(get_settings(), "generic_webhook_secret", "s3cret")
    refusal = await client.post("/api/v1/webhooks/argocd", json={"app": "billing-prod"})
    assert refusal.status_code == 401
    ok = await client.post(
        "/api/v1/webhooks/argocd",
        json={
            "app": "billing-api-prod",
            "health": "Healthy",
            "project": "billing-api",
            "env": "prod",
            "revision": "abc",
            "delivery_id": "d-1",
        },
        headers={"X-Choregos-Secret": "s3cret"},
    )
    assert ok.status_code == 202 and ok.json()["events"] == 1
    assert any(
        wf == "train-billing-api-prod" and name == "deploy_event" for wf, name, _ in get_temporal().signals
    )
    again = await client.post(
        "/api/v1/webhooks/argocd",
        json={"app": "x", "project": "billing-api", "env": "prod", "delivery_id": "d-1"},
        headers={"X-Choregos-Secret": "s3cret"},
    )
    assert again.json()["duplicate"] is True


async def test_a_project_with_a_repository_still_receives_default_simple(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    assert project["workflow_name"] == "default-simple", "D14 is unchanged for a software project"


async def test_tekton_webhook_accepts_token_and_no_longer_the_former_parameter(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It had no check at all. Tekton's CloudEvents sink adds no header: the secret also travels in the
    URL (`?token=`). The former parameter, removed in 0.24.0 (S45-49), authenticates nothing."""
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_GENERIC_WEBHOOK_SECRET", "s3cret-tekton")
    reset_settings_cache()
    body = {"pipelineRun": {"metadata": {"name": "r", "labels": {"choregos/project": "billing-api"}}}}
    headers = {"ce-type": "dev.tekton.event.pipelinerun.successful.v1"}
    try:
        no_secret = await client.post(
            "/api/v1/webhooks/tekton", json=body, headers={**headers, "ce-id": "t-1"}
        )
        forged = await client.post(
            "/api/v1/webhooks/tekton",
            json=body,
            headers={**headers, "ce-id": "t-2", "x-choregos-secret": "non"},
        )
        assert no_secret.status_code == 401 and forged.status_code == 401
        header = await client.post(
            "/api/v1/webhooks/tekton",
            json=body,
            headers={**headers, "ce-id": "t-3", "x-choregos-secret": "s3cret-tekton"},
        )
        former_parameter = await client.post(
            "/api/v1/webhooks/tekton",
            params={"jeton": "s3cret-tekton"},  # lang: fr-ok - removed, asserted gone
            json=body,
            headers={**headers, "ce-id": "t-4"},
        )
        with_token = await client.post(
            "/api/v1/webhooks/tekton",
            params={"token": "s3cret-tekton"},
            json=body,
            headers={**headers, "ce-id": "t-5"},
        )
        wrong_token = await client.post(
            "/api/v1/webhooks/tekton",
            params={"token": "non"},
            json=body,
            headers={**headers, "ce-id": "t-6"},
        )
        assert header.status_code == 202 and with_token.status_code == 202
        assert former_parameter.status_code == 401, "the former parameter carries no secret any more"
        assert wrong_token.status_code == 401
    finally:
        reset_settings_cache()


async def test_without_a_secret_the_tekton_webhook_is_refused_outside_development(
    client: AsyncClient, org: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "env", "prod")
    monkeypatch.setattr(settings, "generic_webhook_secret", "")
    refusal = await client.post(
        "/api/v1/webhooks/tekton",
        json={},
        headers={"ce-type": "dev.tekton.event.pipelinerun.started.v1", "ce-id": "p-1"},
    )
    assert refusal.status_code == 401 and "TRANIBOR_GENERIC_WEBHOOK_SECRET" in refusal.text


async def test_a_transient_argocd_health_does_not_signal_a_degradation(
    client: AsyncClient, project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Since S22-18, the train rolls a departure back on `cd.app.degraded`: a `Progressing`
    notification, sent during a HEALTHY deployment, must not be one (#349)."""
    from tranibor_api.config import get_settings
    from tranibor_api.temporal import get_temporal

    monkeypatch.setattr(get_settings(), "generic_webhook_secret", "s3cret")
    headers = {"X-Choregos-Secret": "s3cret"}
    base = {"app": "billing-api-prod", "project": "billing-api", "env": "prod"}

    def degradations() -> list[Any]:
        return [
            p
            for wf, name, p in get_temporal().signals
            if name == "deploy_event" and p["type"] == "cd.app.degraded"
        ]

    for i, health_value in enumerate(["Progressing", "Suspended", "Missing", "Unknown"]):
        response = await client.post(
            "/api/v1/webhooks/argocd",
            json={**base, "health": health_value, "delivery_id": f"p-{i}"},
            headers=headers,
        )
        assert response.status_code == 202 and response.json()["events"] == 0, health_value
    assert degradations() == [], "a transient health is not a degradation"

    response = await client.post(
        "/api/v1/webhooks/argocd", json={**base, "health": "Degraded", "delivery_id": "p-9"}, headers=headers
    )
    assert response.json()["events"] == 1
    (signal_seen,) = degradations()
    assert signal_seen["payload"]["health"] == "degraded", "the received health travels with the event"
