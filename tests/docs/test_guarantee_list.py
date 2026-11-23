"""The concepts page names every guarantee of the core (S22-20).

The "Gates shipped today" list of `docs/concepts.md` had fallen behind: `outputs_in`,
`markdown_sections` and `action_succeeded` shipped, absent from the page. A guarantee that the
reference documentation does not name does not exist for whoever writes a workflow.

The `software-delivery` plugin's twelve are checked by its own suite
(`plugins/software-delivery/tests/test_concepts_name_the_guarantees.py`, ADR 0181 round D).
"""

from __future__ import annotations

import re
from pathlib import Path

from tranibor_core.gates import _REGISTRY

CONCEPTS = Path(__file__).resolve().parents[2] / "docs" / "concepts.md"


def test_every_core_guarantee_is_named_in_the_concepts() -> None:
    # The core's guarantees only: an extension or a test can register others.
    core_names = {name for name, fn in _REGISTRY.items() if fn.__module__ == "tranibor_core.gates"}
    assert "evidence_present" in core_names and "adr_number_free" not in core_names
    text = CONCEPTS.read_text(encoding="utf-8")
    block = re.search(r"Gates shipped today:\n\n```\n(.*?)```", text, re.S)
    assert block is not None, "the list of guarantees left docs/concepts.md"
    named = {name.strip() for name in re.split(r"[·\n]", block.group(1)) if name.strip()}
    assert sorted(core_names - named) == [], "guarantees missing from docs/concepts.md"
    assert sorted(named - core_names) == [], "named guarantees that do not exist"
