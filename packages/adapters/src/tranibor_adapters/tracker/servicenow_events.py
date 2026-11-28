# SPDX-License-Identifier: Apache-2.0
"""What a ServiceNow Business Rule posts, verified and normalised into `InboundEvent` (ADR 0228).

The Business Rule of the setup procedure (`connector.tracker.servicenow`) posts, for each insert or
update of an incident, a request item or a change, a JSON object (or a list of them):

    {"table": "incident", "sys_id": "<32 hex>", "number": "INC0010001",
     "sys_mod_count": 3, "operation": "insert" | "update" | "comment", "state": "2",
     "instance": "<instance name, optional>"}

with the shared secret in `X-Tranibor-Secret`. ServiceNow signs nothing: the secret, compared in
constant time, is the only barrier. Each object becomes one event keyed by the record's number, whose
`delivery_id` is `sys_id:sys_mod_count`: a replayed delivery carries the same id, and the API
de-duplicates on it. Nothing else of the payload is trusted: the API re-reads the item before acting.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import Any

from tranibor_contracts import InboundEvent, InboundEventType
from tranibor_contracts.wire import read_header

from ..errors import ConfigurationError
from .servicenow_api import CLOSED_STATES, TRACKER_TABLES, is_sys_id, table_of_number

SOURCE = "servicenow"


class ServiceNowEventError(ValueError):
    """A delivery whose body is not what the Business Rule posts: answered 400, never retried as is."""


def secret_of(headers: Mapping[str, str]) -> str:
    """The secret a delivery carries: `X-Tranibor-Secret` (the former header too, for the window of ADR
    0226: a Business Rule written for the plugin's server sends that one). Empty when none."""
    return read_header(headers, "Secret")


def verify_servicenow_delivery(headers: Mapping[str, str], secret: str | None) -> bool:
    """Constant-time comparison of the delivery's secret with the configured one; no secret, no delivery."""
    if not secret:
        return False
    return hmac.compare_digest(secret_of(headers).encode(), secret.encode())


def parse_servicenow_event(body: bytes, *, tables: tuple[str, ...] = TRACKER_TABLES) -> list[InboundEvent]:
    """The events of one delivery. A record of a table outside `tables` (the connector's) is skipped; a
    malformed object raises `ServiceNowEventError`."""
    try:
        payload: Any = json.loads(body or b"null")
    except ValueError as error:
        raise ServiceNowEventError("the body is not JSON") from error
    items = payload if isinstance(payload, list) else [payload]
    events: list[InboundEvent] = []
    for item in items:
        if not isinstance(item, dict):
            raise ServiceNowEventError("each event is a JSON object")
        event = _event(item, tables)
        if event is not None:
            events.append(event)
    return events


def _event(item: dict[str, Any], tables: tuple[str, ...]) -> InboundEvent | None:
    table = str(item.get("table", ""))
    if table not in TRACKER_TABLES or table not in tables:
        return None
    sys_id = str(item.get("sys_id", ""))
    number = str(item.get("number", ""))
    if not is_sys_id(sys_id):
        raise ServiceNowEventError("an event needs the record's sys_id")
    try:
        owner = table_of_number(number)
    except ConfigurationError as error:  # the delivery's fault, not the connector's
        raise ServiceNowEventError(str(error)) from error
    if owner != table:
        raise ServiceNowEventError(f"{number} is not a {table} number")
    try:
        mod_count = int(item.get("sys_mod_count", -1))
    except (TypeError, ValueError) as error:
        raise ServiceNowEventError("sys_mod_count is an integer") from error
    if mod_count < 0:
        raise ServiceNowEventError("an event needs the record's sys_mod_count")
    operation = str(item.get("operation", "update"))
    state = str(item.get("state", ""))
    if operation == "insert":
        kind = InboundEventType.ITEM_CREATED
    elif operation == "comment":
        kind = InboundEventType.ITEM_COMMENTED
    elif operation == "update":
        kind = (
            InboundEventType.ITEM_CLOSED if state in CLOSED_STATES[table] else InboundEventType.ITEM_UPDATED
        )
    else:
        return None
    instance = str(item.get("instance") or "").strip()
    return InboundEvent(
        type=kind,
        source=SOURCE,
        delivery_id=f"{sys_id}:{mod_count}",
        work_item_key=number,
        payload={
            "table": table,
            "item_type": table,
            "state_value": state,
            "operation": operation,
            **({"instance": instance} if instance else {}),
        },
    )


__all__ = [
    "SOURCE",
    "ServiceNowEventError",
    "parse_servicenow_event",
    "secret_of",
    "verify_servicenow_delivery",
]
