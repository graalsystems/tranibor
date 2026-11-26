# SPDX-License-Identifier: Apache-2.0
"""The plugin's name (ADR 0181 point 4): the content plugin's, published from the publishing identity's
agents repository, and the name of this extension's `choregos.plugins` entry point; and the values of the
contracts' open lists that are software delivery's, which the core's enumerations held until ADR 0181
closed the windows C2, D-train and D (2026-11-23): the inbound event types, finding types, memory kinds and
connector kinds of software delivery, the release train's event types and its statuses. The strings never
change: they are stored (`received_events`, the findings, the memories, the connectors, the `events` and
`releases` rows) and carried by recorded histories."""

from __future__ import annotations

from enum import StrEnum

NAME = "software-delivery"


class DeliveryEvent(StrEnum):
    """The inbound event types of a source control, a pipeline and a deployment (`scm.*`, `ci.*`, `cd.*`),
    under the names the core's `InboundEventType` gave them until round C2's window closed."""

    PR_OPENED = "scm.pr.opened"
    PR_SYNCHRONIZED = "scm.pr.synchronized"
    PR_REVIEW_SUBMITTED = "scm.pr.review_submitted"
    PR_MERGED = "scm.pr.merged"
    PR_CLOSED = "scm.pr.closed"
    CHECK_COMPLETED = "scm.check.completed"
    CI_STARTED = "ci.run.started"
    CI_SUCCEEDED = "ci.run.succeeded"
    CI_FAILED = "ci.run.failed"
    CD_SYNCED = "cd.app.synced"
    CD_DEGRADED = "cd.app.degraded"
    ROLLOUT_COMPLETED = "cd.rollout.completed"
    ROLLOUT_ABORTED = "cd.rollout.aborted"


class DeliveryFinding(StrEnum):
    """The finding types of software delivery (the core's `FindingType` members until round C2's window
    closed)."""

    BUG = "bug"
    PERF = "perf"
    TECH_DEBT = "tech-debt"
    FLAKY_TEST = "flaky-test"


class DeliveryMemory(StrEnum):
    """The memory kinds of software delivery (the core's `MemoryKind` members until round C2's window
    closed)."""

    FLAKY_TEST = "flaky_test"
    HOTSPOT = "hotspot"


class DeliveryKind(StrEnum):
    """The connector kinds of software delivery: a source control, a pipeline, a deployment (the core's
    `ConnectorKind` members until round D's window closed). Stored connectors keep their `kind`."""

    SCM = "scm"
    CI = "ci"
    CD = "cd"


class DeliveryRole(StrEnum):
    """The stage roles of software delivery, which the core's `StageRole` held until ADR 0181 moved them here
    (S83-12): each has its playbook in `roles/`, which the extension declares (`roles.declare_roles`). The
    strings never change: stored workflows, the runs' `stage_role` and recorded histories carry them."""

    FIX_CI = "fix_ci"
    ADDRESS_REVIEW = "address_review"
    RELEASE_NOTES = "release_notes"
    VERIFY_PROD = "verify_prod"


class ReleaseStatus(StrEnum):
    """A release's status, as the train's `status_query` answers it and the `releases` rows store it (the
    contracts' `ReleaseStatus` until the window D-train of ADR 0181 closed, under the same values)."""

    COLLECTING = "collecting"
    DEPARTING = "departing"
    STAGING = "staging"
    AWAITING_APPROVAL = "awaiting_approval"
    PROMOTING = "promoting"
    VERIFYING = "verifying"
    DONE = "done"
    ROLLED_BACK = "rolled_back"
    FROZEN = "frozen"


class ReleaseEvent(StrEnum):
    """The release train's event types, declared in the contracts' `EVENT_TYPES` (round D):
    `tranibor.release.*` since 0.30.0 (ADR 0226, R9c). An `events` row or a subscriber's filter under the
    former `choregos.release.*` reads as its twin (`EVENT_TYPES.check`, `wire.event_type`) until 0.32.0."""

    COLLECTED = "tranibor.release.collected"
    DEPARTED = "tranibor.release.departed"
    STAGED = "tranibor.release.staged"
    APPROVAL_REQUESTED = "tranibor.release.approval_requested"
    PROMOTED = "tranibor.release.promoted"
    VERIFIED = "tranibor.release.verified"
    ROLLED_BACK = "tranibor.release.rolled_back"
    FROZEN = "tranibor.release.frozen"


#: The deployment events that interrupt a train (`cd.*`, the plugin's inbound event types since round C2).
CD_DEGRADED = DeliveryEvent.CD_DEGRADED.value
CD_ROLLOUT_ABORTED = DeliveryEvent.ROLLOUT_ABORTED.value
