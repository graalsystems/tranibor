# SPDX-License-Identifier: Apache-2.0
"""The concepts page names every guarantee the plugin's extension registers (S22-20, ADR 0181 round C2).

Moved from `tests/docs/test_guarantee_list.py` (S83-05n): the twelve are the plugin's, read from its own
module; the core's half of that file stays with the core.
"""

from __future__ import annotations

import re
from pathlib import Path

from tranibor_software_delivery.gates import GATES

CONCEPTS = Path(__file__).resolve().parents[3] / "docs" / "concepts.md"


def test_every_software_delivery_guarantee_is_named_in_the_concepts() -> None:
    """The twelve the `software-delivery` plugin's extension registers (ADR 0181, round C2)."""
    text = CONCEPTS.read_text(encoding="utf-8")
    block = re.search(r"are the `software-delivery` plugin's.*?```\n(.*?)```", text, re.S)
    assert block is not None, "the plugin's list of guarantees left docs/concepts.md"
    named = {name.strip() for name in re.split(r"[·\n]", block.group(1)) if name.strip()}
    assert named == set(GATES)
