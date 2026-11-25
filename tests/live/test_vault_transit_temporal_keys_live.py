"""Live: an organisation's Temporal data keys wrapped by a real HashiCorp Vault transit engine (ADR 0224).

    docker run -d --name vault-dev -p 8200:8200 -e VAULT_DEV_ROOT_TOKEN_ID=root hashicorp/vault:1.18.5
    docker exec -e VAULT_ADDR=http://127.0.0.1:8200 -e VAULT_TOKEN=root vault-dev vault secrets enable transit
    TRANIBOR_LIVE_VAULT_ADDR=http://127.0.0.1:8200 \\
    uv run pytest tests/live/test_vault_transit_temporal_keys_live.py -m live -rs -s

`TRANIBOR_LIVE_VAULT_TOKEN` defaults to `root`, the root token a Vault **dev server** is started with
above, and nothing else: against any other Vault, set a token whose policy allows `transit/keys/*`
(create, rotate, configure, delete) and `transit/{encrypt,decrypt,rewrap}/*`. The test makes its own
transit keys, one plain and one `derived=true`, and deletes them at the end. Skipped without
`TRANIBOR_LIVE_VAULT_ADDR`; failed, not skipped, when `TRANIBOR_LIVE_REQUIRED` names `vault`.

What runs for real: the API with the `vault-transit` extension's store (`set_secret_store`, as its
`register()` does), the payload codec, `temporal_data_keys` in a database, and the routes an administrator
calls. A key is wrapped by Vault, a payload sealed and opened through a fresh cache (so through Vault's
`decrypt`), the transit key rotated and the data keys moved under its new version by
`POST /platform/secrets/rewrap` (unwrapped by Vault, wrapped again by Vault), the organisation's data key
rotated, then shredded: the payloads no longer decode.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from httpx import AsyncClient
from tranibor_testkit.api import admin, app, client, org  # noqa: F401 - the API suite's fixtures

from .conftest import require

pytestmark = pytest.mark.live

TICKET = "HR-42: salary review for Jane Doe, 81 500 EUR"
MASTER = os.urandom(32)


class VaultAdmin:
    """The operator's side of Vault: making, rotating and deleting the test's transit keys."""

    def __init__(self, address: str, token: str) -> None:
        self.http = httpx.AsyncClient(base_url=address.rstrip("/"), headers={"X-Vault-Token": token})

    async def create(self, name: str, *, derived: bool) -> None:
        response = await self.http.post(f"/v1/transit/keys/{name}", json={"derived": derived})
        response.raise_for_status()

    async def rotate(self, name: str) -> None:
        (await self.http.post(f"/v1/transit/keys/{name}/rotate")).raise_for_status()

    async def version(self, name: str) -> int:
        response = await self.http.get(f"/v1/transit/keys/{name}")
        response.raise_for_status()
        return int(response.json()["data"]["latest_version"])

    async def delete(self, name: str) -> None:
        await self.http.post(f"/v1/transit/keys/{name}/config", json={"deletion_allowed": True})
        await self.http.delete(f"/v1/transit/keys/{name}")

    async def aclose(self) -> None:
        await self.http.aclose()


@pytest.fixture
async def vault() -> AsyncIterator[tuple[str, str, VaultAdmin]]:
    env = require("TRANIBOR_LIVE_VAULT_ADDR")
    address = env["TRANIBOR_LIVE_VAULT_ADDR"]
    # Dev-only default: the root token `docker run ... -e VAULT_DEV_ROOT_TOKEN_ID=root` gives a dev server.
    token = os.environ.get("TRANIBOR_LIVE_VAULT_TOKEN", "root")
    operator = VaultAdmin(address, token)
    try:
        yield address, token, operator
    finally:
        await operator.aclose()


async def _org_id() -> str:
    from sqlalchemy import select
    from tranibor_api.db.models import Organization
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        found = await session.execute(select(Organization.id).where(Organization.slug == "northwind"))
        return str(found.scalar_one())


async def _row() -> Any:
    from sqlalchemy import select
    from tranibor_api.db.models import TemporalDataKey
    from tranibor_api.db.session import session_scope

    async with session_scope(orgs="*") as session:
        return list(
            (await session.execute(select(TemporalDataKey).order_by(TemporalDataKey.created_at))).scalars()
        )


@pytest.mark.parametrize("derived", [False, True], ids=["plain-key", "derived-key-bound-context"])
async def test_vault_wraps_rotates_and_shreds_an_organisations_temporal_keys(
    vault: tuple[str, str, VaultAdmin],
    client: AsyncClient,  # noqa: F811 - the fixture
    admin: str,  # noqa: F811 - the fixture
    derived: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from temporalio.api.common.v1 import Payload
    from tranibor_api.secret_store import ENV_KEKS, set_secret_store
    from tranibor_api.services import temporal_keys
    from tranibor_core.temporal_connection import (
        KEY_ID_METADATA,
        AesGcmCodec,
        DataKeyCache,
        ShreddedKeyError,
        install_data_keys,
        organisation_scope,
    )
    from tranibor_vault_transit.auth import TokenAuth
    from tranibor_vault_transit.client import VaultTransitClient
    from tranibor_vault_transit.wrapper import VaultTransitKeyWrapper, VaultTransitSecretStore

    address, token, operator = vault
    key = f"choregos-t-{os.urandom(4).hex()}"
    await operator.create(key, derived=derived)
    monkeypatch.delenv(ENV_KEKS, raising=False)
    wrapper = VaultTransitKeyWrapper(
        VaultTransitClient(address, TokenAuth(token, None), key), bind_context=derived, refresh_s=0.0
    )
    set_secret_store(VaultTransitSecretStore(wrapper))
    report: list[str] = [f"vault {address}, transit key {key} (derived={derived})"]
    try:
        org_id = await _org_id()
        cache = temporal_keys.install_organisation_data_keys()
        assert cache is not None, "the extension's wrapper is the deployment's key wrapper"

        def codec(keys: DataKeyCache | None = None) -> AesGcmCodec:
            return AesGcmCodec([("k1", MASTER)], data_keys=keys or DataKeyCache(cache.source))

        async def seal(with_codec: AesGcmCodec) -> Payload:
            with organisation_scope(org_id):
                (sealed,) = await with_codec.encode(
                    [Payload(metadata={"encoding": b"json/plain"}, data=TICKET.encode())]
                )
            return sealed

        async def opened(sealed: Payload) -> bytes:
            (back,) = await codec().decode([sealed])  # a fresh cache: the key comes from Vault again
            return bytes(back.data)

        # 1. wrap: the organisation's first payload makes its data key, wrapped by Vault
        first = await seal(codec(cache))
        (row,) = await _row()
        assert row.kek_id == f"{key}-v1" and row.wrapped_key.startswith(b"vault:v1:")
        assert TICKET.encode() not in first.data
        report.append(
            f"wrap: data key {row.key_id} wrapped as {row.wrapped_key[:9].decode()}..., kek {row.kek_id}"
        )

        # 2. encode / decode through Vault's decrypt
        assert await opened(first) == TICKET.encode()
        report.append("decode: a fresh cache unwrapped the key through Vault and opened the payload")

        # 3. rotate the transit key in Vault, then move the data keys with the operator's rewrap
        await operator.rotate(key)
        assert await operator.version(key) == 2
        rewrap = await client.post("/api/v1/platform/secrets/rewrap")
        assert rewrap.status_code == 200 and rewrap.json()["rewrapped"] >= 1, rewrap.text
        (row,) = await _row()
        assert row.kek_id == f"{key}-v2" and row.wrapped_key.startswith(b"vault:v2:")
        assert await opened(first) == TICKET.encode()
        report.append(f"rotate transit: v2, rewrap moved the data key to {row.kek_id}; the old payload opens")

        # 4. rotate the organisation's data key: new payloads under it, old ones still open
        rotated = await client.post("/api/v1/orgs/northwind/temporal-keys/rotation")
        assert rotated.status_code == 200, rotated.text
        second = await seal(codec(cache))
        assert second.metadata[KEY_ID_METADATA].decode() == rotated.json()["key_id"] != row.key_id
        assert await opened(first) == TICKET.encode() and await opened(second) == TICKET.encode()
        report.append(f"rotate data key: {row.key_id} retired, {rotated.json()['key_id']} active; both open")

        # 5. a wrapped key moved to another organisation does not open (Vault refuses it when derived)
        moved = (await _row())[0]
        if derived:
            with pytest.raises(Exception, match="does not open"):
                await wrapper.on_loop(
                    wrapper.aunwrap(
                        moved.wrapped_key,
                        b"",
                        b"temporal-data-key|other|" + moved.key_id.encode(),
                        moved.kek_id,
                    )
                )
            report.append("bound context: Vault itself refused the wrapped key under another organisation")

        # 6. shred: every wrapped key destroyed, and nothing the organisation sealed decodes any more
        shredded = await client.post(
            "/api/v1/orgs/northwind/temporal-keys/shredding",
            json={"confirm": "northwind", "reason": "live proof against Vault"},
        )
        assert shredded.status_code == 200, shredded.text
        assert sorted(shredded.json()["shredded"]) == sorted(r.key_id for r in await _row())
        for sealed in (first, second):
            with pytest.raises(ShreddedKeyError):
                await opened(sealed)
            with pytest.raises(ShreddedKeyError):
                await codec(cache).decode([sealed])
        assert all(r.state == "shredded" and r.wrapped_key is None for r in await _row())
        report.append(
            f"shred: {len(shredded.json()['shredded'])} keys destroyed; both payloads refuse to decode"
        )
    finally:
        install_data_keys(None)
        set_secret_store(None)
        wrapper.close()
        await operator.delete(key)
        print("\n".join(["", *report]))
