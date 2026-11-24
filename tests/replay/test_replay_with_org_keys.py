"""Histories under one key per organisation, and histories sealed before it, all replay (ADR 0220).

Three runs of the same workflow, each recorded then replayed with the deployment's converter:

- an organisation's run: its payloads name the organisation, and carry no ticket text;
- a run sealed the ADR 0183 way (master key, no organisation): it replays unchanged;
- the organisation's history does not replay under another deployment's keyring.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Sequence
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, PayloadCodec
from tranibor_core.temporal_connection import (
    ENCRYPTED_ENCODING,
    KEY_ID_METADATA,
    ORG_METADATA,
    TemporalSecurity,
    data_converter,
    organisation_scope,
    parse_codec_keys,
)

TICKET = "HR-77 Salary review for Jane Doe: 81500 EUR, medical leave since March"
MASTER = os.urandom(32)
#: The encoding of a sealed payload, as the exported history writes it (base64). Search attributes are
#: Temporal's to read and stay `json/plain`.
SEALED = base64.b64encode(ENCRYPTED_ENCODING).decode()


class Adr0183Codec(PayloadCodec):
    """The codec exactly as ADR 0183 shipped it: one key, its id as associated data, no organisation."""

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for payload in payloads:
            nonce = os.urandom(12)
            sealed = AESGCM(MASTER).encrypt(nonce, payload.SerializeToString(), b"k1")
            out.append(
                Payload(
                    metadata={"encoding": ENCRYPTED_ENCODING, KEY_ID_METADATA: b"k1"}, data=nonce + sealed
                )
            )
        return out

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        out = []
        for payload in payloads:
            plain = AESGCM(MASTER).decrypt(payload.data[:12], payload.data[12:], b"k1")
            original = Payload()
            original.ParseFromString(plain)
            out.append(original)
        return out


def _converter() -> Any:
    keys = parse_codec_keys(f"k1:{base64.b64encode(MASTER).decode()}")
    return data_converter(TemporalSecurity(codec_keys=keys))


def _stub_activities() -> list[Any]:
    from temporalio import activity

    @activity.defn(name="load_train_config")
    async def load_train_config(payload: dict[str, Any]) -> dict[str, Any]:
        return {"mode": "auto_sync", "summary": TICKET}

    return [load_train_config]


async def _record(converter: Any, organisation: str | None) -> Any:
    from temporalio.testing import WorkflowEnvironment
    from tranibor_orchestrator.testing import workers_by_queue

    ReleaseTrain = pytest.importorskip(  # noqa: N806 - the class, by its name
        "tranibor_software_delivery.train.workflow", reason="the release train is the plugin's"
    ).ReleaseTrain

    environment = await WorkflowEnvironment.start_time_skipping(data_converter=converter)
    try:
        async with workers_by_queue(
            environment.client,
            activities=_stub_activities(),
            workflows=[ReleaseTrain],
            workflow_queue="replay",
        ):
            with organisation_scope(organisation):
                handle = await environment.client.start_workflow(
                    "ReleaseTrain",
                    {"project_slug": "p", "env": "dev", "carried_items": [{"key": "HR-77", "title": TICKET}]},
                    id="train-org-dev",
                    task_queue="replay",
                )
                await handle.signal("abort", {"by": TICKET})
            await handle.result()
            return await handle.fetch_history()
    finally:
        await environment.shutdown()


def _history(raw: Any) -> Any:
    from temporalio.client import WorkflowHistory

    return WorkflowHistory.from_json("train-org-dev", json.loads(raw.to_json()))


def _sealed_without_org(history: Any) -> int:
    """How many sealed payloads of the exported history name no organisation."""
    count = 0

    def walk(node: Any) -> None:
        nonlocal count
        if isinstance(node, dict):
            metadata = node.get("metadata")
            sealed = isinstance(metadata, dict) and metadata.get("encoding") == SEALED
            if sealed and ORG_METADATA not in metadata:
                count += 1
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(json.loads(history.to_json()))
    return count


def _orgs(history: Any) -> set[bytes]:
    """The organisations the exported history's payload metadata name (Temporal exports it as base64)."""
    found: set[bytes] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get(ORG_METADATA)
            if isinstance(value, str):
                found.add(base64.b64decode(value))
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(json.loads(history.to_json()))
    return found


async def test_an_organisations_history_names_it_hides_the_ticket_and_replays() -> None:
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    recorded = await _record(_converter(), "org-a")
    assert _orgs(recorded) == {b"org-a"}
    # the worker's own payloads (activity results, the workflow's result) learned the organisation too
    assert _sealed_without_org(recorded) == 0
    assert TICKET not in recorded.to_json()
    await Replayer(workflows=all_workflows(), data_converter=_converter()).replay_workflow(_history(recorded))


async def test_a_history_sealed_the_adr_0183_way_still_replays() -> None:
    import dataclasses

    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    legacy = dataclasses.replace(DataConverter.default, payload_codec=Adr0183Codec())
    recorded = await _record(legacy, None)
    assert TICKET not in recorded.to_json() and _orgs(recorded) == set()
    await Replayer(workflows=all_workflows(), data_converter=_converter()).replay_workflow(_history(recorded))


async def test_an_organisations_history_does_not_replay_under_another_keyring() -> None:
    from temporalio.worker import Replayer
    from tranibor_orchestrator.extension_parts import all_workflows

    recorded = await _record(_converter(), "org-a")
    stranger = data_converter(
        TemporalSecurity(codec_keys=parse_codec_keys(f"k1:{base64.b64encode(os.urandom(32)).decode()}"))
    )
    with pytest.raises(Exception, match="InvalidTag"):
        await Replayer(workflows=all_workflows(), data_converter=stranger).replay_workflow(_history(recorded))
