# SPDX-License-Identifier: Apache-2.0
"""`workflow validate` offline, with the `software-delivery` extension installed beside the CLI: its gates
are known, and nothing is said of them (ADR 0181, round C2; moved from the CLI's suite, S83-05n, which
proves the warning without the extension)."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

WORKFLOW = """apiVersion: choregos/v1
kind: Workflow
metadata: {name: delivery, version: 1}
actors:
  dev: {type: agent, role: implement}
states:
  todo: {display: To do, kind: wait}
  done: {display: Done, terminal: true}
transitions:
  - {id: t-do, from: todo, to: done, by: dev, gates: [{name: ci_green}]}
"""


def test_with_the_plugin_installed_nothing_is_said(tmp_path: Path) -> None:
    from tranibor_cli.main import app

    path = tmp_path / "workflow.yaml"
    path.write_text(WORKFLOW, encoding="utf-8")
    result = CliRunner().invoke(app, ["workflow", "validate", str(path)])
    assert result.exit_code == 0 and "checked offline" not in result.output, result.output
