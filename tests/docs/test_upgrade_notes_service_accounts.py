"""The 0.30.0 chart notes tell the operator to bind every renamed ServiceAccount before upgrading.

A ServiceAccount the chart renamed in 0.30.0 (ADR 0226 R6) loses every right granted outside the chart by
its former name: a Vault role, a Role laid down for the plugin servers, a workload identity. The notes
must name each one, or an operator upgrades into an API that cannot read its secrets (#2329).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT = ROOT / "docs" / "deployment.md"
CHART = ROOT / "charts" / "tranibor"
HEADING = "## Upgrading to 0.30.0: the chart takes the name tranibor"
PARAGRAPH = "**Bind the new ServiceAccounts before you upgrade.**"

_ACCOUNT = re.compile(r"kind:\s*ServiceAccount\s*\n(?:[^\n]*\n){0,3}?\s*name:\s*(tranibor-[a-z-]+)\s*$", re.M)


def _section() -> str:
    text = DEPLOYMENT.read_text(encoding="utf-8")
    assert HEADING in text, f"docs/deployment.md has no {HEADING!r}"
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def _paragraph() -> str:
    section = _section()
    assert PARAGRAPH in section, f"the R6 notes have no paragraph {PARAGRAPH!r}"
    return section.split(PARAGRAPH, 1)[1].split("\n\n", 1)[0]


def renamed_accounts() -> set[str]:
    """The `tranibor-*` ServiceAccounts the chart renders under a literal name."""
    names: set[str] = set()
    for template in CHART.rglob("templates/*.yaml"):
        names.update(_ACCOUNT.findall(template.read_text(encoding="utf-8")))
    return names


def test_the_chart_renders_renamed_accounts() -> None:
    # The guard below means nothing if the scan finds no account: the API's must be among them.
    assert "tranibor-api" in renamed_accounts()


def test_the_notes_name_every_renamed_account_and_say_before() -> None:
    paragraph = _paragraph()
    missing = sorted(name for name in renamed_accounts() if f"`{name}`" not in paragraph)
    assert not missing, f"the R6 notes do not tell to bind {missing} before upgrading"
    assert "before the upgrade" in paragraph
    for former in ("`choregos-api`", "`choregos-migrations`"):
        assert former in paragraph
