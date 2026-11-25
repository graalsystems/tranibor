# SPDX-License-Identifier: Apache-2.0
"""The platform's object store, built once from the settings (ADR 0049).

`TRANIBOR_FAKES=1` or a `memory://` URL gives the in-memory fake (ADR 0005); otherwise
`TRANIBOR_OBJECT_STORE_KIND` picks the adapter (`s3` by default, `azure-blob`, `gcs`, S86-17), with the
endpoint and credentials of `TRANIBOR_OBJECT_STORE_*`, or the pod's cloud identity when none is given.
Tests replace it with `use_object_store`.
"""

from __future__ import annotations

from tranibor_adapters.fakes.storage import FakeObjectStore
from tranibor_adapters.storage import ObjectStore, build_object_store

from .config import get_settings

_store: ObjectStore | None = None


def object_store() -> ObjectStore:
    global _store  # noqa: PLW0603 - one store per process, like the Temporal gateway
    if _store is None:
        settings = get_settings()
        url = settings.object_store_url
        if settings.fakes or url.startswith("memory://"):
            _store = FakeObjectStore(url if url.startswith("memory://") else "memory://choregos")
        else:
            _store = build_object_store(
                settings.object_store_kind,
                url,
                endpoint=settings.object_store_endpoint,
                region=settings.object_store_region,
                access_key=settings.object_store_access_key,
                secret_key=settings.object_store_secret_key,
                session_token=settings.object_store_session_token,
                account_key=settings.object_store_account_key,
                sas_token=settings.object_store_sas_token,
            )
    return _store


def use_object_store(store: ObjectStore | None) -> None:
    """Replaces the store (tests), or forgets it so the next call rebuilds it from the settings."""
    global _store  # noqa: PLW0603 - one store per process, like the Temporal gateway
    _store = store
