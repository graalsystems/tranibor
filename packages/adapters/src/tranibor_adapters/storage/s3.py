# SPDX-License-Identifier: Apache-2.0
"""The S3 store: AWS, RustFS, MinIO, Ceph, with a hand-written SigV4 signature over `httpx`.

The credentials are static keys or the pod's AWS identity (`aws.py`, S86-17); temporary ones carry a
session token, sent as `x-amz-security-token` and signed with the other headers.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from urllib.parse import quote

import httpx

from .aws import AwsCredentials, AwsCredentialSource, StaticAwsCredentials
from .base import CHECK_TIMEOUT, ObjectNotFoundError, ObjectStoreError, send

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_headers(  # noqa: PLR0913 - the inputs of the signature, keyword-only
    *,
    method: str,
    host: str,
    path: str,
    query: str = "",
    headers: dict[str, str] | None = None,
    payload_sha256: str,
    access_key: str,
    secret_key: str,
    region: str,
    now: datetime,
    service: str = "s3",
    session_token: str = "",
) -> dict[str, str]:
    """The headers of an AWS Signature Version 4 request: `x-amz-date`, `x-amz-content-sha256`,
    `Authorization`, and `x-amz-security-token` for temporary credentials. `path` is already
    URI-encoded; every header passed is signed, the session token included."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = now.strftime("%Y%m%d")
    signed = {k.lower(): v.strip() for k, v in (headers or {}).items()}
    signed.update({"host": host, "x-amz-date": amz_date, "x-amz-content-sha256": payload_sha256})
    if session_token:
        signed["x-amz-security-token"] = session_token
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join([method, path, query, canonical_headers, signed_headers, payload_sha256])
    scope = f"{day}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac(_hmac(_hmac(_hmac(f"AWS4{secret_key}".encode(), day), region), service), "aws4_request")
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    result = {
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_sha256,
        "Authorization": (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
    }
    if session_token:
        result["x-amz-security-token"] = session_token
    return result


class S3ObjectStore:
    """An S3 bucket, path-style addressed (`<endpoint>/<bucket>/<key>`), which every S3-compatible
    server accepts — RustFS in development included.

    `credentials` is where the signing keys come from (`aws.aws_credentials` picks one); static
    `access_key` and `secret_key` (and `session_token`) are a shorthand for `StaticAwsCredentials`.
    Neither: the calls go unsigned."""

    def __init__(  # noqa: PLR0913 - the store's address, then its credentials, all keywords
        self,
        url: str,
        *,
        endpoint: str = "",
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
        session_token: str = "",
        credentials: AwsCredentialSource | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 60.0,
    ) -> None:
        if not url.startswith("s3://"):
            raise ValueError(f"an S3 object store URL starts with s3://, not `{url}`")
        bucket, _, prefix = url.removeprefix("s3://").partition("/")
        if not bucket:
            raise ValueError(f"no bucket in `{url}`")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.endpoint = (endpoint or f"https://s3.{region}.amazonaws.com").rstrip("/")
        self.region = region
        if credentials is None and access_key:
            credentials = StaticAwsCredentials(access_key, secret_key, session_token)
        self.credentials = credentials
        self._client = client
        self._timeout = timeout

    def _full_key(self, key: str) -> str:
        key = key.lstrip("/")
        return f"{self.prefix}/{key}" if self.prefix else key

    def url_of(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._full_key(key)}"

    async def _request(
        self,
        method: str,
        key: str | None,
        data: bytes = b"",
        media_type: str = "",
        *,
        timeout: float | None = None,
    ) -> httpx.Response:
        """`key=None` addresses the bucket itself."""
        base = httpx.URL(self.endpoint)
        base_path = base.path.rstrip("/")
        target = f"{base_path}/{self.bucket}" + ("" if key is None else f"/{self._full_key(key)}")
        path = quote(target, safe="/-_.~")
        host = base.host + (f":{base.port}" if base.port else "")
        payload = hashlib.sha256(data).hexdigest() if data else EMPTY_SHA256
        extra = {"content-type": media_type} if media_type else {}
        headers = {**extra}
        if self.credentials is not None:
            keys: AwsCredentials = await self.credentials.credentials()
            headers.update(
                sigv4_headers(
                    method=method,
                    host=host,
                    path=path,
                    headers=extra,
                    payload_sha256=payload,
                    access_key=keys.access_key,
                    secret_key=keys.secret_key,
                    session_token=keys.session_token,
                    region=self.region,
                    now=datetime.now(UTC),
                )
            )
        return await send(
            self._client,
            method,
            f"{base.scheme}://{host}{path}",
            what=f"object store ({self.endpoint})",
            timeout=timeout or self._timeout,
            content=data or None,
            headers=headers,
        )

    async def put(self, key: str, data: bytes, media_type: str) -> str:
        response = await self._request("PUT", key, data, media_type)
        if response.status_code >= 300:
            raise ObjectStoreError(
                f"object store refused PUT {self.url_of(key)}: {response.status_code} {response.text[:300]}"
            )
        return self.url_of(key)

    async def get(self, key: str) -> bytes:
        response = await self._request("GET", key)
        if response.status_code == 404:
            raise ObjectNotFoundError(f"no object at {self.url_of(key)}")
        if response.status_code >= 300:
            raise ObjectStoreError(
                f"object store refused GET {self.url_of(key)}: {response.status_code} {response.text[:300]}"
            )
        return response.content

    async def delete(self, key: str) -> None:
        """`DELETE` on the key; S3 answers 204 whether or not the object existed, and 404 is taken as done
        (some S3-compatible servers answer it). It needs the `s3:DeleteObject` permission."""
        response = await self._request("DELETE", key)
        if response.status_code >= 300 and response.status_code != 404:
            raise ObjectStoreError(
                f"object store refused DELETE {self.url_of(key)}: "
                f"{response.status_code} {response.text[:300]}"
            )

    async def check(self) -> None:
        """A `HEAD` on the bucket, signed like any call, with a short timeout: it writes nothing."""
        response = await self._request("HEAD", None, timeout=CHECK_TIMEOUT)
        if response.status_code < 300:
            return
        if self.credentials is None:
            hint = "; no access key is set and the pod has no AWS identity"
        else:
            hint = f" ({self.credentials.kind})"
        reasons = {
            403: "the store refused the credentials",
            404: f"bucket `{self.bucket}` does not exist",
        }
        reason = reasons.get(response.status_code, f"it answered {response.status_code}")
        raise ObjectStoreError(f"object store {self.endpoint}: {reason}{hint}")


__all__ = ["EMPTY_SHA256", "S3ObjectStore", "sigv4_headers"]
