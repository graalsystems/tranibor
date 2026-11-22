# SPDX-License-Identifier: Apache-2.0
"""The webhooks of the `ci` and `cd` connector types (ADR 0181, round D): a Tekton pipeline run, a Jenkins
build, an Argo CD application. They were the core's (`routers/webhooks.py`) until round D, and are served
through `extensions.declare_router` under the same paths and operation ids (`tektonWebhook`,
`jenkinsWebhook`, `argocdWebhook`), which `openapi.yaml` marks `x-plugin: software-delivery`.

What they share with the core's webhooks (the deduplication, the routing to a work item, the project an
event names) is the core's public `tranibor_api.routers.webhooks` helpers: a delivery is deduplicated and
routed the same way whoever serves it. They are imported when a delivery arrives: every process that loads
the extensions declares this router, and one that serves no route (the outposts' door alone) must not
import the API's routers.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request, status
from tranibor_api.config import get_settings
from tranibor_api.deps import PlatformDb
from tranibor_api.errors import unauthorized
from tranibor_api.schemas import WebhookAck
from tranibor_api.security import body_digest, verify_shared_secret
from tranibor_api.services import received_events
from tranibor_api.temporal import get_temporal, train_id
from tranibor_contracts import InboundEvent, InboundEventType

router = APIRouter(tags=["webhooks"], prefix="/webhooks")


# ───────────────────────────── Tekton ─────────────────────────────


@router.post(
    "/tekton", response_model=WebhookAck, status_code=status.HTTP_202_ACCEPTED, operation_id="tektonWebhook"
)
async def tekton_webhook(
    request: Request,
    session: PlatformDb,
    ce_type: Annotated[str, Header()] = "",
    ce_id: Annotated[str, Header()] = "",
    x_choregos_secret: Annotated[str, Header()] = "",
) -> WebhookAck:
    """CloudEvents `dev.tekton.event.pipelinerun.{successful,failed}.v1`.

    Authenticated by the shared secret, in the `X-Choregos-Secret` header OR in the sink URL
    (`?token=`): Tekton's CloudEvents sink is configured with a URL and adds no header. The former
    spelling of the parameter was removed in 0.24.0 (S45-49): it authenticates nothing. There used
    to be NO check: harmless while row-level security hid everything from this route on PostgreSQL,
    an anonymous write path as soon as it sees the projects ("CI passed" on any work item).
    """
    from tranibor_api.routers.webhooks import already_seen, dispatch

    settings = get_settings()
    supplied = x_choregos_secret or request.query_params.get("token", "")
    if not settings.generic_webhook_secret:
        if settings.deployed:
            raise unauthorized("Tekton webhook not configured: TRANIBOR_GENERIC_WEBHOOK_SECRET is missing")
    elif not verify_shared_secret(settings.generic_webhook_secret, supplied):
        raise unauthorized("invalid shared secret")
    body = await request.body()
    if await already_seen(session, "tekton", ce_id or body_digest(body), ce_type, body):
        return WebhookAck(accepted=True, duplicate=True)
    payload = json.loads(body or b"{}")
    run = payload.get("pipelineRun", payload)
    labels = (run.get("metadata", {}) or {}).get("labels", {})
    mapping = {
        "dev.tekton.event.pipelinerun.successful.v1": InboundEventType.CI_SUCCEEDED,
        "dev.tekton.event.pipelinerun.failed.v1": InboundEventType.CI_FAILED,
        "dev.tekton.event.pipelinerun.started.v1": InboundEventType.CI_STARTED,
    }
    event = InboundEvent(
        type=mapping.get(ce_type, InboundEventType.CI_STARTED),
        source="tekton",
        delivery_id=ce_id or body_digest(body),
        project_slug=labels.get("choregos/project"),
        work_item_key=labels.get("choregos/work-item"),
        payload={
            "pipeline_run": (run.get("metadata", {}) or {}).get("name"),
            "sha": labels.get("choregos/sha"),
            "run_id": labels.get("choregos/run-id"),
        },
    )
    delivered = await dispatch(session, [event])
    return WebhookAck(accepted=True, events=delivered)


# ───────────────────────────── Jenkins ─────────────────────────────


@router.post(
    "/jenkins", response_model=WebhookAck, status_code=status.HTTP_202_ACCEPTED, operation_id="jenkinsWebhook"
)
async def jenkins_webhook(
    request: Request,
    session: PlatformDb,
    x_choregos_secret: Annotated[str, Header()] = "",
) -> WebhookAck:
    """A build notification from Jenkins: the Notification plugin's payload, or the flat one a pipeline
    posts (`connectors.ci.jenkins.parse_jenkins_event`).

    Authenticated like the Tekton sink, by the shared secret in `X-Choregos-Secret` or in the URL
    (`?token=`): the Notification plugin takes a URL and no header."""
    from tranibor_api.routers.webhooks import already_seen, dispatch

    settings = get_settings()
    supplied = x_choregos_secret or request.query_params.get("token", "")
    if not settings.generic_webhook_secret:
        if settings.deployed:
            raise unauthorized("Jenkins webhook not configured: TRANIBOR_GENERIC_WEBHOOK_SECRET is missing")
    elif not verify_shared_secret(settings.generic_webhook_secret, supplied):
        raise unauthorized("invalid shared secret")
    body = await request.body()

    from .connectors.ci.jenkins import parse_jenkins_event

    events = parse_jenkins_event(json.loads(body or b"{}"))
    if not events:
        return WebhookAck(accepted=True)
    if await already_seen(session, "jenkins", events[0].delivery_id, str(events[0].type), body):
        return WebhookAck(accepted=True, duplicate=True)
    delivered = await dispatch(session, events)
    return WebhookAck(accepted=True, events=delivered)


# ───────────────────────────── Argo CD ─────────────────────────────


@router.post(
    "/argocd", response_model=WebhookAck, status_code=status.HTTP_202_ACCEPTED, operation_id="argocdWebhook"
)
async def argocd_webhook(
    request: Request,
    session: PlatformDb,
    x_choregos_secret: Annotated[str, Header()] = "",
) -> WebhookAck:
    from tranibor_api.routers.webhooks import already_seen, project_for

    body = await request.body()
    settings = get_settings()
    if not verify_shared_secret(settings.generic_webhook_secret, x_choregos_secret):
        raise unauthorized("invalid shared secret")
    payload = json.loads(body or b"{}")
    app = payload.get("app", payload.get("application", ""))
    health = str(payload.get("health", payload.get("status", ""))).lower()
    kind = _argocd_kind(health, payload.get("rollout"))
    delivery = payload.get("delivery_id", body_digest(body))
    if await already_seen(session, "argocd", delivery, str(kind or f"argocd.{health or 'unknown'}"), body):
        return WebhookAck(accepted=True, duplicate=True)
    if kind is None:
        # A transient health (`progressing`, `suspended`, `missing`, `unknown`...) is neither a
        # delivery nor an alert: reporting it as `cd.app.degraded` rolled back a healthy start,
        # since the train listens to the CD (S22-18, #349).
        return WebhookAck(accepted=True, events=0)
    event = InboundEvent(
        type=kind,
        source="argocd",
        delivery_id=delivery,
        project_slug=payload.get("project"),
        payload={
            "app": app,
            "revision": payload.get("revision"),
            "env": payload.get("env"),
            "health": health,
        },
    )
    project = await project_for(session, event.project_slug)
    if project is not None and event.payload.get("env"):
        await get_temporal().signal(
            train_id(project.slug, str(event.payload["env"])), "deploy_event", event.model_dump(mode="json")
        )
        received_events.record(session, project, event, "signalled")
    return WebhookAck(accepted=True, events=1)


def _argocd_kind(health: str, rollout: Any) -> InboundEventType | None:
    """What an Argo CD notification announces: delivered, degraded, rollout finished or aborted, or
    nothing, for a transient health. Only `degraded` is an alert (#349)."""
    if rollout == "aborted":
        return InboundEventType.ROLLOUT_ABORTED
    if rollout == "completed":
        return InboundEventType.ROLLOUT_COMPLETED
    if health in {"healthy", "synced"}:
        return InboundEventType.CD_SYNCED
    if health == "degraded":
        return InboundEventType.CD_DEGRADED
    return None
