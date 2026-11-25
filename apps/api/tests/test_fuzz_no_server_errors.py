"""Inputs a schema fuzzer (Schemathesis, docs/security-testing.md) turned into a 500, and which are refused.

Each case is the minimal request the tool shrank a finding to: a body that is not JSON on an authenticated
webhook, a `null` where a column is NOT NULL, a number the schema accepted and the configuration refused,
a card URL with no host.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

ORG = "/api/v1/orgs/northwind"


@pytest.mark.parametrize("body", [b"\x00", b"not json", b"[]", b'"text"', b"0", b"\xff\xfe"])
async def test_a_github_delivery_that_is_not_a_json_object_is_a_400(client: AsyncClient, body: bytes) -> None:
    sent = await client.post(
        "/api/v1/webhooks/github",
        content=body,
        headers={"X-GitHub-Event": "ping", "X-GitHub-Delivery": f"bad-{body!r}"},
    )
    assert sent.status_code == 400, sent.text
    assert sent.json()["detail"].startswith("the body")


async def test_an_alertmanager_delivery_that_is_not_json_is_a_400(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tranibor_api.config import reset_settings_cache

    monkeypatch.setenv("TRANIBOR_GENERIC_WEBHOOK_SECRET", "s3cret")
    reset_settings_cache()
    try:
        sent = await client.post(
            "/api/v1/webhooks/alertmanager", content=b"\x00", headers={"x-choregos-secret": "s3cret"}
        )
        assert sent.status_code == 400, sent.text
    finally:
        reset_settings_cache()


async def test_a_null_scope_on_a_knowledge_base_leaves_it_as_it_was(client: AsyncClient, admin: str) -> None:
    base = f"{ORG}/knowledge"
    created = await client.post(base, json={"slug": "fuzz", "name": "Fuzz"})
    assert created.status_code == 201, created.text
    before = created.json()["scope"]
    patched = await client.patch(f"{base}/fuzz", json={"scope": None, "status": "active"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["scope"] == before


@pytest.mark.parametrize("factor", [0, -1, 0.0])
async def test_a_model_profile_with_no_positive_turn_factor_is_a_422(
    client: AsyncClient, project: dict[str, Any], factor: float
) -> None:
    sent = await client.put(
        f"/api/v1/projects/{project['id']}/models",
        json={"profiles": {"strong": {"litellm_model": "platform/strong", "max_turns_factor": factor}}},
    )
    assert sent.status_code == 422, sent.text


@pytest.mark.parametrize("card_url", ["https://", "https:///card.json", "https://[", "https://a b/card.json"])
async def test_a_card_url_without_a_usable_host_is_a_422(
    client: AsyncClient, admin: str, card_url: str
) -> None:
    sent = await client.post(f"{ORG}/agents/discover", json={"card_url": card_url})
    assert sent.status_code == 422, sent.text
