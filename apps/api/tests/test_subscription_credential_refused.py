"""A person's model subscription never reaches the platform: every door refuses it (S87-20, ADR 0227).

The run's API refuses a body that carries one before any route reads it; a connector, a secret and an
agent's environment refuse one when written; the `direct` gateway refuses one as its key in every
environment. Each refusal names the shape, never the value.
"""

from __future__ import annotations

import base64
import os
from typing import Any

import pytest
from httpx import AsyncClient

CLAUDE_ACCESS = "sk-ant-oat01-" + "Zq7" * 30
GOOGLE_ACCESS = "ya29." + "a0AfB_" * 10
ORG = "/api/v1/orgs/northwind/connectors"


@pytest.fixture(autouse=True)
def sealed_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_core.redaction import forget_secrets

    monkeypatch.setenv("TRANIBOR_SECRETS_KEKS", f"k1:{base64.b64encode(os.urandom(32)).decode()}")
    forget_secrets()


async def test_the_runs_api_refuses_a_body_that_carries_a_subscription(client: AsyncClient) -> None:
    """Before authentication and before any route: nothing of the body is read, stored or echoed."""
    answer = await client.post(
        "/api/v1/internal/runs/run-1/events", json={"events": [{"text": f"token {CLAUDE_ACCESS}"}]}
    )
    assert answer.status_code == 422
    problem = answer.json()
    assert problem["code"] == "subscription_credential_refused"
    assert problem["shapes"] == ["Claude subscription token"]
    assert CLAUDE_ACCESS not in answer.text


async def test_a_body_without_one_reaches_the_route(client: AsyncClient) -> None:
    """The guard hands the body on unchanged: the route answers as it would (here, no run token)."""
    answer = await client.post("/api/v1/internal/runs/run-1/events", json={"events": [{"text": "hello"}]})
    assert answer.status_code != 422


async def test_a_connector_refuses_one_in_a_field_or_a_secret(client: AsyncClient, admin: str) -> None:
    for body in (
        {"config": {"base_url": "https://llm.acme.example/v1", "note": GOOGLE_ACCESS}, "secret_values": {}},
        {"config": {"base_url": "https://llm.acme.example/v1"}, "secret_values": {"api_key": CLAUDE_ACCESS}},
    ):
        refused = await client.post(
            ORG,
            json={
                "name": "acme-llm",
                "kind": "gateway",
                "type": "openai-compatible",
                "allow_egress": True,
                **body,
            },
        )
        assert refused.status_code == 422, refused.text
        assert "subscription credential" in refused.text
        assert CLAUDE_ACCESS not in refused.text and GOOGLE_ACCESS not in refused.text


async def test_the_secret_store_refuses_one_whoever_writes(app: Any) -> None:
    from tranibor_api.secret_store import get_secret_store
    from tranibor_core.subscription_credentials import SubscriptionCredentialRefusedError

    store = get_secret_store()
    with pytest.raises(SubscriptionCredentialRefusedError):
        await store.put(None, "org-1", "connector/x/api_key", f"  {CLAUDE_ACCESS}\n", "a@b.c")  # type: ignore[arg-type]


def test_a_hosted_agent_environment_refuses_one() -> None:
    from pydantic import ValidationError
    from tranibor_api.schemas.agents import HostedRuntime

    with pytest.raises(ValidationError) as refused:
        HostedRuntime.model_validate(
            {"source": {"type": "image", "ref": "acme/agent:1"}, "env": {"MY_TOKEN": CLAUDE_ACCESS}}
        )
    assert "subscription credential" in str(refused.value) and CLAUDE_ACCESS not in str(refused.value)


def test_the_direct_gateway_refuses_one_as_its_key_in_every_environment() -> None:
    from tranibor_adapters.gateway.direct import DirectGateway
    from tranibor_core.subscription_credentials import SubscriptionCredentialRefusedError

    with pytest.raises(SubscriptionCredentialRefusedError):
        DirectGateway(key=CLAUDE_ACCESS)
    assert DirectGateway(key="sk-provider-key").key == "sk-provider-key"


def test_the_api_refuses_to_start_with_one_in_its_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_core.subscription_credentials import (
        SubscriptionCredentialInEnvironmentError,
        refuse_in_environment,
    )

    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", CLAUDE_ACCESS)
    with pytest.raises(SubscriptionCredentialInEnvironmentError):
        refuse_in_environment("API")


async def test_the_runs_api_refuses_one_in_a_url(client: AsyncClient) -> None:
    """A token in a path or a query string is refused too, whatever the method."""
    answer = await client.get(f"/api/v1/internal/runs/run-1/input?key={GOOGLE_ACCESS}")
    assert answer.status_code == 422
    assert answer.json()["shapes"] == ["Google OAuth access token"]
    assert GOOGLE_ACCESS not in answer.text
