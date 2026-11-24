# SPDX-License-Identifier: Apache-2.0
"""The core names no operator (ADR 0157).

Choregos is installed and run by operators: a company that hosts an instance for its own teams, a
service provider that runs one for its customers. What one of them needed was written into the core
as if it were the platform's: an operator's name in a chart's comment, its hosts in a chart test, its
GitHub organisation in the console's demonstration data, its Jira site in a fixture. Read by anyone
else, it says the core is that operator's.

So the test reads every tracked file for the names below, in any case, and allows two things:

  * the two legal notices, `NOTICE` and `docs/TRADEMARK.md`, which name the companies they are
    about and change only with the notices themselves;
  * this file, which spells what it looks for.

An example in code, a test or the documentation uses `example.com`, `acme` or `globex`.

History is no longer an exception (ADR 0157, amended 2026-11-24): the ADRs, `docs/plan/` and the trial
records of `trials/` keep their dates and facts but say "the first operator's dev tenant", not whose,
and a recorded stack trace carries no person's home directory. The repository is published, and a
record is read by the same outsiders as the code.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: An operator's names: its company name (any case, any suffix: a host, a GitHub organisation, a
#: plugin), the Jira site a fixture was mirrored from, its internal products and registry, its
#: hosting provider, and the home directories of the workstations records were taken on. A neutral
#: home is `/home/user`.
OPERATOR = re.compile(
    r"diametral|fondation\.atlassian\.net|insights360|harbor\.build\.|layer4|\bmoji\b"
    r"|treydone|/home/(?!user\b)[a-z][a-z0-9_-]*/|-home-(?!user-|dir\b)[a-z][a-z0-9_]*-",
    re.IGNORECASE,
)
#: Legal notices naming the companies they are about.
NOTICES = frozenset({"NOTICE", "docs/TRADEMARK.md"})
#: This file, which has to spell what it looks for.
SELF = pathlib.Path(__file__).resolve().relative_to(ROOT).as_posix()


def _tracked_files() -> list[str]:
    listed = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    return [name for name in listed.decode().split("\0") if name]


def _offenders(names: list[str]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for name in names:
        path = ROOT / name
        if not path.is_file():
            continue
        text = path.read_bytes().decode("utf-8", errors="ignore")
        lines = [
            f"{number}: {line.strip()[:120]}"
            for number, line in enumerate(text.splitlines(), start=1)
            if OPERATOR.search(line)
        ]
        if lines:
            found[name] = lines
    return found


def test_there_is_a_tree_to_check() -> None:
    """Without this guard, a listing that returned nothing would make the suite green."""
    assert len(_tracked_files()) > 1000


def test_the_pattern_catches_an_operator_in_any_form() -> None:
    caught = (
        "choregos-api.internal.dev.DIAMETRAL.com",
        "DiametralGroup/choregos-sandbox-dev#4",
        "plugin:diametral-delivery/x",
        "https://fondation.atlassian.net/rest/api/3/issue/1",
        "agentic.diametral.com",
        "the Insights360 MCP server",
        "harbor.build.example.org/choregos/api:1",
        "someone@layer4.fr",
        "hosted by MOJI",
        'File "/home/treydone/git/choregos/x.py"',
        'File "/home/devil/git/choregos/x.py"',
        "/tmp/claude-1000/-home-devil-git-choregos/x",
    )
    spared = (
        "choregos-api.internal.dev.example.com",
        "acme/choregos-sandbox-dev#4",
        "a diameter",
        "the emoji counts",
        "mountPath: /home/user/.local/share/buildkit",
        'File "/home/user/src/choregos/x.py"',
        "--home-dir",
        "analytics-mcp",
    )
    assert all(OPERATOR.search(text) for text in caught)
    assert not any(OPERATOR.search(text) for text in spared)


def test_the_core_names_no_operator() -> None:
    files = [name for name in _tracked_files() if name not in NOTICES and name != SELF]
    offenders = _offenders(files)
    assert not offenders, (
        "The core names an operator:\n  "
        + "\n  ".join(f"{name}:{line}" for name, lines in sorted(offenders.items()) for line in lines)
        + "\nUse a neutral example (example.com, acme, globex, /home/user); the legal notices are the "
        "only exception, history included (tests/licences/test_no_operator_in_core.py, ADR 0157)."
    )


def test_every_exception_is_still_needed() -> None:
    """A notice that stops naming an operator leaves the list: the exceptions only shrink."""
    stale = sorted(NOTICES - set(_offenders(sorted(NOTICES))))
    assert not stale, f"no longer names an operator, remove it from NOTICES: {stale}"
