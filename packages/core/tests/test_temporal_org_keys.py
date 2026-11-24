"""One key per organisation, derived from the master keyring; no clear payloads in production (ADR 0220)."""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import DataConverter, WorkflowSerializationContext
from tranibor_core import temporal_connection as tc
from tranibor_core.temporal_connection import (
    ENCRYPTED_ENCODING,
    ENV_ALLOW_CLEAR_PAYLOADS,
    KEY_ID_METADATA,
    ORG_METADATA,
    AesGcmCodec,
    TemporalConfigError,
    TemporalSecurity,
    derive_organisation_key,
    organisation_scope,
    parse_codec_keys,
)

MASTER = os.urandom(32)
TICKET = "HR-42: salary review for Jane Doe, 81 500 EUR"


def _codec() -> AesGcmCodec:
    return AesGcmCodec([("k1", MASTER)])


def _payload(text: str = TICKET) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=text.encode())


async def _sealed_for(org: str | None) -> Payload:
    with organisation_scope(org):
        (sealed,) = await _codec().encode([_payload()])
    return sealed


async def test_an_organisations_payload_is_sealed_under_its_derived_key_and_names_it() -> None:
    sealed = await _sealed_for("org-a")
    assert sealed.metadata[ORG_METADATA] == b"org-a" and sealed.metadata[KEY_ID_METADATA] == b"k1"
    assert TICKET.encode() not in sealed.data
    (back,) = await _codec().decode([sealed])
    assert back.data == TICKET.encode()


async def test_organisation_a_payload_cannot_be_opened_with_organisation_b_key() -> None:
    sealed = await _sealed_for("org-a")
    nonce, body = sealed.data[:12], sealed.data[12:]
    key_b = derive_organisation_key(MASTER, "org-b")
    for associated in (b"k1|org|org-a", b"k1|org|org-b", b"k1"):
        with pytest.raises(InvalidTag):
            AESGCM(key_b).decrypt(nonce, body, associated)
    # nor with the master key, which is the platform's
    with pytest.raises(InvalidTag):
        AESGCM(MASTER).decrypt(nonce, body, b"k1")
    # and a payload relabelled as org B's is refused by the codec itself
    relabelled = Payload(metadata={**sealed.metadata, ORG_METADATA: b"org-b"}, data=sealed.data)
    with pytest.raises(InvalidTag):
        await _codec().decode([relabelled])


def test_the_keys_are_distinct_per_organisation_and_per_master_and_never_the_master() -> None:
    a, b = derive_organisation_key(MASTER, "org-a"), derive_organisation_key(MASTER, "org-b")
    assert len({a, b, MASTER}) == 3 and len(a) == 32
    assert a == derive_organisation_key(MASTER, "org-a")
    assert a != derive_organisation_key(os.urandom(32), "org-a")


async def test_a_payload_of_no_organisation_is_the_platforms_master_key_payload() -> None:
    sealed = await _sealed_for(None)
    assert ORG_METADATA not in sealed.metadata
    plain = AESGCM(MASTER).decrypt(sealed.data[:12], sealed.data[12:], b"k1")
    assert Payload.FromString(plain).data == TICKET.encode()


async def test_a_payload_sealed_before_adr_0220_still_decodes() -> None:
    """ADR 0183's format, rebuilt by hand: the master key, the key id as associated data, no organisation."""
    nonce = os.urandom(12)
    legacy = Payload(
        metadata={"encoding": ENCRYPTED_ENCODING, KEY_ID_METADATA: b"k1"},
        data=nonce + AESGCM(MASTER).encrypt(nonce, _payload().SerializeToString(), b"k1"),
    )
    (back,) = await _codec().decode([legacy])
    assert back.data == TICKET.encode()
    (clear,) = await _codec().decode([_payload()])
    assert clear.data == TICKET.encode(), "a clear payload decodes as it did"


async def test_rotation_keeps_old_organisation_payloads_readable() -> None:
    sealed = await _sealed_for("org-a")
    rotated = AesGcmCodec([("k2", os.urandom(32)), ("k1", MASTER)])
    (back,) = await rotated.decode([sealed])
    assert back.data == TICKET.encode()
    with organisation_scope("org-a"):
        (fresh,) = await rotated.encode([_payload()])
    assert fresh.metadata[KEY_ID_METADATA] == b"k2"


