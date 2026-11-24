"""The API tells the payload codec which organisation a workflow belongs to (ADR 0220)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from tranibor_api.temporal import RealTemporal, organisation_of_payload
from tranibor_core.temporal_connection import (
    ORG_MEMO,
    AesGcmCodec,
    current_organisation,
    organisation_scope,
)


class _Handle:
    id = "wi-p-1"

    def __init__(self, client: _Client) -> None:
        self._client = client

    async def signal(self, name: str, payload: Any) -> None:
        self._client.seen.append(("signal", current_organisation()))

    async def describe(self) -> Any:
        client = self._client

        class Description:
            async def memo(self) -> dict[str, Any]:
                return {ORG_MEMO: "org-from-memo"} if client.memo else {}

        return Description()


class _Client:
    """Records the organisation in scope at each call, as the codec would read it."""

    def __init__(self, *, memo: bool = False) -> None:
        self.data_converter = type("C", (), {"payload_codec": AesGcmCodec([("k1", os.urandom(32))])})()
        self.seen: list[tuple[str, str | None]] = []
        self.memos: list[Any] = []
        self.memo = memo

    async def start_workflow(self, *args: Any, **kwargs: Any) -> _Handle:
        self.seen.append(("start", current_organisation()))
        self.memos.append(kwargs.get("memo"))
        return _Handle(self)

    def get_workflow_handle(self, workflow_id: str) -> _Handle:
        return _Handle(self)


def _real(client: _Client) -> RealTemporal:
    real = RealTemporal("t:7233", "default", "orchestrator")
    real._client = client
    return real


def test_a_start_payload_names_its_organisation_by_org_id() -> None:
    assert organisation_of_payload({"org_id": "o1"}) == "o1"
    assert organisation_of_payload({"org": "a-slug"}) is None
    assert organisation_of_payload({"project_id": "p"}) is None


async def test_a_start_inside_an_organisation_scope_encodes_for_it_and_writes_its_memo() -> None:
    client = _Client()
    with organisation_scope("org-a"):
        await _real(client).start_interpreter("wi-p-1", {"project_id": "p"})
    assert client.seen == [("start", "org-a")] and client.memos == [{ORG_MEMO: "org-a"}]


async def test_a_start_that_names_no_organisation_is_the_platforms() -> None:
    client = _Client()
    await _real(client).start_action("action-1", {"action_id": "a"})
    assert client.seen == [("start", None)] and client.memos == [None]


async def test_a_signal_from_a_process_that_did_not_start_the_workflow_reads_its_memo() -> None:
    client = _Client(memo=True)
    await _real(client).signal("wi-p-1", "decision", {})
    assert client.seen == [("signal", "org-from-memo")]


async def test_a_signal_to_a_workflow_with_no_memo_goes_under_the_platform_key() -> None:
    client = _Client(memo=False)
    await _real(client).signal("wi-p-1", "decision", {})
    assert client.seen == [("signal", None)]


@pytest.mark.parametrize("org", ["bad id", "a|b"])
async def test_an_organisation_id_the_codec_would_refuse_never_starts_a_workflow(org: str) -> None:
    client = _Client()
    with pytest.raises(ValueError, match="organisation id"):
        await _real(client).start_action("action-1", {"org_id": org})
    assert client.seen == []
