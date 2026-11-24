"""The reference pages name what a reader must type today (S80-42, ADR 0176 R4, ADR 0186).

A page that tells a reader which variable to set, or which repository to add, must name the current one:
``TRANIBOR_*`` (the ``CHOREGOS_*`` name is a fallback of the window), the core repository. The old
names that stay on a page are listed, each with its reason. History (ADRs, plan, changelogs) is not read.
"""

from __future__ import annotations

import pathlib
import re

from tranibor_core.publishing import PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
HISTORY = ("docs/adr/", "docs/plan/", "docs/site/node_modules/")

#: The former variables a page may still spell, and why.
FORMER_VARIABLES = {
    "CHOREGOS_ENV": "what the chart renders; ADR 0176 R4's helper maps it",
    "CHOREGOS_SERVER_TOKEN": "the plugin-server token's fallback name (docs/plugins.md)",
    "CHOREGOS_MEMORY_SCOPED_BY_ID": "read under this name only: no TRANIBOR_ alias (builtins.py)",
}


def _pages() -> list[pathlib.Path]:
    pages = [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]
    return [p for p in pages if not p.relative_to(ROOT).as_posix().startswith(HISTORY)]


def test_pages_name_no_former_variable_but_the_listed_ones() -> None:
    offenders = {
        f"{page.relative_to(ROOT)}: {name}"
        for page in _pages()
        for name in re.findall(r"\bCHOREGOS_[A-Z0-9_]+", page.read_text(encoding="utf-8"))
        if name not in FORMER_VARIABLES
    }
    assert not offenders, (
        f"use the TRANIBOR_ name, or list the exception with its reason: {sorted(offenders)}"
    )


def test_the_plugin_marketplace_is_added_from_the_core_repository() -> None:
    offenders = [
        str(page.relative_to(ROOT))
        for page in _pages()
        if f"marketplace add {PUBLISHING.owner}/choregos" in page.read_text(encoding="utf-8")
    ]
    assert not offenders, f"the marketplace is the core repository: {offenders}"


def test_no_page_says_the_graalsystems_packages_are_private() -> None:
    offenders = [
        str(page.relative_to(ROOT))
        for page in _pages()
        if re.search(r"While that package is private", page.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"the published packages are public: {offenders}"
