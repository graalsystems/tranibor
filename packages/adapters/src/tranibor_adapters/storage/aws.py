# SPDX-License-Identifier: Apache-2.0
"""Where the S3 store's credentials come from (S86-17): static keys, or the pod's AWS identity.

In order, as `aws_credentials` chooses them:

1. **static keys** (`access-key`, `secret-key`, and a `session-token` when they are temporary);
2. **web identity**, the flow of EKS IAM Roles for Service Accounts (IRSA): `AWS_ROLE_ARN` and
   `AWS_WEB_IDENTITY_TOKEN_FILE` (both set by EKS's webhook) give the projected service account token,
   exchanged at STS by an *unsigned* `AssumeRoleWithWebIdentity`;
3. **the container credentials endpoint**, the flow of EKS Pod Identity (and of ECS):
   `AWS_CONTAINER_CREDENTIALS_FULL_URI` with the token of `AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE`.

The temporary credentials of 2 and 3 are cached and fetched again `REFRESH_MARGIN` seconds before they
expire; the token files are read again at each fetch, since the kubelet rotates them. Nothing here
logs or raises a key, a session token or a web identity token.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

import httpx

from .base import ObjectStoreError, Refreshing, read_token_file, send

STS_VERSION = "2011-06-15"
#: The hosts a plain-http container credentials endpoint may name, as the AWS SDKs allow them: the
#: loopback, the ECS agent and the EKS Pod Identity agent. Anything else must be https.
CONTAINER_HOSTS = frozenset({"169.254.170.2", "169.254.170.23", "fd00:ec2::23"})
ECS_HOST = "http://169.254.170.2"
_IDENTITY_TIMEOUT = 10.0


@dataclass(frozen=True)
class AwsCredentials:
    """One set of credentials; `session_token` is set for temporary ones, which must be signed with it."""

    access_key: str
    secret_key: str = field(repr=False)
    session_token: str = field(default="", repr=False)


@runtime_checkable
class AwsCredentialSource(Protocol):
    #: Where the credentials come from, for messages: `static keys`, `web identity`, `container endpoint`.
    kind: str

    async def credentials(self) -> AwsCredentials: ...


class StaticAwsCredentials:
    kind = "static keys"

    def __init__(self, access_key: str, secret_key: str, session_token: str = "") -> None:
        self._credentials = AwsCredentials(access_key, secret_key, session_token)

    async def credentials(self) -> AwsCredentials:
        return self._credentials


def _iso_expiry(value: str, what: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise ObjectStoreError(f"{what}: an expiry that is not a date ({value[:40]!r})") from exc


def _xml_field(document: str, name: str) -> str:
    """One element's text: STS answers a flat, known document, read without an XML parser (no
    entity is ever expanded)."""
    found = re.search(rf"<{name}>([^<]*)</{name}>", document)
    return found.group(1).strip() if found else ""


class WebIdentityAwsCredentials:
    """`AssumeRoleWithWebIdentity` with the pod's projected service account token (EKS IRSA)."""

    kind = "web identity"

    def __init__(
        self,
        role_arn: str,
        token_file: str,
        *,
        region: str = "",
        session_name: str = "",
        sts_endpoint: str = "",
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.role_arn = role_arn
        self.token_file = token_file
        self.session_name = session_name or f"tranibor-object-store-{int(clock())}"
        if sts_endpoint:
            self.sts_endpoint = sts_endpoint.rstrip("/")
        elif region:
            self.sts_endpoint = f"https://sts.{region}.amazonaws.com"
        else:
            self.sts_endpoint = "https://sts.amazonaws.com"
        self._client = client
        self._cache: Refreshing[AwsCredentials] = Refreshing(self._fetch, clock=clock)

    async def credentials(self) -> AwsCredentials:
        return await self._cache.get()

    async def _fetch(self) -> tuple[AwsCredentials, float | None]:
        what = "AWS web identity"
        token = await read_token_file(self.token_file, what)
        # Unsigned on purpose: the web identity token is the proof, and the pod has no key to sign with.
        response = await send(
            self._client,
            "POST",
            f"{self.sts_endpoint}/",
            what=f"STS ({self.sts_endpoint})",
            timeout=_IDENTITY_TIMEOUT,
            headers={"Accept": "application/xml"},
            data={
                "Action": "AssumeRoleWithWebIdentity",
                "Version": STS_VERSION,
                "RoleArn": self.role_arn,
                "RoleSessionName": self.session_name,
                "WebIdentityToken": token,
            },
        )
        if response.status_code >= 300:
            code = _xml_field(response.text, "Code") or str(response.status_code)
            raise ObjectStoreError(f"STS refused AssumeRoleWithWebIdentity for {self.role_arn}: {code}")
        body = response.text
        credentials = AwsCredentials(
            _xml_field(body, "AccessKeyId"),
            _xml_field(body, "SecretAccessKey"),
            _xml_field(body, "SessionToken"),
        )
        if not (credentials.access_key and credentials.secret_key and credentials.session_token):
            raise ObjectStoreError("STS answered AssumeRoleWithWebIdentity without credentials")
        return credentials, _iso_expiry(_xml_field(body, "Expiration"), what)


def _container_uri_allowed(uri: str) -> bool:
    url = httpx.URL(uri)
    if url.scheme == "https":
        return True
    if url.scheme != "http":
        return False
    host = url.host
    if host == "localhost" or host in CONTAINER_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ContainerAwsCredentials:
    """The container credentials endpoint (EKS Pod Identity's agent, or ECS's), with its token."""

    kind = "container endpoint"

    def __init__(
        self,
        full_uri: str,
        *,
        token: str = "",
        token_file: str = "",
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not _container_uri_allowed(full_uri):
            raise ValueError(
                "AWS_CONTAINER_CREDENTIALS_FULL_URI must be https, or http to the loopback, "
                f"169.254.170.2 or 169.254.170.23; not `{httpx.URL(full_uri).host}`"
            )
        self.full_uri = full_uri
        self.token = token
        self.token_file = token_file
        self._client = client
        self._cache: Refreshing[AwsCredentials] = Refreshing(self._fetch, clock=clock)

    async def credentials(self) -> AwsCredentials:
        return await self._cache.get()

    async def _fetch(self) -> tuple[AwsCredentials, float | None]:
        what = "AWS container credentials"
        headers = {"Accept": "application/json"}
        token = await read_token_file(self.token_file, what) if self.token_file else self.token
        if token:
            headers["Authorization"] = token
        response = await send(
            self._client,
            "GET",
            self.full_uri,
            what=f"the container credentials endpoint ({httpx.URL(self.full_uri).host})",
            timeout=_IDENTITY_TIMEOUT,
            headers=headers,
        )
        if response.status_code >= 300:
            raise ObjectStoreError(f"the container credentials endpoint answered {response.status_code}")
        try:
            body = response.json()
            credentials = AwsCredentials(
                str(body["AccessKeyId"]), str(body["SecretAccessKey"]), str(body.get("Token", ""))
            )
            expiration = body.get("Expiration")
        except (ValueError, KeyError, TypeError) as exc:
            raise ObjectStoreError("the container credentials endpoint answered without credentials") from exc
        return credentials, _iso_expiry(str(expiration), what) if expiration else None


def aws_credentials(
    access_key: str = "",
    secret_key: str = "",
    session_token: str = "",
    *,
    region: str = "",
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> AwsCredentialSource | None:
    """The first source available, in the order of the module's docstring; `None` when there is none
    (the calls then go unsigned, as to an open or a bucket-policy store)."""
    env = os.environ if environ is None else environ
    if access_key:
        return StaticAwsCredentials(access_key, secret_key, session_token)
    role_arn, token_file = env.get("AWS_ROLE_ARN", ""), env.get("AWS_WEB_IDENTITY_TOKEN_FILE", "")
    if role_arn and token_file:
        return WebIdentityAwsCredentials(
            role_arn,
            token_file,
            region=env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION") or region,
            session_name=env.get("AWS_ROLE_SESSION_NAME", ""),
            sts_endpoint=env.get("AWS_ENDPOINT_URL_STS", ""),
            client=client,
        )
    full_uri = env.get("AWS_CONTAINER_CREDENTIALS_FULL_URI", "")
    relative = env.get("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "")
    if full_uri or relative:
        return ContainerAwsCredentials(
            full_uri or f"{ECS_HOST}{relative}",
            token=env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN", ""),
            token_file=env.get("AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE", ""),
            client=client,
        )
    return None


__all__ = [
    "AwsCredentialSource",
    "AwsCredentials",
    "ContainerAwsCredentials",
    "StaticAwsCredentials",
    "WebIdentityAwsCredentials",
    "aws_credentials",
]
