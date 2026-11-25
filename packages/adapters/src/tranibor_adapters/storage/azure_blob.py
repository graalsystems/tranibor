# SPDX-License-Identifier: Apache-2.0
"""The Azure Blob store (S86-17): Azure Blob Storage has no S3 API, so it is spoken to in its own REST
API (Put Blob, Get Blob, Delete Blob, List Blobs), over `httpx` like the S3 store — no SDK in the core.

The store URL is `azblob://<container>/<prefix>`; the endpoint is the account's blob endpoint,
`https://<account>.blob.core.windows.net` (or Azurite's, path-style: `http://host:10000/<account>`).
Three ways to authorise, the first one given wins:

- a **shared key** (`account-key`): each request signed with the account's key (a bench, Azurite);
- a **SAS token** (`sas-token`): appended to each request's query, as Azure issued it;
- a **bearer token** from a `TokenSource`, in production the pod's Azure Workload Identity
  (`tokens.AzureWorkloadIdentity`), with the scope `https://storage.azure.com/.default`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import parse_qsl, quote, urlencode

import httpx

from .base import CHECK_TIMEOUT, ObjectNotFoundError, ObjectStoreError, send
from .tokens import TokenSource

#: The Blob service version every request names: bearer tokens need 2017-11-09 or later, and
#: Azurite accepts this one without `--skipApiVersionCheck`.
API_VERSION = "2021-12-02"
SCHEME = "azblob://"


def account_of(endpoint: str) -> str:
    """The storage account an endpoint names: `<account>.blob.core.windows.net` (or another cloud's
    suffix), else the first path segment of a path-style endpoint (Azurite)."""
    url = httpx.URL(endpoint)
    host = url.host
    if ".blob." in host:
        return host.split(".", 1)[0]
    return url.path.strip("/").split("/", 1)[0]


def shared_key_authorization(
    *,
    account: str,
    key: str,
    method: str,
    path: str,
    query: dict[str, str],
    headers: dict[str, str],
    content_length: int,
) -> str:
    """The `Authorization` header of a Shared Key request (Blob service, version 2015-02-21 or later).

    `path` is the request's encoded path (with the account's segment for a path-style endpoint),
    `headers` holds every header sent; a zero `content_length` is signed as the empty string."""
    lower = {k.lower(): v.strip() for k, v in headers.items()}
    standard = [
        "content-encoding",
        "content-language",
        "",  # content-length, below
        "content-md5",
        "content-type",
        "date",
        "if-modified-since",
        "if-match",
        "if-none-match",
        "if-unmodified-since",
        "range",
    ]
    length = str(content_length) if content_length else ""
    values = [lower.get(name, "") if name else length for name in standard]
    ms_headers = sorted(name for name in lower if name.startswith("x-ms-"))
    canonical_headers = "".join(f"{name}:{lower[name]}\n" for name in ms_headers)
    resource = f"/{account}{path}" + "".join(f"\n{name.lower()}:{query[name]}" for name in sorted(query))
    string_to_sign = "\n".join([method, *values]) + "\n" + canonical_headers + resource
    try:
        secret = base64.b64decode(key, validate=True)
    except binascii.Error as exc:
        raise ObjectStoreError("the Azure account key is not base64") from exc
    signature = base64.b64encode(hmac.new(secret, string_to_sign.encode("utf-8"), hashlib.sha256).digest())
    return f"SharedKey {account}:{signature.decode()}"


class AzureBlobObjectStore:
    """A container of an Azure storage account (or of Azurite)."""

    def __init__(
        self,
        url: str,
        *,
        endpoint: str,
        account: str = "",
        account_key: str = "",
        sas_token: str = "",
        token_source: TokenSource | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not url.startswith(SCHEME):
            raise ValueError(f"an Azure Blob object store URL starts with {SCHEME}, not `{url}`")
        container, _, prefix = url.removeprefix(SCHEME).partition("/")
        if not container:
            raise ValueError(f"no container in `{url}`")
        if not endpoint:
            raise ValueError(
                "an Azure Blob store needs its account's endpoint (https://<account>.blob.core.windows.net)"
            )
        self.container = container
        self.prefix = prefix.strip("/")
        self.endpoint = endpoint.rstrip("/")
        self.account = account or account_of(self.endpoint)
        if account_key and not self.account:
            raise ValueError(f"no storage account in `{endpoint}`: name it")
        self._account_key = account_key
        self._sas = dict(parse_qsl(sas_token.lstrip("?"), keep_blank_values=True)) if sas_token else {}
        self.token_source = token_source
        self._client = client
        self._timeout = timeout

    @property
    def authorisation(self) -> str:
        """How the requests are authorised, for messages: never the credential itself."""
        if self._account_key:
            return "shared key"
        if self._sas:
            return "SAS token"
        if self.token_source is not None:
            return self.token_source.kind
        return ""

    def _full_key(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def url_of(self, key: str) -> str:
        return f"{SCHEME}{self.container}/{self._full_key(key)}"

    async def _request(
        self,
        method: str,
        key: str | None,
        *,
        query: dict[str, str] | None = None,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """`key=None` addresses the container itself."""
        base = httpx.URL(self.endpoint)
        target = f"{base.path.rstrip('/')}/{self.container}"
        if key is not None:
            target += f"/{self._full_key(key)}"
        path = quote(target, safe="/-_.~")
        query = dict(query or {})
        sent = {
            "x-ms-date": datetime.now(UTC).strftime("%a, %d %b %Y %H:%M:%S GMT"),
            "x-ms-version": API_VERSION,
            **(headers or {}),
        }
        if self._account_key:
            sent["Authorization"] = shared_key_authorization(
                account=self.account,
                key=self._account_key,
                method=method,
                path=path,
                query=query,
                headers=sent,
                content_length=len(data),
            )
        elif self._sas:
            query.update(self._sas)
        elif self.token_source is not None:
            sent["Authorization"] = f"Bearer {await self.token_source.token()}"
        host = base.host + (f":{base.port}" if base.port else "")
        url = f"{base.scheme}://{host}{path}" + (f"?{urlencode(query, quote_via=quote)}" if query else "")
        return await send(
            self._client,
            method,
            url,
            what=f"object store ({self.endpoint})",
            timeout=timeout or self._timeout,
            content=data if method == "PUT" else None,
            headers=sent,
        )

    @staticmethod
    def _error_code(response: httpx.Response) -> str:
        return str(response.headers.get("x-ms-error-code", ""))

    def _refused(self, method: str, key: str, response: httpx.Response) -> ObjectStoreError:
        code = self._error_code(response) or response.text[:300]
        return ObjectStoreError(
            f"object store refused {method} {self.url_of(key)}: {response.status_code} {code}"
        )

    async def put(self, key: str, data: bytes, media_type: str) -> str:
        headers = {"x-ms-blob-type": "BlockBlob"}
        if media_type:
            headers["Content-Type"] = media_type
        response = await self._request("PUT", key, data=data, headers=headers)
        if response.status_code >= 300:
            raise self._refused("PUT", key, response)
        return self.url_of(key)

    async def get(self, key: str) -> bytes:
        response = await self._request("GET", key)
        if response.status_code == 404 and self._error_code(response) != "ContainerNotFound":
            raise ObjectNotFoundError(f"no object at {self.url_of(key)}")
        if response.status_code >= 300:
            raise self._refused("GET", key, response)
        return response.content

    async def delete(self, key: str) -> None:
        """Delete Blob; an absent blob (404 `BlobNotFound`) is taken as done, a missing container is not."""
        response = await self._request("DELETE", key)
        if response.status_code == 404 and self._error_code(response) != "ContainerNotFound":
            return
        if response.status_code >= 300:
            raise self._refused("DELETE", key, response)

    async def check(self) -> None:
        """List Blobs, at most one, under the prefix: it writes nothing, and it needs only the right to
        list what the platform writes (a container SAS or a `Storage Blob Data Contributor` role)."""
        query = {"restype": "container", "comp": "list", "maxresults": "1"}
        if self.prefix:
            query["prefix"] = f"{self.prefix}/"
        response = await self._request("GET", None, query=query, timeout=CHECK_TIMEOUT)
        if response.status_code < 300:
            return
        authorisation = self.authorisation
        hint = (
            f" ({authorisation})"
            if authorisation
            else "; no account key, SAS token or workload identity is set"
        )
        reasons = {
            403: "the store refused the credentials",
            404: f"container `{self.container}` does not exist",
        }
        reason = reasons.get(response.status_code, f"it answered {response.status_code}")
        raise ObjectStoreError(f"object store {self.endpoint}: {reason}{hint}")


__all__ = ["API_VERSION", "AzureBlobObjectStore", "account_of", "shared_key_authorization"]
