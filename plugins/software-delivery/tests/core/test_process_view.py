# SPDX-License-Identifier: Apache-2.0
"""The process view of the `software-delivery` plugin's workflows, gates and effects (ADR 0031, S39-13a;
moved from the core's suite, S83-05n).

The core's view says what an extension's gate and effect say of themselves when it registers them
(`packages/core/tests/test_process.py`, `test_process_view_names_the_agent.py`, on stand-ins): these are
what this plugin's say, its thresholds, its waits, the pull request it opens and merges, and the branch a
step writes when a diff gate reads it.
"""

from __future__ import annotations

from pathlib import Path

from tranibor_core import parse_workflow, to_process
from tranibor_core.dsl import template_yaml
from tranibor_core.dsl.process import gate_detail

ROOT = Path(__file__).resolve().parents[4]
DEV_COMPLEX = ROOT / "templates/github-software-delivery/workflows/dev-complex.yaml"

INLINE = """
apiVersion: choregos/v1
kind: Workflow
metadata: { name: inline, version: 1 }
initial: todo
actors:
  quant: { type: job, role: backtest, command: [python, run.py, "--fast"], image: quant-tools }
  maker: { type: agent, role: custom, playbook: maker }
  platform: { type: system }
states:
  todo: { display: To do, kind: wait }
  made: { display: Made }
  done: { display: Done, terminal: true, kind: terminal }
transitions:
  - { id: t-make, from: todo, to: made, by: maker, outputs: [deck], output_types: { deck: pptx } }
  - { id: t-run, from: made, to: done, by: quant, gates: [no_secrets] }
"""


def _steps(text: str) -> dict[str, dict]:
    workflow, _ = parse_workflow(text)
    return {s["id"]: s for s in to_process(workflow)}


def test_what_the_platform_does_is_said_in_plain_words() -> None:
    """`does` (#175): the process view says the platform opens, then merges the PR."""
    workflow, _ = parse_workflow(template_yaml("default-simple"))
    steps = {e["to"]: str(e["sentence"]) for e in to_process(workflow)}
    assert "the platform, which opens the pull request" in steps["pr_open"]
    assert "the platform, which merges the pull request" in steps["merged"]


def test_a_guarantee_says_its_thresholds_and_how_long_it_waits() -> None:
    """The map listed `diff_size_max` and `ci_green` as bare names: what the limit is, and that the
    step waits for the CI, were not said anywhere."""
    assert (
        gate_detail("diff_size_max", {})
        == "at most 60 files and 100000 lines changed (the platform's defaults)"
    )
    assert (
        gate_detail("diff_size_max", {"files": 10, "lines": 400}) == "at most 10 files and 400 lines changed"
    )
    assert "with none declared, anything passes" in str(gate_detail("scope_respected", {}))
    assert gate_detail("ci_green", {}, 72) == "waits for it up to 72 h"
    assert gate_detail("ci_green", {}) == "waits for it up to 24 h"
    assert gate_detail("no_secrets", {}) is None


def test_a_step_that_carries_a_diff_gate_writes_the_branch() -> None:
    steps = _steps(DEV_COMPLEX.read_text("utf-8"))
    assert steps["t-implement"]["writes_branch"] is True and steps["t-implement"]["outputs"] == []
    assert steps["t-review"]["writes_branch"] is False
    assert steps["t-approve-spec"]["writes_branch"] is False, "a person does not write the branch"


def test_a_job_that_carries_a_diff_gate_writes_the_branch() -> None:
    steps = _steps(INLINE)
    assert steps["t-run"]["command"] == ["python", "run.py", "--fast"]
    assert steps["t-run"]["image"] == "quant-tools" and steps["t-run"]["writes_branch"] is True
    assert steps["t-make"]["output_types"] == {"deck": "pptx"} and steps["t-make"]["agent"] is None
