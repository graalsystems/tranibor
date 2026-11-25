# SPDX-License-Identifier: Apache-2.0
"""An organisation's Temporal data keys in the database, wrapped by the key wrapper, rotated and shredded
through the API (ADR 0225)."""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from temporalio.api.common.v1 import Payload
from tranibor_api.db.models import AuditLog, Organization, TemporalDataKey
from tranibor_api.db.session import session_scope
from tranibor_api.secret_store import ENV_KEKS, EnvKeyWrapper, parse_keyring
from tranibor_api.services import temporal_keys
from tranibor_core.temporal_connection import (
    KEY_ID_METADATA,
    KEY_SOURCE_METADATA,
    ORG_DATA_KEY,
    AesGcmCodec,
    DataKeyCache,
    ShreddedKeyError,
    install_data_keys,
    organisation_scope,
)

TICKET = "HR-42: salary review for Jane Doe, 81 500 EUR"
MASTER = os.urandom(32)


def _ring(*ids: str) -> str:
    return ",".join(f"{key_id}:{base64.b64encode(os.urandom(32)).decode()}" for key_id in ids)


@pytest.fixture
def keks(monkeypatch: pytest.MonkeyPatch) -> str:
    value = _ring("k1")
    monkeypatch.setenv(ENV_KEKS, value)
    return value


@pytest.fixture(autouse=True)
def _no_installed_keys() -> Any:
    yield
    install_data_keys(None)


async def _org_id(slug: str = "northwind") -> str:
    async with session_scope(orgs="*") as session:
        return (await session.execute(select(Organization.id).where(Organization.slug == slug))).scalar_one()


def _codec(cache: DataKeyCache | None) -> AesGcmCodec:
    return AesGcmCodec([("k1", MASTER)], data_keys=cache)


async def _seal(codec: AesGcmCodec, org_id: str) -> Payload:
    with organisation_scope(org_id):
        (sealed,) = await codec.encode([Payload(metadata={"encoding": b"json/plain"}, data=TICKET.encode())])
    return sealed


async def _open(codec: AesGcmCodec, sealed: Payload) -> bytes:
    (back,) = await codec.decode([sealed])
    return bytes(back.data)


async def test_the_database_holds_a_random_wrapped_key_per_organisation(
    app: Any, org: str, keks: str
) -> None:
    org_id = await _org_id()
    wrapper = EnvKeyWrapper(parse_keyring(keks))  # type: ignore[arg-type]
    source = temporal_keys.DatabaseDataKeys(wrapper)
    key_id, key = await source.active_data_key(org_id)
    assert key_id.startswith("dk-") and len(key) == 32
    assert await source.active_data_key(org_id) == (key_id, key), "made once, then read"
    assert await source.data_key(org_id, key_id) == key
    async with session_scope(orgs="*") as session:
        row = (await session.execute(select(TemporalDataKey))).scalar_one()
    assert row.state == "active" and row.kek_id == "k1" and row.org_id == org_id
    assert row.wrapped_key is not None and key not in row.wrapped_key, "stored wrapped, never in clear"
    # the wrapped key is bound to its organisation and id: moved, it does not open
    with pytest.raises(Exception, match="does not open"):
        wrapper.unwrap(
            row.wrapped_key, row.key_nonce or b"", b"temporal-data-key|other|" + key_id.encode(), "k1"
        )


async def test_encode_shred_and_the_payload_never_decodes_again(
    client: AsyncClient, admin: str, keks: str
) -> None:
    """ADR 0225's proof, through the route an administrator calls."""
    org_id = await _org_id()
    cache = temporal_keys.install_organisation_data_keys()
    assert cache is not None, "a key wrapper is configured: the data keys are installed"
    codec = _codec(cache)
    sealed = await _seal(codec, org_id)
    assert sealed.metadata[KEY_SOURCE_METADATA] == ORG_DATA_KEY and TICKET.encode() not in sealed.data
    assert await _open(codec, sealed) == TICKET.encode()
    assert cache.holds(org_id)

    refused = await client.post(
        "/api/v1/orgs/northwind/temporal-keys/shredding", json={"confirm": "north", "reason": "offboarding"}
    )
    assert refused.status_code == 422, "the slug must be typed again"
    done = await client.post(
        "/api/v1/orgs/northwind/temporal-keys/shredding",
        json={"confirm": "northwind", "reason": "the organisation left"},
    )
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["shredded"] == [sealed.metadata[KEY_ID_METADATA].decode()]
    assert any("search attributes" in line for line in body["not_erased"])
    assert not cache.holds(org_id), "the shredding process dropped the key at once"

    with pytest.raises(ShreddedKeyError, match="shredded"):
        await _open(codec, sealed)
    with pytest.raises(ShreddedKeyError):
        await _open(_codec(DataKeyCache(temporal_keys.DatabaseDataKeys(cache.source.wrapper))), sealed)  # type: ignore[attr-defined]

    async with session_scope(orgs="*") as session:
        row = (await session.execute(select(TemporalDataKey))).scalar_one()
        audit = (
            await session.execute(select(AuditLog).where(AuditLog.action == "org.temporal_keys.shred"))
        ).scalar_one()
    assert row.state == "shredded" and row.wrapped_key is None and row.key_nonce is None
    assert row.shredded_by == admin and row.shredded_at is not None
    assert audit.org_id == org_id and "the organisation left" in str(audit.payload)

    again = await client.post(
        "/api/v1/orgs/northwind/temporal-keys/shredding",
        json={"confirm": "northwind", "reason": "the organisation left"},
    )
    assert again.status_code == 200 and again.json()["shredded"] == [], "idempotent"
    # the organisation's next payload gets a new key, which opens
    fresh = await _seal(codec, org_id)
    assert fresh.metadata[KEY_ID_METADATA] != sealed.metadata[KEY_ID_METADATA]
    assert await _open(codec, fresh) == TICKET.encode()


