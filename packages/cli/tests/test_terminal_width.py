# SPDX-License-Identifier: Apache-2.0
"""A CLI test's verdict does not depend on the width of the terminal that runs the suite (#2280).

Rich wraps the CLI's output at the terminal's width, so a sentence an assertion looks for could be cut by a
long path on a narrow terminal. The suite pins the width (`conftest._a_terminal_width_of_its_own`); these
tests run tests that read such output under a narrow terminal, in a pytest of their own, since the width a
console keeps is the one of the process that imported the CLI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

#: Tests whose output Rich folded at 60 columns before the suite pinned the width.
FOLDED_ON_A_NARROW_TERMINAL = (
    "packages/cli/tests/test_validate_offline.py::test_a_plugin_s_gate_is_a_warning_offline",
    "packages/cli/tests/test_outposts.py::test_list_shows_status_instances_and_presence",
    "packages/cli/tests/test_outposts.py::test_calls_shows_states_and_refusal_codes_as_digests_only",
)


def test_a_narrow_terminal_does_not_change_a_verdict() -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COV_CORE_"))}
    env["COLUMNS"] = "40"  # what a narrow terminal exports, read when the CLI's consoles are built
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *FOLDED_ON_A_NARROW_TERMINAL],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]


def test_the_cli_s_consoles_draw_at_the_suite_s_width() -> None:
    from rich.console import Console
    from tranibor_cli import main, outposts

    assert isinstance(main.console, Console)
    assert main.console.width == outposts.console.width == 200  # `conftest.TEST_COLUMNS`
