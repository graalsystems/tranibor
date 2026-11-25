# SPDX-License-Identifier: Apache-2.0
"""An organisation's Temporal data keys (ADR 0225): random, wrapped by the deployment's key wrapper,
rotated and shredded per organisation.

- Each organisation gets a random 32-byte data key the first time one of its payloads is sealed. It is
  stored wrapped in `temporal_data_keys` by the `KeyWrapper` of ADR 0103: the `TRANIBOR_SECRETS_KEKS`
  keyring by default, Vault transit when the `vault-transit` extension is configured. The additional
  data `temporal-data-key|<org id>|<key id>` binds a wrapped key to its organisation and id.
- `DatabaseDataKeys` is the core codec's `DataKeySource`: it unwraps a key when the codec asks for it, and
  the core's `DataKeyCache` keeps it for `TRANIBOR_TEMPORAL_DATA_KEY_TTL_S` (300 s by default).
- `rotate_data_key` retires the active key and makes a new one: new payloads go under the new key, what the
  retired key sealed still opens. `shred_data_keys` destroys every wrapped key of the organisation: what
  they sealed cannot be read again, by anyone.
- `install_organisation_data_keys` is what the API's lifespan and the worker's start call: with a key
  wrapper, the data keys; without one, the organisation keys stay the ones ADR 0220 derives, and shredding
  answers that it cannot.

The wrapper's protocol is synchronous (ADR 0103) and a Vault wrapper makes a network call: every wrap and
unwrap runs in a thread, so that a worker's event loop is not held by Vault.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_core import utcnow
from tranibor_core.temporal_connection import (
    KEY_BYTES,
    DataKeyCache,
    ShreddedKeyError,
    UnknownCodecKeyError,
    install_data_keys,
    installed_data_keys,
)

from ..db.models import TemporalDataKey
from ..db.session import ALL_ORGS, after_commit, session_scope
from ..logging import get_logger
from ..secret_store import KeyWrapper, SecretStoreUnavailable, active_key_wrapper

logger = get_logger("choregos.temporal_keys")

ACTIVE = "active"
RETIRED = "retired"
SHREDDED = "shredded"
#: What shredding does not erase: said by the API, the CLI and the documentation in the same words.
NOT_ERASED = (
    "Temporal's own metadata: workflow ids and types, task queues, timestamps, run status",
    "search attributes and the workflow memo (the organisation id is in it and in each payload's metadata)",
    "payloads sealed before ADR 0225 (under the ADR 0220 derived key or the master key)",
    "payloads of workflows that name no organisation (under the master key)",
    "database backups taken before the shredding, which still hold the wrapped key",
    "a key another process still holds in memory, for TRANIBOR_TEMPORAL_DATA_KEY_TTL_S at most",
)
NO_WRAPPER = (
    "no key wrapper is configured (TRANIBOR_SECRETS_KEKS, or the vault-transit extension): the "
    "organisation's Temporal keys are derived from the master key (ADR 0220) and cannot be shredded"
)


class TemporalKeysUnavailable(RuntimeError):  # noqa: N818 - a named lack, rendered as is (409)
    """No key wrapper: the deployment cannot hold per-organisation data keys."""


def associated_data(org_id: str, key_id: str) -> bytes:
    return f"temporal-data-key|{org_id}|{key_id}".encode()


def new_key_id() -> str:
    return "dk-" + os.urandom(8).hex()


def available() -> bool:
    """Whether the deployment can hold data keys: a key wrapper is configured."""
    return active_key_wrapper() is not None


def _wrapper() -> KeyWrapper:
    wrapper = active_key_wrapper()
    if wrapper is None:
        raise TemporalKeysUnavailable(NO_WRAPPER)
    return wrapper


async def _unwrap(wrapper: KeyWrapper, row: TemporalDataKey) -> bytes:
    if row.wrapped_key is None:
        raise ShreddedKeyError(_shredded(row.org_id, row.key_id, row.shredded_at))
    key = await asyncio.to_thread(
        wrapper.unwrap,
        row.wrapped_key,
        row.key_nonce or b"",
        associated_data(row.org_id, row.key_id),
        row.kek_id,
    )
    if len(key) != KEY_BYTES:
        raise UnknownCodecKeyError(f"the Temporal data key `{row.key_id}` does not unwrap to 32 bytes")
    return key


def _shredded(org_id: str, key_id: str, at: datetime | None) -> str:
    when = f" on {at.isoformat(timespec='seconds')}" if at is not None else ""
    return (
        f"the Temporal data key `{key_id}` of the organisation `{org_id}` was shredded{when}: "
        "what it sealed cannot be read (ADR 0225)"
    )


async def create_data_key(
    session: AsyncSession, org_id: str, wrapper: KeyWrapper, *, actor: str | None
) -> tuple[TemporalDataKey, bytes]:
    """A new active data key for the organisation, wrapped; returns the row and the key."""
    key, key_id = os.urandom(KEY_BYTES), new_key_id()
    wrapped, nonce, kek_id = await asyncio.to_thread(wrapper.wrap, key, associated_data(org_id, key_id))
    row = TemporalDataKey(
        org_id=org_id,
        key_id=key_id,
        state=ACTIVE,
        wrapped_key=wrapped,
        key_nonce=nonce,
        kek_id=kek_id,
        created_by=actor,
    )
    session.add(row)
    await session.flush()
    return row, key


async def list_data_keys(session: AsyncSession, org_id: str) -> list[TemporalDataKey]:
    return list(
        (
            await session.execute(
                select(TemporalDataKey)
                .where(TemporalDataKey.org_id == org_id)
                .order_by(TemporalDataKey.created_at, TemporalDataKey.id)
            )
        ).scalars()
    )


async def _active_row(session: AsyncSession, org_id: str) -> TemporalDataKey | None:
    """The newest active key: two processes that made one at the same moment agree on the later."""
    return (
        (
            await session.execute(
                select(TemporalDataKey)
                .where(TemporalDataKey.org_id == org_id, TemporalDataKey.state == ACTIVE)
                .order_by(TemporalDataKey.created_at.desc(), TemporalDataKey.id.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


def _forget(session: AsyncSession, org_id: str) -> None:
    """This process drops the organisation's keys now, and again once the change is committed (a payload
    sealed in between would otherwise have cached the old key again)."""
    cache = installed_data_keys()
    if cache is None:
        return
    cache.forget(org_id)

    async def again() -> None:
        cache.forget(org_id)

    after_commit(session, again)


async def rotate_data_key(session: AsyncSession, org_id: str, *, actor: str | None) -> TemporalDataKey:
    """Retire the organisation's active keys and make a new one: new payloads go under it, what the retired
    keys sealed still opens. Other processes move to it when their cached key expires."""
    wrapper = _wrapper()
    for row in await list_data_keys(session, org_id):
        if row.state == ACTIVE:
            row.state = RETIRED
    row, _ = await create_data_key(session, org_id, wrapper, actor=actor)
    _forget(session, org_id)
    return row


async def shred_data_keys(session: AsyncSession, org_id: str, *, actor: str | None) -> list[str]:
    """Destroy every wrapped data key of the organisation (crypto-shredding): the payloads they sealed cannot
    be read again. Returns the ids shredded now. The rows stay as tombstones; the organisation's next
    payload gets a new key."""
    _wrapper()  # without a wrapper there is nothing to shred: the keys are derived (ADR 0220)
    shredded: list[str] = []
    now = utcnow()
    for row in await list_data_keys(session, org_id):
        if row.state == SHREDDED:
            continue
        row.wrapped_key, row.key_nonce = None, None
        row.state, row.shredded_at, row.shredded_by = SHREDDED, now, actor
        shredded.append(row.key_id)
    await session.flush()
    _forget(session, org_id)
    return shredded


async def rewrap_data_keys(session: AsyncSession, wrapper: KeyWrapper) -> int:
    """After a rotation of the key wrapper's key: re-wrap every live data key under the active one. The data
    keys themselves do not change, so no payload is touched. Returns the rows changed."""
    active = await asyncio.to_thread(lambda: wrapper.active_id)
    rows: Sequence[TemporalDataKey] = list(
        (
            await session.execute(
                select(TemporalDataKey).where(
                    TemporalDataKey.state != SHREDDED, TemporalDataKey.kek_id != active
                )
            )
        ).scalars()
    )
    for row in rows:
        key = await _unwrap(wrapper, row)
        aad = associated_data(row.org_id, row.key_id)
        row.wrapped_key, row.key_nonce, row.kek_id = await asyncio.to_thread(wrapper.wrap, key, aad)
    await session.flush()
    return len(rows)


class DatabaseDataKeys:
    """The core codec's `DataKeySource` over `temporal_data_keys`, read in the platform scope."""

    def __init__(self, wrapper: KeyWrapper) -> None:
        self.wrapper = wrapper

    async def active_data_key(self, org_id: str) -> tuple[str, bytes]:
        async with session_scope(orgs=ALL_ORGS) as session:
            row = await _active_row(session, org_id)
            if row is None:
                row, key = await create_data_key(session, org_id, self.wrapper, actor=None)
                logger.info("temporal.data_key.created", org=org_id, key_id=row.key_id, kek_id=row.kek_id)
                return row.key_id, key
            return row.key_id, await self._open(row)

    async def data_key(self, org_id: str, key_id: str) -> bytes:
        async with session_scope(orgs=ALL_ORGS) as session:
            row = (
                await session.execute(
                    select(TemporalDataKey).where(
                        TemporalDataKey.org_id == org_id, TemporalDataKey.key_id == key_id
                    )
                )
            ).scalar_one_or_none()
        if row is None:
            raise UnknownCodecKeyError(
                f"the organisation `{org_id}` has no Temporal data key `{key_id}` (deleted with it, or "
                "another deployment's)"
            )
        if row.state == SHREDDED:
            raise ShreddedKeyError(_shredded(org_id, key_id, row.shredded_at))
        return await self._open(row)

    async def _open(self, row: TemporalDataKey) -> bytes:
        try:
            return await _unwrap(self.wrapper, row)
        except SecretStoreUnavailable as refusal:
            raise UnknownCodecKeyError(f"the Temporal data key `{row.key_id}`: {refusal}") from None


def install_organisation_data_keys(wrapper: KeyWrapper | None = None) -> DataKeyCache | None:
    """Per-organisation data keys for every Temporal client of this process, when the deployment has a key
    wrapper (ADR 0225); otherwise the ADR 0220 derived keys, said once in the log."""
    chosen = wrapper if wrapper is not None else active_key_wrapper()
    if chosen is None:
        install_data_keys(None)
        logger.warning("temporal.data_keys.derived", detail=NO_WRAPPER)
        return None
    cache = install_data_keys(DatabaseDataKeys(chosen))
    logger.info("temporal.data_keys.installed", wrapper=type(chosen).__name__)
    return cache


__all__ = [
    "ACTIVE",
    "NOT_ERASED",
    "NO_WRAPPER",
    "RETIRED",
    "SHREDDED",
    "DatabaseDataKeys",
    "TemporalKeysUnavailable",
    "associated_data",
    "available",
    "create_data_key",
    "install_organisation_data_keys",
    "list_data_keys",
    "new_key_id",
    "rewrap_data_keys",
    "rotate_data_key",
    "shred_data_keys",
]
