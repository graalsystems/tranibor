"""Histories under an organisation's data key replay, older histories replay under them, and a shredded
organisation's history never replays again (ADR 0224).

The release train is run for real on a time-skipping Temporal, recorded, exported and replayed with the
deployment's converter, as in `test_replay_with_org_keys.py`, whose recording helpers this file reuses.
`workflows/` is not touched by ADR 0224: these runs prove the codec change leaves every history replayable.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import pytest
from tranibor_core.temporal_connection import (
    KEY_SOURCE_METADATA,
    ORG_METADATA,
    DataKeyCache,
    ShreddedKeyError,
    TemporalSecurity,
    UnknownCodecKeyError,
    data_converter,
    parse_codec_keys,
)

from .test_replay_with_org_keys import MASTER, TICKET, _history, _orgs, _record

ORG_DATA_KEY_B64 = base64.b64encode(b"org-data-key").decode()


class MemoryKeys:
    """An organisation's data keys, as the API's table holds them, in memory."""

    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, bytes]] = {}
        self.shredded: set[str] = set()

    async def active_data_key(self, org_id: str) -> tuple[str, bytes]:
        if org_id not in self.keys or org_id in self.shredded:
            self.keys[org_id] = ("dk-" + os.urandom(8).hex(), os.urandom(32))
            self.shredded.discard(org_id)
        return self.keys[org_id]

    async def data_key(self, org_id: str, key_id: str) -> bytes:
        if org_id in self.shredded:
            raise ShreddedKeyError(f"the data key `{key_id}` of `{org_id}` was shredded")
        found = self.keys.get(org_id)
        if found is None or found[0] != key_id:
            raise UnknownCodecKeyError(f"`{org_id}` has no data key `{key_id}`")
        return found[1]


def _converter(source: MemoryKeys | None) -> Any:
    keys = parse_codec_keys(f"k1:{base64.b64encode(MASTER).decode()}")
    cache = DataKeyCache(source) if source is not None else None
    return data_converter(TemporalSecurity(codec_keys=keys, data_keys=cache))


def _sources(history: Any) -> set[str]:
    """The `encryption-key-source` values the exported history's payloads carry (base64, as exported)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            metadata = node.get("metadata")
            if isinstance(metadata, dict) and ORG_METADATA in metadata:
                found.add(str(metadata.get(KEY_SOURCE_METADATA, "")))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(json.loads(history.to_json()))
    return found


async def test_an_organisations_history_under_its_data_key_hides_the_ticket_and_replays() -> None:
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    source = MemoryKeys()
    recorded = await _record(_converter(source), "org-a")
    assert _orgs(recorded) == {b"org-a"}
    assert _sources(recorded) == {ORG_DATA_KEY_B64}, "every organisation payload is under its data key"
    assert TICKET not in recorded.to_json()
    # another process of the same deployment: a fresh cache over the same key store
    await Replayer(workflows=all_workflows(), data_converter=_converter(source)).replay_workflow(
        _history(recorded)
    )


async def test_a_history_of_adr_0220_replays_under_the_data_keys() -> None:
    """The upgrade: a history sealed under the derived key replays with the new converter unchanged."""
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    recorded = await _record(_converter(None), "org-a")
    assert _sources(recorded) == {""}, "ADR 0220: the organisation, no data key"
    await Replayer(workflows=all_workflows(), data_converter=_converter(MemoryKeys())).replay_workflow(
        _history(recorded)
    )


async def test_a_shredded_organisations_history_never_replays_again() -> None:
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    source = MemoryKeys()
    recorded = await _record(_converter(source), "org-a")
    source.shredded.add("org-a")
    with pytest.raises(Exception, match="ShreddedKeyError"):
        await Replayer(workflows=all_workflows(), data_converter=_converter(source)).replay_workflow(
            _history(recorded)
        )
