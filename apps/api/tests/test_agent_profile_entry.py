# SPDX-License-Identifier: Apache-2.0
"""An agent's `mcp_servers` entry names a connector or a profile, never both (ADR 0224 §4, S86-01).

Nothing resolves a profile entry at run time yet (S86-04): the shape is the contract."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tranibor_api.schemas.agents import AgentMcpServer


def test_a_connector_entry_keeps_its_stored_shape() -> None:
    """No `profile` key in the dump: versions published before the field keep their digest."""
    entry = AgentMcpServer(connector="feed", tools=["get_bars"])
    assert entry.model_dump(mode="json") == {"connector": "feed", "tools": ["get_bars"]}


def test_a_profile_entry_has_no_connector() -> None:
    entry = AgentMcpServer(profile="notes/notes@1", tools=["list_notes"])
    assert entry.model_dump(mode="json") == {"profile": "notes/notes@1", "tools": ["list_notes"]}


@pytest.mark.parametrize(
    "entry",
    [
        {"tools": ["x"]},
        {"connector": "feed", "profile": "notes/notes@1", "tools": ["x"]},
        {"profile": "notes/notes@1", "identity": "user_exchange"},
        {"profile": "notes"},
        {"profile": "notes/notes@0"},
        {"profile": "notes/Notes@1"},
    ],
)
def test_a_malformed_entry_is_refused(entry: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AgentMcpServer.model_validate(entry)
