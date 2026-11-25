"""ADR 0222: the one comparison that refuses an unisolated agent in staging and prod, and the opt-out."""

from __future__ import annotations

import pytest
from tranibor_core.agent_isolation import (
    AGENT_ISOLATION_UNAVAILABLE,
    agent_isolation_none_accepted,
    agent_isolation_refusal,
    unisolated_agent_refusal,
)


@pytest.mark.parametrize("environment", ["staging", "prod", " Prod "])
def test_process_in_production_is_refused_naming_the_setting(environment: str) -> None:
    message = unisolated_agent_refusal(environment, "process", "tekton", accepted=False)
    assert message is not None and message.startswith(AGENT_ISOLATION_UNAVAILABLE)
    for wanted in ("TRANIBOR_AGENT_ISOLATION=none-accepted", "acceptUnisolatedAgent", "`tekton`", "ADR 0222"):
        assert wanted in message


@pytest.mark.parametrize("environment", ["dev", "test", "", None])
def test_outside_production_nothing_is_refused(environment: str | None) -> None:
    assert unisolated_agent_refusal(environment, "process", "tekton", accepted=False) is None


def test_container_and_the_opt_out_are_not_refused() -> None:
    assert unisolated_agent_refusal("prod", "container", "k8s_job", accepted=False) is None
    assert unisolated_agent_refusal("prod", "process", "aca", accepted=True) is None


def test_only_the_named_word_opts_out() -> None:
    assert agent_isolation_none_accepted(None) is False
    assert agent_isolation_none_accepted("  ") is False
    assert agent_isolation_none_accepted("none-accepted") is True
    for typo in ("none", "true", "1", "None-Accepted"):
        with pytest.raises(ValueError, match="none-accepted"):
            agent_isolation_none_accepted(typo)


def test_the_capability_refusal_tells_production_what_else_it_needs() -> None:
    message = agent_isolation_refusal("container", "aca", frozenset())
    assert message is not None and "TRANIBOR_AGENT_ISOLATION=none-accepted" in message
