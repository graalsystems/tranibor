"""S87-07: the facts the runner relies on to run Claude Code on a subscription, and where each was read.

The fixture is read by the stories that recognise a signed-out CLI or a usage limit (S87-22, S87-23) and that
build the CLI's environment (S87-21). Each fact carries the source it was read from; a measured fixture, when
the subscription's owner has run `trials/2026-12-personal-subscription/run.sh`, holds no token.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tranibor_core.subscription_credentials import shapes_in

FIXTURES = Path(__file__).parent / "fixtures" / "subscription"


def _from_source() -> dict[str, Any]:
    facts: dict[str, Any] = json.loads((FIXTURES / "from_source.json").read_text("utf-8"))
    return facts


def test_every_fact_says_where_it_was_read() -> None:
    facts = _from_source()
    assert facts["provenance"] == "from_source" and facts["measured"] is False
    for section in ("credential", "initialize", "signed_out", "usage_limit", "provider_hosts"):
        assert facts[section]["sources"], f"{section} cites no source"


def test_the_pinned_adapter_is_the_one_read() -> None:
    lock = (Path(__file__).parents[1] / "src" / "tranibor_runner" / "backends" / "versions.lock").read_text()
    pinned = next(line.split("=", 1)[1] for line in lock.splitlines() if line.startswith("claude-agent-acp="))
    assert _from_source()["packages"]["@agentclientprotocol/claude-agent-acp"] == pinned


def test_what_the_runner_must_remove_and_recognise() -> None:
    facts = _from_source()
    assert {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"} <= set(facts["credential"]["outrank_a_subscription"])
    assert facts["signed_out"]["acp_error_code"] == -32000
    assert all(facts["signed_out"]["marker"] in text for text in facts["signed_out"]["result_texts"])
    assert "You've hit your" in facts["usage_limit"]["prefixes"]


def test_no_fixture_holds_a_credential() -> None:
    for path in sorted(FIXTURES.glob("*.json")):
        assert shapes_in(path.read_text("utf-8")) == [], f"{path.name} holds a subscription credential"
