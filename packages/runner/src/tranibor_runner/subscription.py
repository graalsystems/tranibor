# SPDX-License-Identifier: Apache-2.0
"""A run on its person's own subscription, as the runner keeps it (ADR 0227 §2, §5, §6).

The daemon of a personal outpost starts the runner for a run it claimed with
`TRANIBOR_RUN_PLACE=outpost:<id>`, `TRANIBOR_RUN_CREDENTIAL=subscription`, `TRANIBOR_RUN_PROVIDER`
(`anthropic`, `google`) and the vendor's token in the runner's own environment (`CLAUDE_CODE_OAUTH_TOKEN` for
`anthropic`). What the runner does about it:

- such a run anywhere but at an outpost stops before anything of it starts (`at_an_outpost`): the
  orchestrator refuses first (`start_run`), the runner repeats it on its own;
- its agent gets no model listener and none of the gateway's variables (`GATEWAY_VARIABLES`): an API key
  outranks a subscription in Claude Code (the S87-07 fixture, `fixtures/subscription/from_source.json`), and
  the run was placed for the subscription;
- its agent, the vendor's CLI, gets the token by name (`vendor_environment`), the provider's own model id
  and a configuration directory of the runner's (`Backend.subscription_model`, `Backend.subscription_env`);
  nothing else the runner starts gets it (`config.agent_base_environment`), nor a gateway run's agent;
- its CLI signed out (ACP `auth_required`, "Please run /login") ends it `subscription_login_required`, before
  its first prompt or after the turn that said so, and its person is told how to sign in again (`SIGN_IN`);
- the runner's one client to the platform (`platform_client`) scrubs every URL and body it sends by the
  token's exact value and by the subscription shapes (`Scrubber`), and the run journals how many it took out
  (`subscription.token_redacted`), never what;
- so does the MCP listener the agent's servers are reached through (`loopback/mcp.py`), for every URL, body
  and header of the agent's it forwards;
- a push whose commits hold the value or a shape is refused before `git push` (`Scrubber.held_in`), merges
  read against their first parent, the whole difference read too, and the objects as they are, not their
  replacements (`Workspace.patch_since`);
- a deliverable of `.choregos/outputs/` that holds either is not uploaded (`OUTPUT_REFUSED_REASON`): sent
  scrubbed, it would no longer be the file the agent made, nor match the digest the platform checks.

What the exact value cannot catch (a copy split, encoded or compressed) is recorded in ADR 0227, not
prevented. A gateway run meets none of this: its client sends what it is given, as before.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from itertools import chain
from types import MappingProxyType
from urllib.parse import quote, quote_plus

import httpx
from tranibor_contracts.enums import SubscriptionProvider
from tranibor_core.subscription_credentials import SCRUBBED, SUBSCRIPTION_VARIABLES, scrub, shapes_in

from .client import InternalClient
from .config import RunnerSettings

#: What the daemon of a personal outpost puts before the outpost's id in `TRANIBOR_RUN_PLACE`.
OUTPOST_PLACE = "outpost:"
#: The variable of the runner's environment that holds each provider's token.
TOKEN_VARIABLES: Mapping[SubscriptionProvider, tuple[str, ...]] = MappingProxyType(
    {
        SubscriptionProvider.ANTHROPIC: ("CLAUDE_CODE_OAUTH_TOKEN",),
        # No sign-in path yet (ADR 0227 §12): placement refuses `google`, and only its shapes are scrubbed.
        SubscriptionProvider.GOOGLE: (),
    }
)
#: Every variable that holds a person's subscription token: the providers' and the platform's list
#: (`SUBSCRIPTION_VARIABLES`). A gateway run's agent never gets one, wherever it would come from.
ALL_TOKEN_VARIABLES = frozenset(chain(*TOKEN_VARIABLES.values())) | SUBSCRIPTION_VARIABLES
#: What the agent of a subscription run never gets, whatever the runner's own environment or the step holds:
#: the gateway's key and address under each backend's names. `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN`
#: would silently take precedence over the subscription (S87-07); the others name another credential or
#: endpoint than the one the run was placed for.
GATEWAY_VARIABLES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GEMINI_API_KEY",
        "GOOGLE_GEMINI_BASE_URL",
    }
)
#: The journal's event for what the scrub took out: `{"count": n}`, never a value.
REDACTED_EVENT = "subscription.token_redacted"
#: `run.failed`'s reason when a subscription run is not at an outpost.
OFF_RUNNER_REASON = "subscription_off_runner"
#: The reason of a result whose push was refused because its commits hold the token or a shape.
PUSH_REFUSED_REASON = "subscription_credential_in_diff"
#: The reason of a result whose commits the runner could not read before pushing them: refused all the same.
PUSH_UNREAD_REASON = "subscription_push_unread"
#: The reason a deliverable of `.choregos/outputs/` is not uploaded, and its `done` result fails: it holds the
#: token or a shape.
OUTPUT_REFUSED_REASON = "subscription_credential_in_output"
#: The journal's event for such a deliverable: its file, the reason, whether the exact value was there and the
#: shapes' labels, never a value.
OUTPUT_REFUSED_EVENT = "outputs.refused"
#: What its person does when a subscription run's vendor CLI is signed out (`subscription_login_required`): on
#: their own machine, since no page of the platform takes a token (ADR 0227 §4).
SIGN_IN: Mapping[SubscriptionProvider, str] = MappingProxyType(
    {SubscriptionProvider.ANTHROPIC: "`claude setup-token`, then `choregos-outpost personal login claude`"}
)
#: The shortest value scrubbed by its exact value. A token is far longer; replacing a short value everywhere
#: would only wreck what the runner sends, and hide nothing.
MIN_EXACT_VALUE = 16


def at_an_outpost(place: str) -> bool:
    """Whether `TRANIBOR_RUN_PLACE` names an outpost: `outpost:` and an id."""
    return place.startswith(OUTPOST_PLACE) and bool(place[len(OUTPOST_PLACE) :].strip())


def signed_out_summary(said: str, provider: SubscriptionProvider | None) -> str:
    """What a `subscription_login_required` result tells its person: their CLI is signed out (what said so,
    the CLI's words or ACP's error), and how they sign in again on their own runner."""
    how = SIGN_IN.get(provider) if provider else None
    return (
        f"the vendor's CLI is signed out of your subscription ({said[:200]}): sign in again on your runner"
        + (f", with {how}" if how else "")
    )


def token_values(
    provider: SubscriptionProvider | None, environ: Mapping[str, str] | None = None
) -> list[str]:
    """The token of `provider` as the runner's environment holds it (every known one without a provider)."""
    names = TOKEN_VARIABLES.get(provider, ()) if provider else tuple(chain(*TOKEN_VARIABLES.values()))
    values = os.environ if environ is None else environ
    return [value for name in names if (value := values.get(name, "").strip())]


