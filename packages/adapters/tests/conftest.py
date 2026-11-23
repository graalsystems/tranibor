# SPDX-License-Identifier: Apache-2.0
import os
from collections.abc import Iterator

import pytest

# The egress allowlist (S39-22) only warns in the adapters' suites: the tests that prove the guard
# ask for `enforce` themselves. Set before the adapters are imported.
os.environ.setdefault("TRANIBOR_EGRESS_MODE", "warn")

from tranibor_adapters import forge_events
from tranibor_contracts import INBOUND_EVENT_TYPES

#: A forge's pull request and pipeline types, the `software-delivery` plugin's (ADR 0181 C2).
FORGE_TYPES = (
    forge_events.PR_OPENED,
    forge_events.PR_SYNCHRONIZED,
    forge_events.PR_REVIEW_SUBMITTED,
    forge_events.PR_MERGED,
    forge_events.PR_CLOSED,
    forge_events.CHECK_COMPLETED,
    forge_events.CI_STARTED,
    forge_events.CI_SUCCEEDED,
    forge_events.CI_FAILED,
)
SOFTWARE_DELIVERY = "software-delivery"


@pytest.fixture
def forge_types() -> Iterator[None]:
    """The deployment with the `software-delivery` plugin's extension: the forge's types declared, as it
    declares them. The suite without the plugin (`core-without-plugins`) declares them here, for the test."""
    missing = [value for value in FORGE_TYPES if value not in INBOUND_EVENT_TYPES.known()]
    for value in missing:
        INBOUND_EVENT_TYPES.declare(value, plugin=SOFTWARE_DELIVERY)
    yield
    if missing:
        INBOUND_EVENT_TYPES.forget(SOFTWARE_DELIVERY)


@pytest.fixture
def no_forge_types() -> Iterator[None]:
    """The deployment without the extension: nothing declares the forge's types, then they are back."""
    declared = INBOUND_EVENT_TYPES.declared_by(SOFTWARE_DELIVERY)
    INBOUND_EVENT_TYPES.forget(SOFTWARE_DELIVERY)
    yield
    for value in sorted(declared):
        INBOUND_EVENT_TYPES.declare(value, plugin=SOFTWARE_DELIVERY)
