"""S86-17: every object store behaves as the port says, whichever cloud holds the bytes.

One suite (`exercise`), run against each implementation: the fake, the three stores against
in-memory emulators of their protocol (always), and — when Docker is asked for (`conftest.py`) — the
Azure Blob store against Azurite with a shared key, the GCS store against fake-gcs-server, and the S3
store against RustFS with static keys and with temporary credentials signed with their session token.

What it does not prove: no real AWS, Azure or Google account; the cloud identities are proved by
their unit tests against scripted transports, and on a cluster by S86-11 to S86-13.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from tranibor_adapters.fakes import FakeObjectStore
from tranibor_adapters.storage import (
    AzureBlobObjectStore,
    GcsObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    S3ObjectStore,
    StaticAwsCredentials,
    StaticToken,
    shared_key_authorization,
    sigv4_headers,
)
from tranibor_adapters.storage.s3 import EMPTY_SHA256

from . import emulators

pytestmark = pytest.mark.conformance

DATA = bytes(range(256)) * 64
#: Azurite's well-known development account (documented by Microsoft, not a secret).
AZURITE_ACCOUNT = "devstoreaccount1"
AZURITE_KEY = "Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw=="

Make = Callable[[str], ObjectStore]


async def exercise(make: Make, missing: ObjectStore) -> None:
    """The port's contract. `make(prefix)` gives a store on the test's bucket under `prefix`;
    `missing` is a store whose bucket does not exist."""
    prefix = f"conformance/{uuid.uuid4().hex[:8]}"
    store = make(prefix)
    await store.check()

    key = "orgs/acme/a deck+v1 é.bin"
    assert await store.put(key, DATA, "application/octet-stream") == store.url_of(key)
    assert prefix in store.url_of(key)
    assert await store.get(key) == DATA

    await store.put(key, b"second", "text/plain")
    assert await store.get(key) == b"second", "a put overwrites"
    await store.put("orgs/acme/empty", b"", "text/plain")
    assert await store.get("orgs/acme/empty") == b""

    with pytest.raises(ObjectNotFoundError):
        await store.get("orgs/acme/absent")
    with pytest.raises(ObjectNotFoundError):
        await make(f"{prefix}-other").get(key)  # another prefix sees nothing of this one

    await store.delete(key)
    with pytest.raises(ObjectNotFoundError):
        await store.get(key)
    await store.delete(key)  # idempotent: deleting an absent object is not an error

    with pytest.raises(ObjectStoreError) as refused:
        await missing.check()
    assert not isinstance(refused.value, ObjectNotFoundError)


# ───────── always: the fake and the emulators ─────────


async def test_the_fake() -> None:
    objects: dict[str, FakeObjectStore] = {}

    def make(prefix: str) -> ObjectStore:
        return objects.setdefault(prefix, FakeObjectStore(f"memory://choregos/{prefix}"))

    missing = FakeObjectStore()
    missing.fail_next("no bucket")
    await exercise(make, missing)


async def test_s3_against_its_emulator() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(emulators.s3({"choregos"})))

    def store(url: str) -> S3ObjectStore:
        return S3ObjectStore(url, endpoint="http://s3.test", access_key="A", secret_key="S", client=client)

    await exercise(lambda prefix: store(f"s3://choregos/{prefix}"), store("s3://absent"))


async def test_azure_blob_against_its_emulator() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(emulators.azure_blob({"artifacts"})))

    def store(url: str) -> AzureBlobObjectStore:
        return AzureBlobObjectStore(
            url, endpoint="https://acme.blob.core.windows.net", token_source=StaticToken("t"), client=client
        )

    await exercise(lambda prefix: store(f"azblob://artifacts/{prefix}"), store("azblob://absent"))


async def test_gcs_against_its_emulator() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(emulators.gcs({"choregos"})))

    def store(url: str) -> GcsObjectStore:
        return GcsObjectStore(url, token_source=StaticToken("ya29"), client=client)

    await exercise(lambda prefix: store(f"gs://choregos/{prefix}"), store("gs://absent"))


# ───────── with Docker: the real servers ─────────


async def test_azure_blob_against_azurite(azurite: str) -> None:
    endpoint = f"{azurite}/{AZURITE_ACCOUNT}"
    container = f"c{uuid.uuid4().hex[:12]}"
    path = f"/{AZURITE_ACCOUNT}/{container}"
    headers = {
        "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
        "x-ms-version": "2021-12-02",
    }
    query = {"restype": "container"}
    headers["Authorization"] = shared_key_authorization(
        account=AZURITE_ACCOUNT,
        key=AZURITE_KEY,
        method="PUT",
        path=path,
        query=query,
        headers=headers,
        content_length=0,
    )
    async with httpx.AsyncClient() as client:
        created = await client.put(f"{azurite}{path}", params=query, headers=headers)
    assert created.status_code == 201, created.text

    def store(url: str, key: str = AZURITE_KEY) -> AzureBlobObjectStore:
        return AzureBlobObjectStore(url, endpoint=endpoint, account_key=key)

    assert store("azblob://x").account == AZURITE_ACCOUNT
    await exercise(lambda prefix: store(f"azblob://{container}/{prefix}"), store("azblob://absent"))
    # A wrong key is refused by the server: the signature is checked, not merely present.
    wrong = store(f"azblob://{container}", key="d3Jvbmcta2V5")
    with pytest.raises(ObjectStoreError, match="refused the credentials"):
        await wrong.check()


async def test_gcs_against_fake_gcs_server(fake_gcs: str) -> None:
    bucket = f"b{uuid.uuid4().hex[:12]}"
    async with httpx.AsyncClient() as client:
        created = await client.post(
            f"{fake_gcs}/storage/v1/b", params={"project": "test"}, json={"name": bucket}
        )
    assert created.status_code == 200, created.text

    def store(url: str) -> GcsObjectStore:
        return GcsObjectStore(url, endpoint=fake_gcs, token_source=StaticToken("ya29.fake"))

    await exercise(lambda prefix: store(f"gs://{bucket}/{prefix}"), store("gs://absent-bucket"))


async def _s3_call(
    endpoint: str, method: str, path: str, keys: tuple[str, str], body: bytes = b"", **kw: str
) -> httpx.Response:
    host = httpx.URL(endpoint).netloc.decode()
    import hashlib

    payload = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
    extra = {"content-type": "application/x-www-form-urlencoded"} if body else {}
    headers = extra | sigv4_headers(
        method=method,
        host=host,
        path=path,
        headers=extra,
        payload_sha256=payload,
        access_key=keys[0],
        secret_key=keys[1],
        region="us-east-1",
        now=datetime.now(UTC),
        service=kw.get("service", "s3"),
    )
    async with httpx.AsyncClient() as client:
        return await client.request(method, f"{endpoint}{path}", content=body or None, headers=headers)


async def test_s3_against_rustfs_with_static_keys_and_with_a_session_token(rustfs: str) -> None:
    keys = ("conformance", "conformance-secret")
    bucket = f"b{uuid.uuid4().hex[:12]}"
    for _ in range(60):  # the server answers 503 until its storage has a quorum
        created = await _s3_call(rustfs, "PUT", f"/{bucket}", keys)
        if created.status_code != 503:
            break
        await asyncio.sleep(1)
    assert created.status_code == 200, created.text

    def store(url: str, credentials: StaticAwsCredentials) -> S3ObjectStore:
        return S3ObjectStore(url, endpoint=rustfs, credentials=credentials)

    static = StaticAwsCredentials(*keys)
    await exercise(lambda prefix: store(f"s3://{bucket}/{prefix}", static), store("s3://absent", static))

    # Temporary credentials from the server's STS (AssumeRole), then the same suite signed with them.
    answer = await _s3_call(
        rustfs,
        "POST",
        "/",
        keys,
        body=b"Action=AssumeRole&Version=2011-06-15&DurationSeconds=900",
        service="sts",
    )
    assert answer.status_code == 200, answer.text

    def field(name: str) -> str:
        found = re.search(rf"<{name}>([^<]+)</{name}>", answer.text)
        assert found, name
        return found.group(1)

    temporary = StaticAwsCredentials(field("AccessKeyId"), field("SecretAccessKey"), field("SessionToken"))
    await exercise(
        lambda prefix: store(f"s3://{bucket}/{prefix}", temporary), store("s3://absent", temporary)
    )
    # The same keys without their session token, or with another one, are refused (RustFS answers 500
    # where AWS answers 403): the token is checked, not merely carried.
    for token in ("", "forged-session-token"):
        forged = StaticAwsCredentials(field("AccessKeyId"), field("SecretAccessKey"), token)
        with pytest.raises(ObjectStoreError, match=r"object store http://127\.0\.0\.1:\d+: "):
            await store(f"s3://{bucket}", forged).check()
        with pytest.raises(ObjectStoreError, match="refused PUT"):
            await store(f"s3://{bucket}", forged).put("k", b"x", "text/plain")
