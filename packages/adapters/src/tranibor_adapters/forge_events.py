# SPDX-License-Identifier: Apache-2.0
"""A forge's pull request and pipeline events, as the core's GitHub, GitLab and Azure DevOps hooks read them.

Those webhooks carry the core's trackers' issue events and, beside them, a source control's and a
pipeline's: a pull request opened, synchronised, reviewed, merged or closed, a check suite completed, a
pipeline run started, succeeded or failed. Their types are the `software-delivery` plugin's (ADR 0181,
round C2): since the window C2 closed (2026-11-23) only its extension declares them in the contracts'
`INBOUND_EVENT_TYPES`. The core names them here by their strings, which never change: `received_events`
stores them, recorded histories carry them and the interpreter reads them by string.

A deployment without the extension receives the same deliveries: the issue events are delivered, and a pull
request's or a pipeline's is not (`forge_events` answers none, and logs it), since no installed extension
gives it a meaning. Nothing in the core consumes one but the plugin's gates and its inbound hook.
"""

from __future__ import annotations

from typing import Any

import structlog
from tranibor_contracts import INBOUND_EVENT_TYPES, InboundEvent

log = structlog.get_logger(__name__)

PR_OPENED = "scm.pr.opened"
PR_SYNCHRONIZED = "scm.pr.synchronized"
PR_REVIEW_SUBMITTED = "scm.pr.review_submitted"
PR_MERGED = "scm.pr.merged"
PR_CLOSED = "scm.pr.closed"
CHECK_COMPLETED = "scm.check.completed"
CI_STARTED = "ci.run.started"
CI_SUCCEEDED = "ci.run.succeeded"
CI_FAILED = "ci.run.failed"


def forge_events(type_: str, **fields: Any) -> list[InboundEvent]:
    """One event of `type_`, or none where no installed extension declares the type."""
    if type_ not in INBOUND_EVENT_TYPES.known():
        log.info(
            "forge event not delivered: no installed extension declares its type",
            event_type=type_,
            source=fields.get("source"),
            delivery=fields.get("delivery_id"),
        )
        return []
    return [InboundEvent(type=type_, **fields)]
