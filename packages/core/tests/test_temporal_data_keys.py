"""An organisation's Temporal payloads under its own random data key, rotated and shredded (ADR 0225).

The source here is an in-memory stand-in for the API's table of wrapped keys: what the codec needs from it
is a key per id, the active one, and a refusal for a shredded one.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from temporalio.api.common.v1 import Payload
from temporalio.converter import WorkflowSerializationContext
from tranibor_core import temporal_connection as tc
from tranibor_core.temporal_connection import (
    ENCRYPTED_ENCODING,
    ENV_DATA_KEY_TTL,
    KEY_ID_METADATA,
    KEY_SOURCE_METADATA,
    ORG_DATA_KEY,
    ORG_METADATA,
    AesGcmCodec,
    DataKeyCache,
    ShreddedKeyError,
    TemporalConfigError,
    TemporalSecurity,
    UnknownCodecKeyError,
    data_converter,
    install_data_keys,
    installed_data_keys,
    organisation_scope,
)

MASTER = os.urandom(32)
TICKET = "HR-42: salary review for Jane Doe, 81 500 EUR"


class MemoryKeys:
    """Keys per organisation: `(key id, key, state)`, the last active one seals."""

    def __init__(self) -> None:
        self.keys: dict[str, list[list[Any]]] = {}
        self.calls = 0
        self._next = 0

    def _new(self, org_id: str) -> tuple[str, bytes]:
        self._next += 1
        key_id, key = f"dk-{self._next:016x}", os.urandom(32)
        self.keys.setdefault(org_id, []).append([key_id, key, "active"])
        return key_id, key

    async def active_data_key(self, org_id: str) -> tuple[str, bytes]:
        self.calls += 1
        live = [entry for entry in self.keys.get(org_id, []) if entry[2] == "active"]
        if not live:
            return self._new(org_id)
        return live[-1][0], live[-1][1]

    async def data_key(self, org_id: str, key_id: str) -> bytes:
        self.calls += 1
        for found_id, key, state in self.keys.get(org_id, []):
            if found_id == key_id:
                if state == "shredded":
                    raise ShreddedKeyError(f"`{key_id}` of `{org_id}` was shredded")
                return bytes(key)
        raise UnknownCodecKeyError(f"`{org_id}` has no data key `{key_id}`")

    def rotate(self, org_id: str) -> str:
        for entry in self.keys.get(org_id, []):
            if entry[2] == "active":
                entry[2] = "retired"
        return self._new(org_id)[0]

    def shred(self, org_id: str) -> None:
        for entry in self.keys.get(org_id, []):
            entry[1], entry[2] = b"", "shredded"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _payload(text: str = TICKET) -> Payload:
    return Payload(metadata={"encoding": b"json/plain"}, data=text.encode())


def _codec(cache: DataKeyCache | None) -> AesGcmCodec:
    return AesGcmCodec([("k1", MASTER)], data_keys=cache)


async def _seal(codec: AesGcmCodec, org: str | None) -> Payload:
    with organisation_scope(org):
        (sealed,) = await codec.encode([_payload()])
    return sealed


async def _open(codec: AesGcmCodec, sealed: Payload) -> bytes:
    (back,) = await codec.decode([sealed])
    return bytes(back.data)


async def test_an_organisations_payload_is_sealed_under_its_data_key_and_names_it() -> None:
    source = MemoryKeys()
    codec = _codec(DataKeyCache(source))
    sealed = await _seal(codec, "org-a")
    key_id = source.keys["org-a"][0][0]
    assert sealed.metadata["encoding"] == ENCRYPTED_ENCODING
    assert sealed.metadata[KEY_SOURCE_METADATA] == ORG_DATA_KEY
    assert sealed.metadata[KEY_ID_METADATA] == key_id.encode()
    assert sealed.metadata[ORG_METADATA] == b"org-a"
    assert TICKET.encode() not in sealed.data
    assert await _open(codec, sealed) == TICKET.encode()
    # neither the master key nor the key ADR 0220 derives opens it
    derived = tc.derive_organisation_key(MASTER, "org-a")
    nonce, body = sealed.data[:12], sealed.data[12:]
    for key in (MASTER, derived):
        with pytest.raises(InvalidTag):
            AESGCM(key).decrypt(nonce, body, f"{key_id}|org-data-key|org-a".encode())


async def test_two_organisations_get_two_keys_and_the_platform_keeps_the_master_key() -> None:
    source = MemoryKeys()
    codec = _codec(DataKeyCache(source))
    a, b, platform = await _seal(codec, "org-a"), await _seal(codec, "org-b"), await _seal(codec, None)
    assert a.metadata[KEY_ID_METADATA] != b.metadata[KEY_ID_METADATA]
    assert KEY_SOURCE_METADATA not in platform.metadata and platform.metadata[KEY_ID_METADATA] == b"k1"
    # the platform's payload is exactly ADR 0183's: a codec without data keys opens it
    assert await _open(_codec(None), platform) == TICKET.encode()


async def test_shredding_makes_the_organisations_payloads_unreadable() -> None:
    """The proof ADR 0225 asks for: encode, shred, fail to decode. Organisation B is untouched."""
    source = MemoryKeys()
    cache = DataKeyCache(source)
    codec = _codec(cache)
    sealed_a, sealed_b = await _seal(codec, "org-a"), await _seal(codec, "org-b")
    assert await _open(codec, sealed_a) == TICKET.encode()

    source.shred("org-a")
    cache.forget("org-a")  # what the shredding process does at once
    with pytest.raises(ShreddedKeyError, match="shredded"):
        await _open(codec, sealed_a)
    with pytest.raises(ShreddedKeyError):  # a process that never held the key either
        await _open(_codec(DataKeyCache(source)), sealed_a)
    assert await _open(codec, sealed_b) == TICKET.encode()


async def test_a_process_that_did_not_shred_stops_reading_when_its_cached_key_expires() -> None:
    source, clock = MemoryKeys(), Clock()
    other = _codec(DataKeyCache(source, ttl_s=300, clock=clock))
    sealed = await _seal(other, "org-a")
    source.shred("org-a")
    clock.now = 299.0
    assert await _open(other, sealed) == TICKET.encode(), "inside the window: still cached (documented)"
    clock.now = 300.0
    with pytest.raises(ShreddedKeyError):
        await _open(other, sealed)


async def test_after_shredding_the_organisation_gets_a_new_key() -> None:
    source = MemoryKeys()
    cache = DataKeyCache(source)
    codec = _codec(cache)
    before = await _seal(codec, "org-a")
    source.shred("org-a")
    cache.forget("org-a")
    after = await _seal(codec, "org-a")
    assert after.metadata[KEY_ID_METADATA] != before.metadata[KEY_ID_METADATA]
    assert await _open(codec, after) == TICKET.encode()


async def test_rotation_seals_under_the_new_key_and_old_payloads_still_open() -> None:
    source = MemoryKeys()
    cache = DataKeyCache(source)
    codec = _codec(cache)
    old = await _seal(codec, "org-a")
    new_id = source.rotate("org-a")
    cache.forget("org-a")
    new = await _seal(codec, "org-a")
    assert new.metadata[KEY_ID_METADATA] == new_id.encode() != old.metadata[KEY_ID_METADATA]
    fresh = _codec(DataKeyCache(source))
    assert await _open(fresh, old) == TICKET.encode()
    assert await _open(fresh, new) == TICKET.encode()


async def test_payloads_of_adr_0220_and_adr_0183_still_decode_with_data_keys() -> None:
    """The migration path: nothing is re-encrypted, the metadata says which key to use."""
    derived = await _seal(_codec(None), "org-a")  # ADR 0220: derived key, no key source
    assert KEY_SOURCE_METADATA not in derived.metadata and derived.metadata[ORG_METADATA] == b"org-a"
    master = await _seal(_codec(None), None)  # ADR 0183: master key
    source = MemoryKeys()
    codec = _codec(DataKeyCache(source))
    assert await _open(codec, derived) == TICKET.encode()
    assert await _open(codec, master) == TICKET.encode()
    assert source.calls == 0, "an old payload does not ask the key source"
    # and shredding the organisation does not reach them: the master key derives the ADR 0220 key
    source.shred("org-a")
    assert await _open(codec, derived) == TICKET.encode()


async def test_a_relabelled_payload_does_not_open() -> None:
    source = MemoryKeys()
    codec = _codec(DataKeyCache(source))
    await _seal(codec, "org-b")  # org-b has a key too
    sealed = await _seal(codec, "org-a")

    as_b = Payload()
    as_b.CopyFrom(sealed)
    as_b.metadata[ORG_METADATA] = b"org-b"
    as_b.metadata[KEY_ID_METADATA] = source.keys["org-b"][0][0].encode()
    with pytest.raises(InvalidTag):
        await _open(codec, as_b)

    as_derived = Payload()
    as_derived.CopyFrom(sealed)
    del as_derived.metadata[KEY_SOURCE_METADATA]
    with pytest.raises(UnknownCodecKeyError):  # its key id is not a master key id
        await _open(codec, as_derived)
    as_derived.metadata[KEY_ID_METADATA] = b"k1"
    with pytest.raises(InvalidTag):
        await _open(codec, as_derived)

    unknown_kind = Payload()
    unknown_kind.CopyFrom(sealed)
    unknown_kind.metadata[KEY_SOURCE_METADATA] = b"kms-v9"
    with pytest.raises(UnknownCodecKeyError, match="kind of key"):
        await _open(codec, unknown_kind)


async def test_a_process_without_a_key_source_says_so_and_names_no_key() -> None:
    source = MemoryKeys()
    sealed = await _seal(_codec(DataKeyCache(source)), "org-a")
    with pytest.raises(UnknownCodecKeyError, match="no organisation key source") as refusal:
        await _open(_codec(None), sealed)
    key = source.keys["org-a"][0][1]
    message = str(refusal.value)
    assert key.hex() not in message and MASTER.hex() not in message


async def test_the_cache_asks_the_source_once_per_key_and_not_again_after_a_refusal() -> None:
    source = MemoryKeys()
    codec = _codec(DataKeyCache(source))
    sealed = [await _seal(codec, "org-a") for _ in range(5)]
    for one in sealed:
        await _open(codec, one)
    assert source.calls == 1, "the active key also serves the decode of what it sealed"
    other = _codec(DataKeyCache(source))
    source.shred("org-a")
    for _ in range(2):
        with pytest.raises(ShreddedKeyError):
            await _open(other, sealed[0])
    assert source.calls == 3, "a refusal is not remembered: the next payload asks again"


async def test_a_bound_codec_shares_the_data_keys_and_learns_the_organisation() -> None:
    source = MemoryKeys()
    codec = _codec(DataKeyCache(source))
    bound = codec.with_context(WorkflowSerializationContext(namespace="default", workflow_id="wf-1"))
    first = await _seal(bound, "org-a")
    (reply,) = await bound.encode([_payload("the workflow's reply")])
    assert reply.metadata[ORG_METADATA] == b"org-a" and reply.metadata[KEY_SOURCE_METADATA] == ORG_DATA_KEY
    assert reply.metadata[KEY_ID_METADATA] == first.metadata[KEY_ID_METADATA]
    assert source.calls == 1


async def test_a_malformed_key_from_the_source_is_refused() -> None:
    class Broken(MemoryKeys):
        async def active_data_key(self, org_id: str) -> tuple[str, bytes]:
            return "dk-1", b"short"

    with pytest.raises(TemporalConfigError, match="malformed"):
        await _seal(_codec(DataKeyCache(Broken())), "org-a")


def test_installed_keys_reach_every_converter_of_the_process(monkeypatch: pytest.MonkeyPatch) -> None:
    source = MemoryKeys()
    try:
        cache = install_data_keys(source, env={ENV_DATA_KEY_TTL: "60"})
        assert cache is not None and installed_data_keys() is cache
        security = TemporalSecurity.from_env({tc.ENV_CODEC_KEYS: "k1:" + _b64(MASTER)})
        assert security.data_keys is cache
        codec = data_converter(security).payload_codec
        assert isinstance(codec, AesGcmCodec) and codec.data_keys is cache
        assert install_data_keys(None) is None and installed_data_keys() is None
        assert TemporalSecurity.from_env({tc.ENV_CODEC_KEYS: "k1:" + _b64(MASTER)}).data_keys is None
    finally:
        install_data_keys(None)


@pytest.mark.parametrize("value", ["soon", "-1", "100000"])
def test_a_wrong_ttl_is_refused(value: str) -> None:
    try:
        with pytest.raises(TemporalConfigError, match=ENV_DATA_KEY_TTL):
            install_data_keys(MemoryKeys(), env={ENV_DATA_KEY_TTL: value})
    finally:
        install_data_keys(None)


def test_forget_without_an_organisation_drops_every_key() -> None:
    cache = DataKeyCache(MemoryKeys())
    cache._active["org-a"] = (0.0, "dk-1", AESGCM(os.urandom(32)))
    cache._keys[("org-b", "dk-2")] = (0.0, AESGCM(os.urandom(32)))
    assert cache.holds("org-a") and cache.holds("org-b")
    cache.forget()
    assert not cache.holds("org-a") and not cache.holds("org-b")


def _b64(key: bytes) -> str:
    import base64

    return base64.b64encode(key).decode()
