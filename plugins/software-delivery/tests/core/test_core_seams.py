# SPDX-License-Identifier: Apache-2.0
"""What the `software-delivery` extension registers on the core's seams when it loads (ADR 0181, C0-C2;
moved from the core's suite, S83-05n): its twelve gates, its two step effects under the names histories
carry, and the inbound event type it took over from the core.

The core's suite proves the seams on stand-in plugins, and that a workflow naming this plugin's gate or
effect is refused naming it where the extension is absent (`packages/core/tests/test_gate_seam.py`,
`test_step_effects.py`, `packages/contracts/tests/test_open_lists.py`). These pin what this extension puts
there.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import pytest
from tranibor_contracts import INBOUND_EVENT_TYPES, STEP_EFFECTS, InboundEvent
from tranibor_core.gates import _NEEDS, PLUGIN_GATES, gate_plugin, unavailable
from tranibor_core.step_effects import (
    CORE_EFFECTS,
    ENTERED,
    PASSED,
    StepEffectRequest,
    known_step_effects,
    step_effect,
    step_effect_of,
)


async def _close(request: StepEffectRequest) -> Mapping[str, Any]:
    return {}


@pytest.fixture
def other() -> Iterator[str]:
    yield "other"
    INBOUND_EVENT_TYPES.forget("other")


def test_the_extension_serves_the_twelve_gates() -> None:
    assert unavailable("ci_green") is None
    assert {name: gate_plugin(name) for name in PLUGIN_GATES} == dict(PLUGIN_GATES)


def test_the_twelve_plugin_gates_are_the_ones_that_need_the_scm() -> None:
    assert set(PLUGIN_GATES) == {name for name, needs in _NEEDS.items() if "scm" in needs}
    assert len(PLUGIN_GATES) == 12


def test_open_pr_and_merge_pr_are_registered_by_the_extension_under_their_names() -> None:
    """Registered by its extension since ADR 0181 round C2 (the suite registers it, as the API and the
    worker do)."""
    assert set(CORE_EFFECTS) <= set(known_step_effects())
    opened, merged = step_effect_of("open_pr"), step_effect_of("merge_pr")
    assert opened is not None and merged is not None
    assert (opened.when, merged.when) == (ENTERED, PASSED)
    assert opened.needs == merged.needs == {"scm"}
    assert opened.plugin == merged.plugin == "software-delivery"
    assert STEP_EFFECTS.plugin_of("open_pr") == "software-delivery"


def test_another_plugin_cannot_take_merge_pr() -> None:
    with pytest.raises(ValueError, match="already registered by the plugin `software-delivery`"):
        step_effect("merge_pr", gesture="x", when=PASSED, plugin="other", execute=_close)


def test_the_extension_declares_scm_pr_opened_and_another_plugin_cannot(other: str) -> None:
    """`scm.pr.opened` is the plugin's alone since the window C2 closed (S83-04r): valid, as a string."""
    with pytest.raises(ValueError, match="already declared by the plugin `software-delivery`"):
        INBOUND_EVENT_TYPES.declare("scm.pr.opened", plugin=other)
    assert "scm.pr.opened" not in INBOUND_EVENT_TYPES.core
    assert InboundEvent(type="scm.pr.opened", source="github", delivery_id="d").type == "scm.pr.opened"
