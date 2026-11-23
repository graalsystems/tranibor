# SPDX-License-Identifier: Apache-2.0
"""The values of the contracts' open lists that are software delivery's (ADR 0181, round C2): the inbound
event types of a source control, a pipeline and a deployment (`scm.*`, `ci.*`, `cd.*`), the finding types
`bug`, `perf`, `tech-debt` and `flaky-test`, the memory kinds `flaky_test` and `hotspot`, the
connector kinds `scm`, `ci` and `cd`, and the release train's event types `choregos.release.*` (round D).

The strings do not change: they are stored in `received_events`, the findings, the memories, the connectors
and the `events` rows. The core's lists held them until the windows C2, D-train and D closed (2026-11-23):
since then only this declaration makes them valid, and a document the platform stored reads them with or
without it (`tranibor_contracts.open_lists.STORED`).
"""

from __future__ import annotations

from tranibor_contracts import CONNECTOR_KINDS, EVENT_TYPES, FINDING_TYPES, INBOUND_EVENT_TYPES, MEMORY_KINDS

from .names import NAME, DeliveryEvent, DeliveryFinding, DeliveryKind, DeliveryMemory, ReleaseEvent

#: The plugin's values of each list, from its own enumerations.
DELIVERY_EVENT_TYPES: frozenset[str] = frozenset(member.value for member in DeliveryEvent)
DELIVERY_FINDING_TYPES: frozenset[str] = frozenset(member.value for member in DeliveryFinding)
DELIVERY_MEMORY_KINDS: frozenset[str] = frozenset(member.value for member in DeliveryMemory)
DELIVERY_CONNECTOR_KINDS: frozenset[str] = frozenset(member.value for member in DeliveryKind)
RELEASE_EVENT_TYPES: frozenset[str] = frozenset(member.value for member in ReleaseEvent)

#: Each open list, and the values the plugin declares in it.
DECLARED = (
    (INBOUND_EVENT_TYPES, DELIVERY_EVENT_TYPES),
    (FINDING_TYPES, DELIVERY_FINDING_TYPES),
    (MEMORY_KINDS, DELIVERY_MEMORY_KINDS),
    (CONNECTOR_KINDS, DELIVERY_CONNECTOR_KINDS),
    # The release train's event types (round D).
    (EVENT_TYPES, RELEASE_EVENT_TYPES),
)


def declare_values() -> None:
    """Declare the plugin's values in the contracts' open lists. Idempotent."""
    for open_list, values in DECLARED:
        for value in sorted(values):
            open_list.declare(value, plugin=NAME)
