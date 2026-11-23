# SPDX-License-Identifier: Apache-2.0
"""The relation of the link to the change a work item proposes (ADR 0181 C1)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: The relation of the link to the change a work item proposes for review.
CHANGE_REQUEST = "change_request"


def change_request_of(item: Mapping[str, Any]) -> str | None:
    """The URL of the change a work item proposes: its `change_request` link."""
    for link in item.get("links") or ():
        if isinstance(link, Mapping) and link.get("rel") == CHANGE_REQUEST and link.get("url"):
            return str(link["url"])
    return None
