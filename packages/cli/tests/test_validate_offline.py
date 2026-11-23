"""`workflow validate` offline cannot know which plugins the deployment installed (ADR 0181, round C2).

A gate a plugin serves (`ci_green`, the `software-delivery` plugin's) is reported as a warning offline
when the plugin's extension is not installed beside the CLI: the deployment refuses it at publication if
it lacks the extension, and `--remote` asks it. A gate nobody serves stays an error.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
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
  - {id: t-do, from: todo, to: done, by: dev, gates: [{name: GATE}]}
"""


@pytest.fixture
def registries() -> Iterator[None]:
    """The gates and context providers registered when the test starts are those after it: a test forgets
    or registers some."""
    from tranibor_core import gates

    tables = (
        "_REGISTRY",
        "_NEEDS",
        "_READS",
        "_PLUGIN_OF",
        "_SUMMARY",
        "_DETAIL",
        "_REQUIRES",
        "_PROVIDERS",
        "_PROVIDER_PLUGIN",
        "_JUDGED",
    )
    sets = ("_ASYNC_GATES", "_WORKSPACE", "_RUN_SCOPED")
    saved = {name: dict(getattr(gates, name)) for name in tables}
    saved_sets = {name: set(getattr(gates, name)) for name in sets}
    yield
    for name, content in saved.items():
        getattr(gates, name).clear()
        getattr(gates, name).update(content)
    for name, members in saved_sets.items():
        getattr(gates, name).clear()
        getattr(gates, name).update(members)


@pytest.fixture
def without_the_plugin(registries: None) -> None:
    """The CLI without the `software-delivery` extension beside it, whether this process installed it or
    not."""
    from tranibor_core.gates import forget

    forget("software-delivery")


@pytest.fixture
def with_the_plugin_s_gate(without_the_plugin: None) -> None:
    """`ci_green` served beside the CLI, as the `software-delivery` extension serves it (here a stand-in of
    its registration; the extension itself: `plugins/software-delivery/tests/test_validate_offline.py`)."""
    from tranibor_core.gates import GateContext, GateOutcome, gate

    @gate("ci_green", asynchronous=True, needs=("scm",), plugin="software-delivery")
    def _ci_green(ctx: GateContext, params: dict[str, Any]) -> GateOutcome:
        return GateOutcome("ci_green", True)


def _validate(tmp_path: Path, gate: str) -> tuple[int, str]:
    from tranibor_cli.main import app

    path = tmp_path / "workflow.yaml"
    path.write_text(WORKFLOW.replace("GATE", gate), encoding="utf-8")
    result = CliRunner().invoke(app, ["workflow", "validate", str(path)])
    return result.exit_code, result.output


def test_a_plugin_s_gate_is_a_warning_offline(tmp_path: Path, without_the_plugin: None) -> None:
    code, output = _validate(tmp_path, "ci_green")
    assert code == 0, output
    assert "software-delivery" in output and "checked offline" in output and "is valid" in output


def test_a_gate_nobody_serves_is_still_refused(tmp_path: Path, without_the_plugin: None) -> None:
    code, output = _validate(tmp_path, "licorne")
    assert code == 1, output
    assert "unknown gate" in output


def test_with_the_plugin_installed_nothing_is_said(tmp_path: Path, with_the_plugin_s_gate: None) -> None:
    code, output = _validate(tmp_path, "ci_green")
    assert code == 0 and "checked offline" not in output, output
