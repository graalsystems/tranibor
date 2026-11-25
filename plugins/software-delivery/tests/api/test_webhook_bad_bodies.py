# SPDX-License-Identifier: Apache-2.0
"""A Jenkins, Tekton or Argo CD delivery whose body is not a JSON object is a 400, not a 500.

Found by fuzzing the API against its contract (docs/security-testing.md): the senders are authenticated by a
shared secret, and a bug in one of them (or a proxy cutting the body) is theirs to read as a client error.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

SECRET = "s3cret-bad-bodies"


@pytest.fixture(autouse=True)
def secret(monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_GENERIC_WEBHOOK_SECRET", SECRET)
    reset_settings_cache()
    yield  # type: ignore[misc]
    reset_settings_cache()


@pytest.mark.parametrize("sink", ["jenkins", "tekton", "argocd"])
@pytest.mark.parametrize("body", [b"\x00", b"not json", b"[]", b"0"])
async def test_a_body_that_is_not_a_json_object_is_refused(
    client: AsyncClient, sink: str, body: bytes
) -> None:
    sent = await client.post(
        f"/api/v1/webhooks/{sink}",
        content=body,
        headers={"x-choregos-secret": SECRET, "ce-id": f"bad-{body!r}", "ce-type": "x"},
    )
    assert sent.status_code == 400, sent.text