async def test_a_worker_learns_an_organisation_from_the_input_it_decodes() -> None:
    """No context is passed to a worker: what it decodes under a workflow id names the key it answers with."""
    codec = _codec()
    context = WorkflowSerializationContext(namespace="default", workflow_id="wi-p-1")
    bound = codec.with_context(context)
    (sealed_in,) = await _sealed_for_org_with(codec.with_context(context), "org-a")
    worker_side = _codec()
    worker_bound = worker_side.with_context(context)
    await worker_bound.decode([sealed_in])
    (answer,) = await worker_bound.encode([_payload("result")])
    assert answer.metadata[ORG_METADATA] == b"org-a"
    other = worker_side.with_context(WorkflowSerializationContext(namespace="default", workflow_id="wi-q-9"))
    (platform,) = await other.encode([_payload("result")])
    assert ORG_METADATA not in platform.metadata
    assert bound is not codec


async def _sealed_for_org_with(codec: AesGcmCodec, org: str) -> list[Payload]:
    with organisation_scope(org):
        return await codec.encode([_payload()])


def test_an_organisation_id_that_could_smuggle_a_separator_is_refused() -> None:
    with pytest.raises(ValueError, match="organisation id"), organisation_scope("a|org|b c"):
        pass


async def test_no_key_appears_in_a_failure_message() -> None:
    sealed = await _sealed_for("org-a")
    other = AesGcmCodec([("k1", os.urandom(32))])
    with pytest.raises(InvalidTag) as failure:
        await other.decode([sealed])
    assert base64.b64encode(MASTER).decode() not in repr(failure.value)
    assert not any(isinstance(v, bytes) and v == MASTER for v in vars(failure.value).values())


def _security(*, keys: bool, allow: bool = False) -> TemporalSecurity:
    ring = parse_codec_keys(f"k1:{base64.b64encode(MASTER).decode()}") if keys else ()
    return TemporalSecurity(codec_keys=ring, allow_clear=allow)


@pytest.mark.parametrize("environment", ["staging", "prod", " Prod "])
def test_production_refuses_to_start_without_encryption(environment: str) -> None:
    with pytest.raises(TemporalConfigError, match=ENV_ALLOW_CLEAR_PAYLOADS):
        _security(keys=False).refuse_clear_in_production(environment)


@pytest.mark.parametrize("environment", ["dev", "test", None, ""])
def test_dev_and_test_start_without_a_key(environment: str | None) -> None:
    _security(keys=False).refuse_clear_in_production(environment)


def test_production_starts_with_a_key_or_the_explicit_opt_out() -> None:
    _security(keys=True).refuse_clear_in_production("prod")
    _security(keys=False, allow=True).refuse_clear_in_production("prod")


def test_the_opt_out_is_read_from_the_environment() -> None:
    assert not TemporalSecurity.from_env({}).allow_clear
    assert TemporalSecurity.from_env({ENV_ALLOW_CLEAR_PAYLOADS: "true"}).allow_clear
    with pytest.raises(TemporalConfigError):
        tc.refuse_clear_payloads(env={"TRANIBOR_ENV": "prod"})
    tc.refuse_clear_payloads(env={"TRANIBOR_ENV": "prod", ENV_ALLOW_CLEAR_PAYLOADS: "1"})


async def test_connect_temporal_refuses_before_it_connects(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Any] = []

    async def connect(*args: Any, **kwargs: Any) -> object:
        calls.append(kwargs)
        return object()

    monkeypatch.setattr(tc.Client, "connect", connect)
    monkeypatch.setenv("TRANIBOR_ENV", "prod")
    with pytest.raises(TemporalConfigError):
        await tc.connect_temporal("t:7233", "default", security=_security(keys=False))
    assert calls == []
    await tc.connect_temporal("t:7233", "default", security=_security(keys=True))
    assert len(calls) == 1
    assert DataConverter.default is not calls[0]["data_converter"]
