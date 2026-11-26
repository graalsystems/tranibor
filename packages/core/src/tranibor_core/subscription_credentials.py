# SPDX-License-Identifier: Apache-2.0
"""A person's model subscription never reaches the platform (ADR 0227).

A Claude or Gemini subscription is the person's own: the vendors' terms let the unmodified vendor CLI,
signed in by that person on their own machine, use it for their own work, and forbid a platform to
collect, store, relay or intermediate it. On a personal runner the token lives in the runner's keychain
and reaches only the child environment of the vendor CLI (S87-22). Everything on the platform side
refuses it here, by its shape:

- the run's API refuses a request body that carries one (`tranibor_api.subscription_ingress`);
- the secret store and the connector, secret and launch-environment writes refuse one with a 422;
- the `direct` gateway refuses a key of that shape in every environment;
- the API, the outposts' door and the orchestrator's workers refuse to start with one in their own
  environment.

The shapes are those of the credential, never a value: a Claude OAuth access or refresh token
(`sk-ant-oat01-…`, `sk-ant-ort01-…`, which `claude setup-token` prints), a Google OAuth access token
(`ya29.…`) and refresh token (`1//0…`), and the key of Claude Code's credentials file (`claudeAiOauth`).
An Anthropic API key (`sk-ant-api03-…`) is not a subscription: it is refused elsewhere as a secret, not
here. A refusal names the shape and where it was found, never the value.

What a shape cannot catch (an encoded, split or compressed copy) is the runner's job: it scrubs every
body it sends by the token's exact value (S87-21), and the canary of S87-33 searches for encoded forms.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

#: (label, pattern) of each subscription credential shape. The labels are what a refusal says.
SUBSCRIPTION_SHAPES: tuple[tuple[str, str], ...] = (
    ("Claude subscription token", r"sk-ant-o[ar]t\d{2}-[A-Za-z0-9_-]{20,}"),
    ("Google OAuth access token", r"\bya29\.[A-Za-z0-9_-]{20,}"),
    ("Google OAuth refresh token", r"(?<![A-Za-z0-9])1//0[A-Za-z0-9_-]{30,}"),
    ("Claude credentials file", r"[\"']claudeAiOauth[\"']\s*:"),
)
#: The shapes that are token values (the credentials-file key is a marker, not a value): the ones the
#: `no_secrets` gate and the log redaction also mask.
TOKEN_SHAPES: tuple[tuple[str, str], ...] = SUBSCRIPTION_SHAPES[:3]
#: The environment variables that hold a subscription by name, whatever their value looks like.
SUBSCRIPTION_VARIABLES = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
#: What a scrubbed token becomes.
SCRUBBED = "[SUBSCRIPTION CREDENTIAL REMOVED]"
#: The problem code of every refusal, as the API answers it.
REFUSAL_CODE = "subscription_credential_refused"

_COMPILED = tuple((label, re.compile(pattern)) for label, pattern in SUBSCRIPTION_SHAPES)


class SubscriptionCredentialRefusedError(ValueError):
    """A subscription credential reached a place of the platform. The message names shapes, never values."""

    def __init__(self, where: str, labels: list[str]) -> None:
        self.where = where
        self.labels = labels
        super().__init__(
            f"{where}: refused, it carries a model subscription credential ({', '.join(labels)}). A "
            "person's Claude or Gemini subscription stays on their own machine, in their personal runner "
            "(ADR 0227); the platform never stores, relays or forwards one. Use a provider API key through "
            "the model gateway instead."
        )


def shapes_in(text: str) -> list[str]:
    """The labels of the subscription shapes `text` holds, in the order of `SUBSCRIPTION_SHAPES`."""
    if not text:
        return []
    return [label for label, pattern in _COMPILED if pattern.search(text)]


def scrub(text: str) -> tuple[str, int]:
    """`text` with every subscription token replaced by `SCRUBBED`, and how many were replaced."""
    count = 0
    for _, pattern in _COMPILED[: len(TOKEN_SHAPES)]:
        text, replaced = pattern.subn(SCRUBBED, text)
        count += replaced
    return text, count


def refuse(text: str, *, where: str) -> None:
    """Raise `SubscriptionCredentialRefusedError` when `text` holds a subscription shape."""
    labels = shapes_in(text)
    if labels:
        raise SubscriptionCredentialRefusedError(where, labels)


def refuse_in_values(values: object, *, where: str) -> None:
    """`refuse` over every string of a nested structure (a JSON body, a configuration, an environment)."""
    found: list[str] = []
    _walk(values, found)
    if found:
        raise SubscriptionCredentialRefusedError(where, sorted(set(found), key=found.index))


def _walk(value: object, found: list[str]) -> None:
    if isinstance(value, str):
        found.extend(shapes_in(value))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _walk(key, found)
            _walk(item, found)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            _walk(item, found)


class SubscriptionCredentialInEnvironmentError(RuntimeError):
    """A platform process was started with a subscription credential in its environment."""


def refuse_in_environment(process: str, environ: Mapping[str, str] | None = None) -> None:
    """Stop a platform process whose own environment holds a subscription credential.

    A platform process (the API, the outposts' door, the orchestrator's workers) never needs one: a
    subscription is used by the vendor CLI on its owner's personal runner, which is not a platform
    process and does not call this. The message names the variables, never their values.
    """
    values = os.environ if environ is None else environ
    named = sorted(name for name in SUBSCRIPTION_VARIABLES if values.get(name, "").strip())
    shaped = sorted(name for name, value in values.items() if name not in named and shapes_in(value))
    if not (named or shaped):
        return
    raise SubscriptionCredentialInEnvironmentError(
        f"the {process} refuses to start: its environment holds a model subscription credential in "
        f"{', '.join(named + shaped)}. A person's subscription stays on their own machine, in their "
        "personal runner (ADR 0227); a platform process never holds one. Remove the variable (a "
        "Kubernetes Secret or a compose `.env` that carries it), and use a provider API key through the "
        "model gateway for the platform's own model calls."
    )
