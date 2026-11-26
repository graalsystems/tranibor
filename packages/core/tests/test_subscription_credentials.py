"""A person's model subscription is recognised by its shape, and named, never echoed (S87-20, ADR 0227)."""

from __future__ import annotations

import pytest
from tranibor_core.gates import scan_secrets
from tranibor_core.redaction import redact
from tranibor_core.subscription_credentials import (
    SCRUBBED,
    SubscriptionCredentialInEnvironmentError,
    SubscriptionCredentialRefusedError,
    refuse,
    refuse_in_environment,
    refuse_in_values,
    scrub,
    shapes_in,
)

#: Shapes of the real credentials, with invented characters.
CLAUDE_ACCESS = "sk-ant-oat01-" + "Zq7" * 30
CLAUDE_REFRESH = "sk-ant-ort01-" + "Wx4" * 30
GOOGLE_ACCESS = "ya29." + "a0AfB_" * 10
GOOGLE_REFRESH = "1//0" + "gLm9" * 12
CREDENTIALS_FILE = '{"claudeAiOauth": {"accessToken": "x"}}'


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (f"export CLAUDE_CODE_OAUTH_TOKEN={CLAUDE_ACCESS}", "Claude subscription token"),
        (CLAUDE_REFRESH, "Claude subscription token"),
        (f"Authorization: Bearer {GOOGLE_ACCESS}", "Google OAuth access token"),
        (f'"refresh_token": "{GOOGLE_REFRESH}"', "Google OAuth refresh token"),
        (CREDENTIALS_FILE, "Claude credentials file"),
    ],
)
def test_each_shape_is_found(text: str, label: str) -> None:
    assert label in shapes_in(text)


@pytest.mark.parametrize(
    "text",
    [
        "sk-ant-api03-" + "k" * 40,  # an Anthropic API key: a secret, not a subscription
        "the run took 1//0 minutes",  # too short to be a refresh token
        "path/1//0a/b",
        "ya29 is a word",
        "claudeAiOauth without a colon",
        "",
    ],
)
def test_what_is_not_a_subscription(text: str) -> None:
    assert shapes_in(text) == []


def test_scrub_replaces_the_tokens_and_counts_them() -> None:
    text = f"a {CLAUDE_ACCESS} b {GOOGLE_ACCESS} c {GOOGLE_REFRESH}"
    scrubbed, count = scrub(text)
    assert count == 3
    assert CLAUDE_ACCESS not in scrubbed and GOOGLE_ACCESS not in scrubbed and GOOGLE_REFRESH not in scrubbed
    assert scrubbed.count(SCRUBBED) == 3


def test_a_refusal_names_the_shape_and_where_never_the_value() -> None:
    with pytest.raises(SubscriptionCredentialRefusedError) as refused:
        refuse(f"key={CLAUDE_ACCESS}", where="the secret api_key")
    message = str(refused.value)
    assert "the secret api_key" in message and "Claude subscription token" in message
    assert CLAUDE_ACCESS not in message and "ADR 0227" in message


def test_nested_values_are_read_keys_included() -> None:
    with pytest.raises(SubscriptionCredentialRefusedError) as refused:
        refuse_in_values({"a": [1, {"b": ("x", GOOGLE_REFRESH)}]}, where="a configuration")
    assert refused.value.labels == ["Google OAuth refresh token"]
    with pytest.raises(SubscriptionCredentialRefusedError):
        refuse_in_values({CLAUDE_ACCESS: "value"}, where="a configuration")
    refuse_in_values({"a": [1, None, {"b": "nothing here"}]}, where="a configuration")


def test_a_platform_process_refuses_to_start_with_one_named_by_variable() -> None:
    with pytest.raises(SubscriptionCredentialInEnvironmentError) as refused:
        refuse_in_environment("API", {"CLAUDE_CODE_OAUTH_TOKEN": "anything", "PATH": "/bin"})
    assert "CLAUDE_CODE_OAUTH_TOKEN" in str(refused.value) and "the API refuses to start" in str(
        refused.value
    )
    with pytest.raises(SubscriptionCredentialInEnvironmentError) as refused:
        refuse_in_environment("API", {"SOME_KEY": CLAUDE_ACCESS})
    assert "SOME_KEY" in str(refused.value) and CLAUDE_ACCESS not in str(refused.value)
    refuse_in_environment("API", {"CLAUDE_CODE_OAUTH_TOKEN": " ", "PATH": "/bin"})


def test_the_no_secrets_gate_and_the_log_redaction_see_the_google_shapes() -> None:
    assert "Google OAuth access token" in scan_secrets(GOOGLE_ACCESS)
    assert "Google OAuth refresh token" in scan_secrets(GOOGLE_REFRESH)
    assert "Anthropic key" in scan_secrets(CLAUDE_ACCESS)
    for token in (GOOGLE_ACCESS, GOOGLE_REFRESH, CLAUDE_ACCESS):
        assert token not in redact(f"the agent printed {token} here")
