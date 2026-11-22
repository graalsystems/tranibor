"""The `delivery` sections of the stage contracts (ADR 0181 C2): either form is read, both are written, and
they cannot disagree, since the section is derived from the former fields during the window."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
from tranibor_contracts import StageInput, StageResult, load_schema
from tranibor_contracts.delivery import PLUGIN, commands_of, repository_of, without_commands
from tranibor_contracts.stage import Artifacts
from tranibor_contracts.window import C2_DELIVERY, WINDOW

EXAMPLE = Path(__file__).resolve().parents[1] / "schemas/examples/stage-input.example.json"


def _stage_input() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return document


def test_a_stage_input_written_with_the_former_fields_carries_its_section() -> None:
    model = StageInput.model_validate(_stage_input())
    assert model.repo is not None and model.delivery is not None
    assert model.delivery.repo is not None and model.delivery.repo.url == model.repo.url
    assert commands_of(model).test == model.project.test_command
    dump = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dump["delivery"]["repo"]["work_branch"] == dump["repo"]["work_branch"]
    jsonschema.validate(dump["delivery"], load_schema("stage-input.schema.json")["properties"]["delivery"])


def test_a_stage_input_written_with_its_section_alone_reads_the_same() -> None:
    document = StageInput.model_validate(_stage_input()).model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    expected_repo = document.pop("repo")
    commands = {
        name: document["project"].pop(f"{name}_command", None) for name in ("test", "lint", "typecheck")
    }
    model = StageInput.model_validate(document)
    assert model.repo is not None and model.repo.model_dump() == expected_repo
    assert commands_of(model).model_dump() == commands
    assert repository_of(model) == model.delivery.repo  # type: ignore[union-attr]


def test_the_two_forms_cannot_disagree_after_a_copy() -> None:
    model = StageInput.model_validate(_stage_input())
    without = model.model_copy(update={"repo": None})
    assert repository_of(without) is None, "a copy that drops the repository drops its section"
    assert commands_of(without_commands(model)).model_dump() == {"test": "", "lint": "", "typecheck": ""}
    # A document that names both and disagrees: the former field wins for the window.
    document = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    document["repo"]["work_branch"] = "other"
    assert repository_of(StageInput.model_validate(document)).work_branch == "other"  # type: ignore[union-attr]


def test_a_stage_input_without_software_has_no_section() -> None:
    document = _stage_input()
    document.pop("repo", None)
    for former in ("test_command", "lint_command", "typecheck_command"):
        document["project"].pop(former, None)
    model = StageInput.model_validate(document)
    assert model.delivery is None and "delivery" not in model.model_dump(exclude_none=True)


def test_a_stage_result_s_branch_and_commits_under_both_forms() -> None:
    assert Artifacts(branch="b", commits=["c"]).model_dump()["delivery"] == {"branch": "b", "commits": ["c"]}
    read = Artifacts.model_validate({"delivery": {"branch": "b", "commits": ["c"]}})
    assert (read.branch, read.commits) == ("b", ["c"])
    assert Artifacts().delivery is None
    result = StageResult.model_validate(
        {"status": "done", "summary": "ok", "artifacts": {"delivery": {"branch": "w", "commits": ["x"]}}}
    )
    dump = result.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert dump["artifacts"]["branch"] == "w" and dump["artifacts"]["delivery"]["commits"] == ["x"]
    section = load_schema("stage-result.schema.json")["properties"]["artifacts"]["properties"]["delivery"]
    jsonschema.validate(dump["artifacts"]["delivery"], section)


def test_the_sections_are_the_plugin_s_and_their_former_fields_on_the_window_list() -> None:
    stage_input = load_schema("stage-input.schema.json")["properties"]["delivery"]
    artifacts = load_schema("stage-result.schema.json")["properties"]["artifacts"]["properties"]["delivery"]
    assert stage_input["x-plugin"] == artifacts["x-plugin"] == PLUGIN
    names = {entry.name for entry in WINDOW if entry.window is C2_DELIVERY}
    assert {"repo", "StageInput.repo", "artifacts.branch", "Artifacts.commits"} <= names
    assert {f"ProjectRef.{n}_command" for n in ("test", "lint", "typecheck")} <= names
