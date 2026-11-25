# SPDX-License-Identifier: Apache-2.0
"""The Google Cloud Storage store (S86-17), in the JSON API over `httpx` — no SDK in the core.

GCS speaks S3 only with HMAC keys, which a Workload Identity does not have: this store takes the
bearer token of the pod's Google service account instead (`tokens.GceMetadataToken`, GKE Workload
Identity). The store URL is `gs://<bucket>/<prefix>`; the endpoint defaults to
`https://storage.googleapis.com` (another one for an emulator).
"""

from __future__ import annotations

from urllib.parse import quote

import httpx

from .base import CHECK_TIMEOUT, ObjectNotFoundError, ObjectStoreError, send
from .tokens import TokenSource

SCHEME = "gs://"
DEFAULT_ENDPOINT = "https://storage.googleapis.com"


class GcsObjectStore:
    """A bucket of Google Cloud Storage. Without a `token_source` the calls are anonymous (an emulator)."""

    def __init__(
        self,
        url: str,
        *,
        endpoint: str = "",
        token_source: TokenSource | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not url.startswith(SCHEME):
            raise ValueError(f"a GCS object store URL starts with {SCHEME}, not `{url}`")
        bucket, _, prefix = url.removeprefix(SCHEME).partition("/")
        if not bucket:
            raise ValueError(f"no bucket in `{url}`")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = (endpoint or DEFAULT_ENDPOINT).rstrip("/")
        self.token_source = token_source
        self._client = client
        self._timeout = timeout

    def _full_key(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def url_of(self, key: str) -> str:
        return f"{SCHEME}{self.bucket}/{self._full_key(key)}"

    def _object(self, key: str) -> str:
        """An object's name as one path segment: every `/` encoded, as the JSON API requires."""
        return quote(self._full_key(key), safe="")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes = b"",
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        sent = dict(headers or {})
        if self.token_source is not None:
            sent["Authorization"] = f"Bearer {await self.token_source.token()}"
        return await send(
            self._client,
            method,
            f"{self.endpoint}{path}",
            what=f"object store ({self.endpoint})",
            timeout=timeout or self._timeout,
            content=data if method == "POST" else None,
            headers=sent,
        )

    def _refused(self, method: str, key: str, response: httpx.Response) -> ObjectStoreError:
        return ObjectStoreError(
            f"object store refused {method} {self.url_of(key)}: {response.status_code} {response.text[:300]}"
        )

    async def put(self, key: str, data: bytes, media_type: str) -> str:
        """A simple media upload: one request, which the JSON API takes up to 5 TiB."""
        name = quote(self._full_key(key), safe="")
        response = await self._request(
            "POST",
            f"/upload/storage/v1/b/{self.bucket}/o?uploadType=media&name={name}",
            data=data,
            headers={"Content-Type": media_type or "application/octet-stream"},
        )
        if response.status_code >= 300:
            raise self._refused("PUT", key, response)
        return self.url_of(key)

    async def get(self, key: str) -> bytes:
        response = await self._request("GET", f"/storage/v1/b/{self.bucket}/o/{self._object(key)}?alt=media")
        if response.status_code == 404:
            raise ObjectNotFoundError(f"no object at {self.url_of(key)}")
        if response.status_code >= 300:
            raise self._refused("GET", key, response)
        return response.content

    async def delete(self, key: str) -> None:
        """Deletes the object; 404 is taken as done. It needs `storage.objects.delete`."""
        response = await self._request("DELETE", f"/storage/v1/b/{self.bucket}/o/{self._object(key)}")
        if response.status_code >= 300 and response.status_code != 404:
            raise self._refused("DELETE", key, response)

    async def check(self) -> None:
        """Lists at most one object under the prefix: it writes nothing and needs only
        `storage.objects.list`, which `roles/storage.objectUser` grants (reading the bucket's own
        metadata would need `storage.buckets.get`, which it does not)."""
        prefix = f"&prefix={quote(self.prefix + '/', safe='')}" if self.prefix else ""
        response = await self._request(
            "GET", f"/storage/v1/b/{self.bucket}/o?maxResults=1&fields=kind{prefix}", timeout=CHECK_TIMEOUT
        )
        if response.status_code < 300:
            return
        hint = f" ({self.token_source.kind})" if self.token_source is not None else "; no identity is set"
        reasons = {
            401: "the store refused the credentials",
            403: "the store refused the credentials",
            404: f"bucket `{self.bucket}` does not exist",
        }
        reason = reasons.get(response.status_code, f"it answered {response.status_code}")
        raise ObjectStoreError(f"object store {self.endpoint}: {reason}{hint}")


__all__ = ["DEFAULT_ENDPOINT", "GcsObjectStore"]
