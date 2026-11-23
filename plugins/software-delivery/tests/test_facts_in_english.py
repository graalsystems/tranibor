# SPDX-License-Identifier: Apache-2.0
"""The diff's helpers keep their English names (S45-39, ADR 0098), where they live since ADR 0181 round C2.

Moved from `apps/orchestrator/tests/test_activities_in_english.py` (S83-05n): the helper is the plugin's,
the core's helpers stay guarded there.
"""

from __future__ import annotations

from tranibor_software_delivery import facts


def test_the_english_diff_helpers_are_defined() -> None:
    assert callable(facts.added_text_of)
    assert "added_text_of" in facts.__all__