def vendor_environment(
    provider: SubscriptionProvider | None, environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The token of `provider` under its own variable, as the vendor's CLI reads it (ADR 0227 §4): what the
    runner hands, by name, to the agent of a subscription run and to nothing else. `agent_base_environment`
    leaves it out of everything the runner starts; empty without a provider or a token."""
    values = os.environ if environ is None else environ
    names = TOKEN_VARIABLES.get(provider, ()) if provider else ()
    return {name: value for name in names if (value := values.get(name, "").strip())}


class Scrubber:
    """The token's exact value, and the subscription shapes, taken out of what the runner sends the platform.

    The value is looked for as it is and as JSON and a URL would write it. Each copy taken out is counted;
    `take` hands the count since its last call to the journal, which records the number only."""

    def __init__(self, values: Iterable[str]) -> None:
        forms: list[str] = []
        for value in values:
            if len(value) < MIN_EXACT_VALUE:
                continue
            for form in (value, json.dumps(value)[1:-1], quote(value, safe=""), quote_plus(value)):
                if form not in forms:
                    forms.append(form)
        # The longest first: a form that holds another is taken out whole.
        self.forms = tuple(sorted(forms, key=len, reverse=True))
        #: Every copy taken out since the run began.
        self.redacted = 0
        self._unjournaled = 0

    def held_in(self, text: str) -> tuple[bool, list[str]]:
        """Whether `text` holds the exact value, and the labels of the shapes it holds."""
        return any(form in text for form in self.forms), shapes_in(text)

    def held_in_data(self, data: bytes) -> tuple[bool, list[str]]:
        """`held_in` over bytes, read the way `data` reads them: whatever `data` would change, this finds."""
        return self.held_in(data.decode("utf-8", "surrogateescape"))

    def text(self, text: str) -> str:
        """`text` without the value nor any subscription token; what it took out is counted."""
        count = 0
        for form in self.forms:
            if form in text:
                count += text.count(form)
                text = text.replace(form, SCRUBBED)
        text, shaped = scrub(text)
        count += shaped
        self.redacted += count
        self._unjournaled += count
        return text

    def data(self, data: bytes) -> bytes:
        """`data` scrubbed as text, every other byte left as it was (a body need not be UTF-8)."""
        if not data:
            return data
        decoded = data.decode("utf-8", "surrogateescape")
        scrubbed = self.text(decoded)
        return data if scrubbed == decoded else scrubbed.encode("utf-8", "surrogateescape")

    async def request(self, request: httpx.Request) -> httpx.Request:
        """The request with its URL and body scrubbed: itself when there was nothing to take out."""
        url = self.text(str(request.url))
        body = await request.aread()
        cleaned = self.data(body)
        if url == str(request.url) and cleaned is body:
            return request
        headers = httpx.Headers(request.headers)
        if "content-length" in headers:
            del headers["content-length"]  # the scrubbed body's own is set below
        return httpx.Request(
            request.method, url, headers=headers, content=cleaned, extensions=request.extensions
        )

    def take(self) -> int:
        """How many copies were taken out since the last call."""
        count, self._unjournaled = self._unjournaled, 0
        return count


def platform_client(
    settings: RunnerSettings,
    scrubber: Scrubber | None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> InternalClient:
    """The runner's one client to the platform: events, result, outputs, the tool server's calls and the tool
    bridge, questions, scope changes, the ticket, knowledge, the git and egress tokens. With a `scrubber` (a
    subscription run), every request it sends goes through it first, whatever method made it."""
    return InternalClient(
        settings.api_url,
        settings.run_id,
        settings.run_token,
        transport=transport,
        rewrite=None if scrubber is None else scrubber.request,
    )
