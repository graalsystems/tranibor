# SPDX-License-Identifier: Apache-2.0
"""With the `software-delivery` extension installed, every shipped workflow validates, the delivery ones
included (S83-05n).

The core's checks over the shipped templates (`packages/core/tests/test_dsl_validation.py`,
`test_unenforced_governance.py`) run with or without the plugins' extensions: without them, a delivery
workflow is refused for the plugin's gates, effects and carrier alone, which a deployment lacking the
extension refuses at publication. These pin the other half, so that the check never shrinks to the
workflows of the core: with the extension, each is valid and warns of nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tranibor_core import parse_workflow
from tranibor_core.dsl import TEMPLATE_NAMES, template_yaml
from tranibor_core.dsl.validator import deployment_issues

ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_every_core_template_is_valid_with_the_extension(name: str) -> None:
    workflow, report = parse_workflow(template_yaml(name), strict=False)
    assert report.valid, [i.format() for i in report.errors]
    assert report.warnings == [], [i.format() for i in report.warnings]
    assert deployment_issues(workflow, report) == ([], [])
    assert workflow.metadata.name == name


def test_every_template_s_workflow_is_valid_with_the_extension() -> None:
    files = sorted((ROOT / "templates").glob("*/workflows/*.yaml"))
    assert len(files) >= 4, files
    for path in files:
        _, report = parse_workflow(path.read_text(encoding="utf-8"), strict=False)
        assert report.errors == [], (path.name, [i.code for i in report.errors])


def test_markdown_sections_without_paths_is_refused() -> None:
    """The gate's own declaration (`requires=("paths", ...)`): without `paths:` it has nothing to check."""
    text = template_yaml("default-simple").replace(
        "    by: checker\n    gates: [evidence_present]",
        "    by: checker\n    gates: [evidence_present, { name: markdown_sections }]",
    )
    _, report = parse_workflow(text, strict=False)
    assert "gate.nothing_to_check" in {e.code for e in report.errors}
