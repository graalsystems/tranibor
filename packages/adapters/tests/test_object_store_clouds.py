"""S87-17: the Azure Blob and GCS stores — their requests, their credentials and what they say when
refused. The port's contract itself is the conformance suite's (`tests/conformance/object_stores`)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from urllib.parse import parse_qs

import httpx
import pytest
from tranibor_adapters.storage import (
    AzureBlobObjectStore,
    GcsObjectStore,
    ObjectNotFoundError,
    ObjectStore,
    ObjectStoreError,
    StaticToken,
    shared_key_authorization,
)
from tranibor_adapters.storage.azure_blob import account_of

KEY = base64.b64encode(b"account-key-bytes").decode()


def _client(
    answer: httpx.Response | None = None, seen: list[httpx.Request] | None = None
) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return answer or httpx.Response(201)

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


# ───────── Azure Blob ─────────


def test_an_azure_url_names_a_container_and_an_endpoint() -> None:
    with pytest.raises(ValueError, match="azblob://"):
        AzureBlobObjectStore("s3://b", endpoint="https://a.blob.core.windows.net")
    with pytest.raises(ValueError, match="no container"):
        AzureBlobObjectStore("azblob://", endpoint="https://a.blob.core.windows.net")
    with pytest.raises(ValueError, match="account's endpoint"):
        AzureBlobObjectStore("azblob://c", endpoint="")
    with pytest.raises(ValueError, match="no storage account"):
        AzureBlobObjectStore("azblob://c", endpoint="http://127.0.0.1:10000", account_key=KEY)
    store = AzureBlobObjectStore("azblob://files/tranibor/", endpoint="https://acme.blob.core.windows.net/")
    assert isinstance(store, ObjectStore)
    assert store.url_of("/orgs/a") == "azblob://files/tranibor/orgs/a"
    assert account_of("https://acme.blob.core.windows.net") == "acme"
    assert account_of("https://acme.blob.core.chinacloudapi.cn") == "acme"
    assert account_of("http://127.0.0.1:10000/devstoreaccount1") == "devstoreaccount1"


def test_the_shared_key_signature_is_the_documented_one() -> None:
    """Recomputed here from the Blob service's documented string-to-sign, field by field."""
    headers = {
        "x-ms-date": "Wed, 25 Nov 2026 12:00:00 GMT",
        "x-ms-version": "2021-12-02",
        "x-ms-blob-type": "BlockBlob",
        "Content-Type": "text/plain",
    }
    got = shared_key_authorization(
        account="acme",
        key=KEY,
        method="PUT",
        path="/files/a%20b",
        query={"timeout": "30"},
        headers=headers,
        content_length=5,
    )
    string_to_sign = (
        "PUT\n\n\n5\n\ntext/plain\n\n\n\n\n\n\n"
        "x-ms-blob-type:BlockBlob\nx-ms-date:Wed, 25 Nov 2026 12:00:00 GMT\nx-ms-version:2021-12-02\n"
        "/acme/files/a%20b\ntimeout:30"
    )
    signature = base64.b64encode(
        hmac.new(b"account-key-bytes", string_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    assert got == f"SharedKey acme:{signature}"
    with pytest.raises(ObjectStoreError, match="not base64"):
        shared_key_authorization(
            account="acme", key="not base64!", method="GET", path="/", query={}, headers={}, content_length=0
        )


async def test_azure_puts_a_block_blob_signed_with_the_shared_key() -> None:
    seen: list[httpx.Request] = []
    store = AzureBlobObjectStore(
        "azblob://files/tranibor",
        endpoint="https://acme.blob.core.windows.net",
        account_key=KEY,
        client=_client(seen=seen),
    )
    assert await store.put("orgs/a deck", b"PK", "application/zip") == "azblob://files/tranibor/orgs/a deck"
    request = seen[0]
    assert request.method == "PUT"
    assert request.url.raw_path == b"/files/tranibor/orgs/a%20deck"
    assert request.headers["x-ms-blob-type"] == "BlockBlob"
    assert request.headers["Content-Type"] == "application/zip"
    assert request.headers["x-ms-version"] == "2021-12-02"
    assert request.headers["Authorization"].startswith("SharedKey acme:")
    assert store.authorisation == "shared key"


async def test_azure_appends_the_sas_token_and_sends_no_authorization() -> None:
    seen: list[httpx.Request] = []
    sas = "?sv=2021-12-02&ss=b&sp=rwdl&sig=abc%2Bdef%2F%3D"
    store = AzureBlobObjectStore(
        "azblob://files/tranibor",
        endpoint="https://acme.blob.core.windows.net",
        sas_token=sas,
        client=_client(httpx.Response(200), seen=seen),
    )
    await store.check()
    request = seen[0]
    assert "Authorization" not in request.headers
    query = parse_qs(request.url.query.decode())
    assert query["sig"] == ["abc+def/="], "the signature reaches the service as Azure issued it"
    assert query["restype"] == ["container"] and query["comp"] == ["list"]
    assert query["prefix"] == ["tranibor/"] and query["maxresults"] == ["1"]
    assert store.authorisation == "SAS token"


async def test_azure_tells_a_missing_blob_from_a_missing_container() -> None:
    blob_missing = httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
    container_missing = httpx.Response(404, headers={"x-ms-error-code": "ContainerNotFound"})
    endpoint = "https://acme.blob.core.windows.net"
    store = AzureBlobObjectStore("azblob://files", endpoint=endpoint, client=_client(blob_missing))
    with pytest.raises(ObjectNotFoundError):
        await store.get("k")
    await store.delete("k")
    gone = AzureBlobObjectStore("azblob://files", endpoint=endpoint, client=_client(container_missing))
    with pytest.raises(ObjectStoreError, match="ContainerNotFound") as raised:
        await gone.get("k")
    assert not isinstance(raised.value, ObjectNotFoundError)
    with pytest.raises(ObjectStoreError, match="refused DELETE"):
        await gone.delete("k")
    with pytest.raises(ObjectStoreError, match="container `files` does not exist"):
        await gone.check()
    refusing = AzureBlobObjectStore(
        "azblob://files",
        endpoint=endpoint,
        client=_client(httpx.Response(403, headers={"x-ms-error-code": "AuthorizationPermissionMismatch"})),
    )
    with pytest.raises(ObjectStoreError, match=r"refused PUT .* AuthorizationPermissionMismatch"):
        await refusing.put("k", b"x", "")
    with pytest.raises(ObjectStoreError, match="no account key, SAS token or workload identity is set"):
        await refusing.check()
    bearer = AzureBlobObjectStore(
        "azblob://files",
        endpoint=endpoint,
        token_source=StaticToken("t"),
        client=_client(httpx.Response(500, text="busy")),
    )
    with pytest.raises(ObjectStoreError, match=r"answered 500 \(static token\)"):
        await bearer.check()


# ───────── Google Cloud Storage ─────────


def test_a_gcs_url_names_a_bucket() -> None:
    with pytest.raises(ValueError, match="gs://"):
        GcsObjectStore("s3://b")
    with pytest.raises(ValueError, match="no bucket"):
        GcsObjectStore("gs://")
    store = GcsObjectStore("gs://files/tranibor", endpoint="http://127.0.0.1:4443/")
    assert isinstance(store, ObjectStore)
    assert store.endpoint == "http://127.0.0.1:4443"
    assert store.url_of("orgs/a") == "gs://files/tranibor/orgs/a"


async def test_gcs_speaks_the_json_api_with_the_object_name_as_one_segment() -> None:
    seen: list[httpx.Request] = []
    store = GcsObjectStore(
        "gs://files/tranibor",
        token_source=StaticToken("ya29"),
        client=_client(httpx.Response(200), seen=seen),
    )
    await store.put("orgs/a deck", b"PK", "")
    await store.get("orgs/a deck")
    await store.delete("orgs/a deck")
    await store.check()
    put, get, delete, check = seen
    assert put.method == "POST"
    assert (
        put.url.raw_path == b"/upload/storage/v1/b/files/o?uploadType=media&name=tranibor%2Forgs%2Fa%20deck"
    )
    assert put.headers["Content-Type"] == "application/octet-stream"
    assert put.content == b"PK"
    assert get.url.raw_path == b"/storage/v1/b/files/o/tranibor%2Forgs%2Fa%20deck?alt=media"
    assert delete.method == "DELETE"
    assert delete.url.raw_path == b"/storage/v1/b/files/o/tranibor%2Forgs%2Fa%20deck"
    assert check.url.raw_path == b"/storage/v1/b/files/o?maxResults=1&fields=kind&prefix=tranibor%2F"
    assert all(r.headers["Authorization"] == "Bearer ya29" for r in seen)
    assert all(r.url.host == "storage.googleapis.com" for r in seen)


async def test_gcs_says_what_it_refused() -> None:
    def store(answer: httpx.Response, *, identity: bool = True) -> GcsObjectStore:
        source = StaticToken("ya29") if identity else None
        return GcsObjectStore("gs://files", token_source=source, client=_client(answer))

    with pytest.raises(ObjectStoreError, match="refused PUT gs://files/k: 403"):
        await store(httpx.Response(403, text="denied")).put("k", b"x", "text/plain")
    with pytest.raises(ObjectNotFoundError):
        await store(httpx.Response(404)).get("k")
    with pytest.raises(ObjectStoreError, match="refused GET"):
        await store(httpx.Response(500)).get("k")
    await store(httpx.Response(404)).delete("k")
    with pytest.raises(ObjectStoreError, match="refused DELETE"):
        await store(httpx.Response(403)).delete("k")
    with pytest.raises(ObjectStoreError, match=r"refused the credentials \(static token\)"):
        await store(httpx.Response(401)).check()
    with pytest.raises(ObjectStoreError, match="bucket `files` does not exist; no identity is set"):
        await store(httpx.Response(404), identity=False).check()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    unreachable = GcsObjectStore("gs://files", client=httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(ObjectStoreError, match="unreachable"):
        await unreachable.check()
