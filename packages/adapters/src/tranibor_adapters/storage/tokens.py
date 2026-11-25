# SPDX-License-Identifier: Apache-2.0
"""Bearer tokens from the pod's cloud identity (S86-17), for the Azure Blob and the GCS stores.

- `AzureWorkloadIdentity`: Azure Workload Identity. The webhook sets `AZURE_CLIENT_ID`,
  `AZURE_TENANT_ID`, `AZURE_FEDERATED_TOKEN_FILE` (and `AZURE_AUTHORITY_HOST`); the projected service
  account token is the `client_assertion` of a client-credentials grant at Microsoft Entra ID.
- `GceMetadataToken`: GKE Workload Identity. The metadata server hands the token of the Google service
  account the Kubernetes service account is bound to (`Metadata-Flavor: Google`).

Each token is cached and fetched again `REFRESH_MARGIN` seconds before it expires; nothing here logs
or raises a token or an assertion.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Callable, Mapping
from typing import Protocol, runtime_checkable

import httpx

from .base import ObjectStoreError, Refreshing, read_token_file, send

AZURE_AUTHORITY = "https://login.microsoftonline.com/"
AZURE_STORAGE_SCOPE = "https://storage.azure.com/.default"
JWT_BEARER = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
GCE_METADATA_HOST = "metadata.google.internal"
GCE_TOKEN_PATH = "/computeMetadata/v1/instance/service-accounts/default/token"  # noqa: S105 - a path
_IDENTITY_TIMEOUT = 10.0


@runtime_checkable
class TokenSource(Protocol):
    #: Where the token comes from, for messages.
    kind: str

    async def token(self) -> str: ...


class StaticToken:
    """A token given as is (a test, a bench); it is never refreshed."""

    kind = "static token"

    def __init__(self, value: str) -> None:
        self._value = value

    async def token(self) -> str:
        return self._value


def _expiry(payload: dict[str, object], clock: Callable[[], float]) -> float:
    try:
        seconds = float(str(payload.get("expires_in", 3600)))
    except ValueError:
        seconds = 3600.0
    return clock() + seconds


class AzureWorkloadIdentity:
    """A Microsoft Entra ID token for Azure Storage, granted on the pod's federated token."""

    kind = "Azure workload identity"

    def __init__(
        self,
        client_id: str,
        tenant_id: str,
        token_file: str,
        *,
        authority_host: str = AZURE_AUTHORITY,
        scope: str = AZURE_STORAGE_SCOPE,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.token_file = token_file
        self.authority_host = authority_host.rstrip("/")
        self.scope = scope
        self._client = client
        self._clock = clock
        self._cache: Refreshing[str] = Refreshing(self._fetch, clock=clock)

    @classmethod
    def from_environment(
        cls, environ: Mapping[str, str] | None = None, *, client: httpx.AsyncClient | None = None
    ) -> AzureWorkloadIdentity | None:
        """The identity the Azure Workload Identity webhook injected, or `None` when it did not."""
        env = os.environ if environ is None else environ
        client_id = env.get("AZURE_CLIENT_ID", "")
        tenant_id = env.get("AZURE_TENANT_ID", "")
        token_file = env.get("AZURE_FEDERATED_TOKEN_FILE", "")
        if not (client_id and tenant_id and token_file):
            return None
        return cls(
            client_id,
            tenant_id,
            token_file,
            authority_host=env.get("AZURE_AUTHORITY_HOST") or AZURE_AUTHORITY,
            client=client,
        )

    async def token(self) -> str:
        return await self._cache.get()

    async def _fetch(self) -> tuple[str, float | None]:
        assertion = await read_token_file(self.token_file, "Azure workload identity")
        response = await send(
            self._client,
            "POST",
            f"{self.authority_host}/{self.tenant_id}/oauth2/v2.0/token",
            what=f"Microsoft Entra ID ({httpx.URL(self.authority_host).host})",
            timeout=_IDENTITY_TIMEOUT,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "scope": self.scope,
                "client_assertion_type": JWT_BEARER,
                "client_assertion": assertion,
            },
        )
        if response.status_code >= 300:
            error = ""
            with contextlib.suppress(ValueError):
                error = str(response.json().get("error", ""))
            raise ObjectStoreError(
                f"Microsoft Entra ID refused the federated token of client {self.client_id}: "
                f"{error or response.status_code}"
            )
        try:
            payload = response.json()
            token = str(payload["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ObjectStoreError("Microsoft Entra ID answered without an access token") from exc
        return token, _expiry(payload, self._clock)


class GceMetadataToken:
    """The token of the pod's Google service account, from the metadata server (GKE Workload Identity)."""

    kind = "GKE workload identity"

    def __init__(
        self,
        *,
        host: str = "",
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        # `GCE_METADATA_HOST` is the name Google's own libraries read to point at another server.
        self.host = host or os.environ.get("GCE_METADATA_HOST") or GCE_METADATA_HOST
        self._client = client
        self._clock = clock
        self._cache: Refreshing[str] = Refreshing(self._fetch, clock=clock)

    async def token(self) -> str:
        return await self._cache.get()

    async def _fetch(self) -> tuple[str, float | None]:
        response = await send(
            self._client,
            "GET",
            f"http://{self.host}{GCE_TOKEN_PATH}",
            what=f"the metadata server ({self.host})",
            timeout=_IDENTITY_TIMEOUT,
            headers={"Metadata-Flavor": "Google"},
        )
        if response.status_code >= 300:
            raise ObjectStoreError(
                f"the metadata server answered {response.status_code} for the pod's service account "
                "token: is the Kubernetes service account bound to a Google service account?"
            )
        try:
            payload = response.json()
            token = str(payload["access_token"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ObjectStoreError("the metadata server answered without an access token") from exc
        return token, _expiry(payload, self._clock)


__all__ = [
    "AZURE_STORAGE_SCOPE",
    "AzureWorkloadIdentity",
    "GceMetadataToken",
    "StaticToken",
    "TokenSource",
]
