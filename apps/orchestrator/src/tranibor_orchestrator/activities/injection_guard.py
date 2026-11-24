# SPDX-License-Identifier: Apache-2.0
"""The guard against prompt injection, played before anything is handed to the agent."""

from __future__ import annotations

import os
from typing import Any

import structlog
from tranibor_api.services import persist_event
from tranibor_contracts import ContextPack, EventType
from tranibor_core.injection_classifier import ClassifierUnavailable, InjectionClassifier

from .base import db
from .plan import StagePlan

log = structlog.get_logger()

#: The deployment's choice of a model-based detector beside the lexical one (ADR 0219): `off` (the
#: default), `http` (a classifier service at `TRANIBOR_INJECTION_CLASSIFIER_URL`), or `fake` (the
#: scriptable fake, honoured only under `TRANIBOR_FAKES`). The deployment's, not a policy's.
CLASSIFIER_VARIABLE = "TRANIBOR_INJECTION_CLASSIFIER"
#: A test's classifier, set with `set_classifier` and cleared with `set_classifier(None)`.
_override: list[InjectionClassifier | None] = [None]


def set_classifier(classifier: InjectionClassifier | None) -> None:
    _override[0] = classifier


def classifier_from_environment() -> InjectionClassifier | None:
    """The configured classifier, `None` when off. A setting that cannot be honoured (an unknown
    value, `http` without a URL, `fake` outside the fakes) raises `ClassifierUnavailable`: a
    misconfigured detector is a detector that cannot answer, not one that is silently off."""
    if _override[0] is not None:
        return _override[0]
    from tranibor_adapters.fakes.injection_classifier import FakeInjectionClassifier
    from tranibor_adapters.injection_classifier import (
        DEFAULT_PATH,
        DEFAULT_THRESHOLD,
        DEFAULT_TIMEOUT_S,
        HttpInjectionClassifier,
    )
    from tranibor_core.production import FAKES_VARIABLE, fakes_on

    env = os.environ
    kind = env.get(CLASSIFIER_VARIABLE, "off").strip().lower() or "off"
    if kind == "off":
        return None
    if kind == "fake":
        if not fakes_on(env.get(FAKES_VARIABLE)):
            raise ClassifierUnavailable(f"{CLASSIFIER_VARIABLE}=fake needs TRANIBOR_FAKES")
        return FakeInjectionClassifier({"ignore all previous": 0.99})
    if kind == "http":
        url = env.get(f"{CLASSIFIER_VARIABLE}_URL", "").strip()
        if not url:
            raise ClassifierUnavailable(f"{CLASSIFIER_VARIABLE}=http needs {CLASSIFIER_VARIABLE}_URL")
        try:
            return HttpInjectionClassifier(
                url,
                token=env.get(f"{CLASSIFIER_VARIABLE}_TOKEN") or None,
                path=env.get(f"{CLASSIFIER_VARIABLE}_PATH", DEFAULT_PATH),
                threshold=float(env.get(f"{CLASSIFIER_VARIABLE}_THRESHOLD", DEFAULT_THRESHOLD)),
                timeout_s=float(env.get(f"{CLASSIFIER_VARIABLE}_TIMEOUT_S", DEFAULT_TIMEOUT_S)),
            )
        except ValueError as exc:
            raise ClassifierUnavailable(f"{CLASSIFIER_VARIABLE}_*: a number was expected") from exc
    raise ClassifierUnavailable(f"{CLASSIFIER_VARIABLE}={kind!r}: expected off, http or fake")


async def guard_against_injection(
    session: Any, bundle: Any, item: Any, run_id: str, plan: StagePlan, context_pack: ContextPack
) -> None:
    """Look at what the agent is about to read BEFORE handing it over, and do what the policy says.

    `warn`: a security event, visible on the work item. `block`: the step does not start; the
    activity fails without a retry, the work item is marked dead with the reason (P0-2), a human
    reads it. `ignore`: nothing. It is lexical (`tranibor_core.injection`), and says so: a net for
    the crude cases, not an understanding.
    """
    if getattr(bundle.policy.sandbox, "prompt_injection", "warn") == "ignore":
        return
    documents = item.documents or {}
    sources: dict[str, str] = {
        "ticket.title": item.title or "",
        "ticket.reporter": getattr(item, "reporter", None) or "",
        "ticket.body": item.body_snapshot or "",
    }
    sources.update({f"document.{name}": text for name, text in documents.items() if isinstance(text, str)})
    sources.update({f"memory.{m.id or i}": m.content for i, m in enumerate(context_pack.memories)})
    await guard_sources(bundle, item, run_id, plan.role, sources)


async def guard_sources(bundle: Any, item: Any, run_id: str, stage: str, sources: dict[str, str]) -> None:
    """The injection guard on named texts about to leave for a model: an agent's context, or the
    state a decision step sends (ADR 0121 §2), under the policy's `sandbox.prompt_injection`.

    `warn` records a security event on the work item; `block` raises a non-retryable
    `injection_suspected` and the step does not start; `ignore` does nothing."""
    from temporalio.exceptions import ApplicationError
    from tranibor_core.injection import analyser
    from tranibor_core.injection_classifier import to_suspicions

    mode = getattr(bundle.policy.sandbox, "prompt_injection", "warn")
    if mode == "ignore":
        return
    suspected = analyser(sources)
    unavailable = False
    try:
        classifier = classifier_from_environment()
        if classifier is not None:
            # What the patterns already flagged needs no second opinion: only the rest is sent.
            flagged = {a.source for a in suspected}
            rest = {k: v for k, v in sources.items() if k not in flagged and v}
            if rest:
                suspected += to_suspicions(await classifier.classify(rest), rest)
    except ClassifierUnavailable as exc:
        unavailable = True
        log.warning(
            "injection_classifier_unavailable",
            project=bundle.slug,
            work_item=item.id,
            run_id=run_id,
            stage=stage,
            mode=mode,
            reason=str(exc),
        )
    if unavailable and mode == "block" and not suspected:
        # Fail closed (ADR 0219): the detector that was asked for could not answer, and `block` is
        # the policy that wants no unread text to reach a model. Retryable: an outage is not a verdict.
        raise ApplicationError(
            "injection classifier unavailable, step stopped before any run — "
            "policy sandbox.prompt_injection: block; the step is retried",
            type="injection_classifier_unavailable",
        )
    if not suspected:
        return
    # In its own transaction: in `block` the activity raises right after and the step's session is
    # rolled back with it; the event must survive, it is what the work item shows.
    async with db() as journal:
        await persist_event(
            journal,
            EventType.SECURITY_INJECTION_SUSPECTED,
            project_id=bundle.project.id,
            work_item_id=item.id,
            project_slug=bundle.slug,
            subject=item.tracker_key,
            run_id=run_id,
            stage=stage,
            mode=mode,
            suspicions=[a.to_dict() for a in suspected[:10]],
        )
    if mode == "block":
        resume = "; ".join(f"{a.source}: {a.motif}" for a in suspected[:3])
        raise ApplicationError(
            f"suspected prompt injection, step stopped before any run ({resume}) — "
            "policy sandbox.prompt_injection: block; read the work item, then retry",
            type="injection_suspected",
            non_retryable=True,
        )
