# SPDX-License-Identifier: Apache-2.0
"""The A2A door's own metadata block under its new key and its former one (ADR 0226, R9c).

A caller names the skill under `metadata.tranibor.skill`, or, written for 0.29, under
`metadata.choregos.skill`: both pick the same skill. What the door writes is covered by
`test_a2a_inbound.py` (both keys, same values).
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from tranibor_api.a2a.door import Fault, Skill, _pick_skill

SERVED = [
    Skill(id="billing/ship", project=cast(Any, None), workflow="ship", description="", input_modes=[]),
    Skill(id="billing/fix", project=cast(Any, None), workflow="fix", description="", input_modes=[]),
]


@pytest.mark.parametrize("key", ["tranibor", "choregos"], ids=["new", "former"])
def test_the_skill_is_read_under_either_key(key: str) -> None:
    assert _pick_skill(SERVED, {key: {"skill": "fix"}}).workflow == "fix"


def test_the_new_key_wins_when_both_are_sent() -> None:
    both = {"tranibor": {"skill": "ship"}, "choregos": {"skill": "fix"}}
    assert _pick_skill(SERVED, both).workflow == "ship"


def test_a_block_under_another_key_names_no_skill() -> None:
    with pytest.raises(Fault):
        _pick_skill(SERVED, {"acme": {"skill": "fix"}})