async def test_rotation_keeps_old_payloads_readable(client: AsyncClient, admin: str, keks: str) -> None:
    org_id = await _org_id()
    cache = temporal_keys.install_organisation_data_keys()
    codec = _codec(cache)
    old = await _seal(codec, org_id)
    rotated = await client.post("/api/v1/orgs/northwind/temporal-keys/rotation")
    assert rotated.status_code == 200, rotated.text
    assert rotated.json()["state"] == "active" and rotated.json()["kek_id"] == "k1"
    new = await _seal(codec, org_id)
    assert new.metadata[KEY_ID_METADATA].decode() == rotated.json()["key_id"]
    assert new.metadata[KEY_ID_METADATA] != old.metadata[KEY_ID_METADATA]
    assert await _open(codec, old) == TICKET.encode() and await _open(codec, new) == TICKET.encode()
    listed = (await client.get("/api/v1/orgs/northwind/temporal-keys")).json()
    assert listed["available"] is True
    assert [k["state"] for k in listed["keys"]] == ["retired", "active"]
    assert "wrapped_key" not in str(listed) and all("key_nonce" not in k for k in listed["keys"])
    async with session_scope(orgs="*") as session:
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "org.temporal_keys.rotate" in actions


async def test_a_member_cannot_list_rotate_or_shred(app: Any, admin: str, keks: str) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as member:
        start = await member.get("/api/v1/auth/login", params={"as": "member@northwind.dev"})
        await member.get(start.headers["location"])
        assert (await member.get("/api/v1/orgs/northwind/temporal-keys")).status_code == 403
        assert (await member.post("/api/v1/orgs/northwind/temporal-keys/rotation")).status_code == 403
        shred = await member.post(
            "/api/v1/orgs/northwind/temporal-keys/shredding", json={"confirm": "northwind", "reason": "xyz"}
        )
        assert shred.status_code == 403


async def test_without_a_key_wrapper_nothing_can_be_shredded(
    client: AsyncClient, admin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_KEKS, raising=False)
    assert temporal_keys.install_organisation_data_keys() is None, "the ADR 0220 derived keys stay"
    listed = (await client.get("/api/v1/orgs/northwind/temporal-keys")).json()
    assert listed == {"org": "northwind", "available": False, "keys": []}
    shred = await client.post(
        "/api/v1/orgs/northwind/temporal-keys/shredding", json={"confirm": "northwind", "reason": "xyz"}
    )
    assert shred.status_code == 409 and "cannot be shredded" in shred.text
    assert (await client.post("/api/v1/orgs/northwind/temporal-keys/rotation")).status_code == 409


async def test_a_kek_rotation_rewraps_the_data_keys(
    client: AsyncClient, admin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`choregos admin secrets rewrap` moves the data keys under the new KEK; the data keys, and so every
    payload, do not change."""
    first = _ring("k1")
    monkeypatch.setenv(ENV_KEKS, first)
    org_id = await _org_id()
    codec = _codec(temporal_keys.install_organisation_data_keys())
    sealed = await _seal(codec, org_id)
    monkeypatch.setenv(ENV_KEKS, _ring("k2") + "," + first)
    done = await client.post("/api/v1/platform/secrets/rewrap")
    assert done.status_code == 200 and done.json() == {"rewrapped": 1}, done.text
    async with session_scope(orgs="*") as session:
        assert (await session.execute(select(TemporalDataKey.kek_id))).scalar_one() == "k2"
    install_data_keys(None)
    fresh = _codec(temporal_keys.install_organisation_data_keys())
    assert await _open(fresh, sealed) == TICKET.encode()
