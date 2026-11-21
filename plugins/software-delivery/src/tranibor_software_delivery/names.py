# SPDX-License-Identifier: Apache-2.0
"""The plugin's name (ADR 0181 point 4): the content plugin's, published from the publishing identity's
agents repository, and the name of this extension's `choregos.plugins` entry point; and the names of the
release train the contracts held until round D (its statuses, its event types)."""

from __future__ import annotations

from enum import StrEnum

NAME = "software-delivery"


class ReleaseStatus(StrEnum):
    """A release's status, as the train's `status_query` answers it and the `releases` rows store it (the
    contracts' `ReleaseStatus` until round D of ADR 0181, under the same values)."""

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
    """The release train's event types, declared in the contracts' `EVENT_TYPES` (round D). The strings are
    the ones subscribers and the `events` rows key on: they never change (ADR 0176 §6)."""

    COLLECTED = "choregos.release.collected"
    DEPARTED = "choregos.release.departed"
    STAGED = "choregos.release.staged"
    APPROVAL_REQUESTED = "choregos.release.approval_requested"
    PROMOTED = "choregos.release.promoted"
    VERIFIED = "choregos.release.verified"
    ROLLED_BACK = "choregos.release.rolled_back"
    FROZEN = "choregos.release.frozen"


#: The deployment events that interrupt a train (`cd.*`, the plugin's inbound event types since round C2).
CD_DEGRADED = "cd.app.degraded"
CD_ROLLOUT_ABORTED = "cd.rollout.aborted"
