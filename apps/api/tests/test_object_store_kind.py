"""S86-17: `TRANIBOR_OBJECT_STORE_KIND` picks the store the API writes the files to, and a store with no
credentials in the settings takes the pod's cloud identity."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pytest
from pydantic import ValidationError
from tranibor_adapters.fakes import FakeObjectStore
from tranibor_adapters.storage import (
    AzureBlobObjectStore,
    AzureWorkloadIdentity,
    GcsObjectStore,
    S3ObjectStore,
    StaticAwsCredentials,
    WebIdentityAwsCredentials,
)
from tranibor_api.config import get_settings
from tranibor_api.object_store import object_store, use_object_store


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("TRANIBOR_FAKES", "0")
    for name in (
        "AWS_ROLE_ARN",
        "AWS_WEB_IDENTITY_TOKEN_FILE",
        "AWS_CONTAINER_CREDENTIALS_FULL_URI",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    use_object_store(None)
    yield
    use_object_store(None)
    get_settings.cache_clear()


def test_s3_is_the_default_and_takes_its_static_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_ACCESS_KEY", "AKIA")
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_SECRET_KEY", "secret")
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_SESSION_TOKEN", "session")
    store = object_store()
    assert isinstance(store, S3ObjectStore)
    assert isinstance(store.credentials, StaticAwsCredentials)
    assert object_store() is store, "built once per process"


def test_s3_without_keys_takes_the_pods_irsa_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/tranibor-api")
    monkeypatch.setenv("AWS_WEB_IDENTITY_TOKEN_FILE", str(tmp_path / "token"))
    store = object_store()
    assert isinstance(store, S3ObjectStore)
    assert isinstance(store.credentials, WebIdentityAwsCredentials)


def test_azure_blob_takes_the_workload_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_KIND", "azure-blob")
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_URL", "azblob://files/choregos")
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_ENDPOINT", "https://acme.blob.core.windows.net")
    monkeypatch.setenv("AZURE_CLIENT_ID", "client")
    monkeypatch.setenv("AZURE_TENANT_ID", "tenant")
    monkeypatch.setenv("AZURE_FEDERATED_TOKEN_FILE", str(tmp_path / "token"))
    store = object_store()
    assert isinstance(store, AzureBlobObjectStore)
    assert isinstance(store.token_source, AzureWorkloadIdentity)
    assert store.url_of("k") == "azblob://files/choregos/k"


def test_gcs_and_the_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_KIND", "gcs")
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_URL", "gs://files")
    assert isinstance(object_store(), GcsObjectStore)
    use_object_store(None)
    monkeypatch.setenv("TRANIBOR_FAKES", "1")
    get_settings.cache_clear()
    assert isinstance(object_store(), FakeObjectStore), "the fakes win over the kind (ADR 0005)"


def test_an_unknown_kind_is_refused_at_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANIBOR_OBJECT_STORE_KIND", "minio")
    with pytest.raises(ValidationError, match="object_store_kind"):
        get_settings()
