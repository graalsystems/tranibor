# SPDX-License-Identifier: Apache-2.0
"""The object store: where the platform keeps the bytes of file artifacts (ADR 0049).

Only the API writes and reads it. A runner never holds object store credentials: it hands its
files to the internal API, which hashes them, stores them and records them.

Four implementations of one port, `ObjectStore`, each over `httpx` with no cloud SDK in the core:

- `S3ObjectStore` (`kind: s3`): the S3 protocol (AWS, RustFS, MinIO, Ceph), a hand-written SigV4
  signature, with static keys or the pod's AWS identity (EKS IRSA or Pod Identity, `aws.py`);
- `AzureBlobObjectStore` (`kind: azure-blob`): the Blob REST API, with a shared key, a SAS token or
  the pod's Azure Workload Identity (`tokens.py`);
- `GcsObjectStore` (`kind: gcs`): the GCS JSON API, with the pod's GKE Workload Identity;
- `FakeObjectStore` (`tranibor_adapters.fakes.storage`), in memory, scriptable, for
  `TRANIBOR_FAKES=1` and tests.

`build_object_store` picks one from the settings (S86-17).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, get_args

import httpx

from .aws import (
    AwsCredentials,
    AwsCredentialSource,
    ContainerAwsCredentials,
    StaticAwsCredentials,
    WebIdentityAwsCredentials,
    aws_credentials,
)
from .azure_blob import AzureBlobObjectStore, shared_key_authorization
from .base import CHECK_TIMEOUT, ObjectNotFoundError, ObjectStore, ObjectStoreError
from .gcs import GcsObjectStore
from .s3 import EMPTY_SHA256, S3ObjectStore, sigv4_headers
from .tokens import AzureWorkloadIdentity, GceMetadataToken, StaticToken, TokenSource

ObjectStoreKind = Literal["s3", "azure-blob", "gcs"]
OBJECT_STORE_KINDS: tuple[str, ...] = get_args(ObjectStoreKind)


def build_object_store(  # noqa: PLR0913 - the settings of every kind, all keywords
    kind: str,
    url: str,
    *,
    endpoint: str = "",
    region: str = "us-east-1",
    access_key: str = "",
    secret_key: str = "",
    session_token: str = "",
    account_key: str = "",
    sas_token: str = "",
    environ: Mapping[str, str] | None = None,
    client: httpx.AsyncClient | None = None,
) -> ObjectStore:
    """The store a deployment's settings name; the credentials it gives win over the pod's identity,
    which is read from `environ` (the process's environment by default) as each cloud's webhook sets it."""
    if kind == "s3":
        return S3ObjectStore(
            url,
            endpoint=endpoint,
            region=region,
            credentials=aws_credentials(
                access_key, secret_key, session_token, region=region, environ=environ, client=client
            ),
            client=client,
        )
    if kind == "azure-blob":
        identity = None
        if not (account_key or sas_token):
            identity = AzureWorkloadIdentity.from_environment(environ, client=client)
        return AzureBlobObjectStore(
            url,
            endpoint=endpoint,
            account_key=account_key,
            sas_token=sas_token,
            token_source=identity,
            client=client,
        )
    if kind == "gcs":
        host = (environ or {}).get("GCE_METADATA_HOST", "")
        return GcsObjectStore(
            url, endpoint=endpoint, token_source=GceMetadataToken(host=host, client=client), client=client
        )
    raise ValueError(f"unknown object store kind `{kind}`: one of {', '.join(OBJECT_STORE_KINDS)}")


__all__ = [
    "CHECK_TIMEOUT",
    "EMPTY_SHA256",
    "OBJECT_STORE_KINDS",
    "AwsCredentialSource",
    "AwsCredentials",
    "AzureBlobObjectStore",
    "AzureWorkloadIdentity",
    "ContainerAwsCredentials",
    "GceMetadataToken",
    "GcsObjectStore",
    "ObjectNotFoundError",
    "ObjectStore",
    "ObjectStoreError",
    "ObjectStoreKind",
    "S3ObjectStore",
    "StaticAwsCredentials",
    "StaticToken",
    "TokenSource",
    "WebIdentityAwsCredentials",
    "aws_credentials",
    "build_object_store",
    "shared_key_authorization",
    "sigv4_headers",
]
