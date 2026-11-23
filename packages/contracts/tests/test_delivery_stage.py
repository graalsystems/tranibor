"""The stage contracts write software delivery in their `delivery` sections and as named facts only
(ADR 0181 C2, the window closed on 2026-11-23); what still reads the former spellings is
`tranibor_contracts.recorded`, for the documents recorded before (Temporal histories, agents' results)."""

from __future__ import annotations

import base64
import contextlib
import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from tranibor_contracts import Artifacts, Evidence, StageInput, StageResult, load_schema
from tranibor_contracts.delivery import EVIDENCE_FACTS, PLUGIN, commands_of, repository_of, without_commands

EXAMPLES = Path(__file__).resolve().parents[1] / "schemas" / "examples"
HISTORIES = Path(__file__).resolve().parents[3] / "tests" / "replay" / "histories"
FORMER_INPUT = {"repo"}
FORMER_COMMANDS = {"test_command", "lint_command", "typecheck_command"}
FORMER_ARTIFACTS = {"branch", "commits", "pr_url"}


def _input(**changes: Any) -> dict[str, Any]:
    document = json.loads((EXAMPLES / "stage-input.example.json").read_text(encoding="utf-8"))
    document.update(changes)
    return document


def test_a_stage_input_holds_its_repository_and_commands_in_its_section() -> None:
    model = StageInput.model_validate(_input())
    repository = repository_of(model)
    assert repository is not None and repository.work_branch == "choregos/123-avoirs-total"
    assert commands_of(model).test == "make test"
    dump = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert not FORMER_INPUT & set(dump) and not FORMER_COMMANDS & set(dump["project"])
    assert "pr" not in dump["work_item"]["links"]
    schema = load_schema("stage-input.schema.json")["properties"]
    jsonschema.validate(dump["delivery"], schema["delivery"])
    jsonschema.validate(dump["project"], schema["project"])
    blank = without_commands(model)
    assert commands_of(blank).test == "" and repository_of(blank) == repository


def test_a_recorded_stage_input_is_read_into_its_section() -> None:
    """A stage input a Temporal history recorded before the window closed: `repo`, the project's
    commands and `links.pr` are folded into their successors, never written back."""
    document = _input()
    section = document.pop("delivery")
    document["repo"] = section["repo"]
    document["project"] = {**document["project"], "test_command": "make test", "lint_command": "make lint"}
    document["work_item"]["links"] = {"pr": "https://github.com/acme/billing-api/pull/9"}
    model = StageInput.model_validate(document)
    assert (
        repository_of(model) is not None
        and repository_of(model) == StageInput.model_validate(_input()).delivery.repo
    )  # type: ignore[union-attr]
    assert (commands_of(model).test, commands_of(model).lint) == ("make test", "make lint")
    assert model.work_item.links.change_request_url() == "https://github.com/acme/billing-api/pull/9"
    dump = model.model_dump(mode="json", by_alias=True)
    assert not FORMER_INPUT & set(dump) and not FORMER_COMMANDS & set(dump["project"])
    assert "pr" not in dump["work_item"]["links"]


def test_a_stage_result_holds_its_branch_and_commits_in_its_section_and_its_proofs_as_facts() -> None:
    document = json.loads((EXAMPLES / "stage-result.example.json").read_text(encoding="utf-8"))
    model = StageResult.model_validate(document)
    assert (
        model.artifacts.delivery is not None
        and model.artifacts.delivery.branch == "choregos/123-avoirs-total"
    )
    assert model.evidence.value("tests_run") == 412 and model.evidence.is_measured("tests_run")
    dump = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert not FORMER_ARTIFACTS & set(dump["artifacts"])
    assert not set(EVIDENCE_FACTS) & set(dump["evidence"]), "the proofs are named facts only"
    schema = load_schema("stage-result.schema.json")["properties"]
    jsonschema.validate(dump["artifacts"], schema["artifacts"])
    jsonschema.validate(dump["evidence"], schema["evidence"])


def test_a_recorded_stage_result_is_read_into_its_section_and_facts() -> None:
    """A result the runner wrote before (both forms, or the fields alone, `pr_url`): the field wins over a
    fact of the same name, as `with_facts` wrote it, and a measured field is a measured fact."""
    model = StageResult.model_validate(
        {
            "status": "done",
            "summary": "done",
            "artifacts": {"branch": "w/1", "commits": ["abc"], "pr_url": "https://x/pull/1"},
            "evidence": {
                "tests_passed": True,
                "tests_run": 3,
                "diff_files": 0,
                "facts": {"tests_run": 99, "smoke": True},
                "measured": ["tests_run", "diff_files", "diff_lines", "facts.smoke"],
            },
        }
    )
    assert model.artifacts.delivery is not None
    assert (model.artifacts.delivery.branch, model.artifacts.delivery.commits) == ("w/1", ["abc"])
    assert model.artifacts.link("change_request") == "https://x/pull/1"
    assert model.evidence.facts == {"tests_passed": True, "tests_run": 3, "diff_files": 0, "smoke": True}
    assert model.evidence.measured == [
        "facts.diff_files",
        "facts.diff_lines",
        "facts.smoke",
        "facts.tests_run",
    ]
    assert model.evidence.is_measured("diff_lines") and model.evidence.value("diff_lines") is None
    assert not model.evidence.is_measured("tests_passed")


def test_an_agent_s_evidence_written_as_fields_is_read_as_facts() -> None:
    """The agents of the published `software-delivery` content plugin (1.3.0) are told to write
    `evidence.tests_passed` and `evidence.tests_run`: their result still parses."""
    evidence = Evidence.model_validate({"tests_passed": True, "tests_run": 0})
    assert evidence.facts == {"tests_passed": True, "tests_run": 0} and evidence.measured is None


def test_a_section_and_a_link_win_over_a_former_field() -> None:
    artifacts = Artifacts.model_validate(
        {
            "branch": "former",
            "delivery": {"branch": "section", "commits": ["s"]},
            "pr_url": "https://x/pull/former",
            "links": [{"rel": "change_request", "url": "https://x/pull/link"}],
        }
    )
    assert artifacts.delivery is not None and artifacts.delivery.branch == "section"
    assert artifacts.delivery.commits == ["s"]
    assert artifacts.link("change_request") == "https://x/pull/link" and len(artifacts.links) == 1


def _payloads(node: Any) -> list[Any]:
    found: list[Any] = []
    if isinstance(node, dict):
        if isinstance(node.get("data"), str) and "metadata" in node:
            with contextlib.suppress(ValueError):
                found.append(json.loads(base64.b64decode(node["data"])))
        for value in node.values():
            found.extend(_payloads(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_payloads(value))
    return found


def _documents(node: Any, key: str) -> list[dict[str, Any]]:
    """Every object of a decoded payload that is a stage input (`run_id` and `transition`) or a stage
    result (`status` and `summary` beside `evidence`)."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        wanted = (
            {"run_id", "transition", "callbacks"} if key == "input" else {"status", "summary", "evidence"}
        )
        if wanted <= set(node):
            found.append(node)
        for value in node.values():
            found.extend(_documents(value, key))
    elif isinstance(node, list):
        for value in node:
            found.extend(_documents(value, key))
    return found


@pytest.mark.skipif(not HISTORIES.is_dir(), reason="the recorded histories are the repository's")
def test_every_recorded_history_s_stage_documents_still_parse_under_the_new_form() -> None:
    """The recorded histories carry stage inputs and results under the former spellings (ADR 0008):
    each parses, and is written back with none of them."""
    inputs = results = 0
    for history in sorted(HISTORIES.glob("*.json")):
        for payload in _payloads(json.loads(history.read_text(encoding="utf-8"))):
            for document in _documents(payload, "input"):
                dump = StageInput.model_validate(document).model_dump(mode="json", by_alias=True)
                assert not FORMER_INPUT & set(dump) and not FORMER_COMMANDS & set(dump["project"]), (
                    history.name
                )
                inputs += 1
            for document in _documents(payload, "result"):
                dump = StageResult.model_validate(document).model_dump(mode="json", by_alias=True)
                assert not FORMER_ARTIFACTS & set(dump["artifacts"]), history.name
                assert not set(EVIDENCE_FACTS) & set(dump["evidence"]), history.name
                results += 1
    assert inputs > 10 and results > 10, (inputs, results)


def test_the_schemas_hold_no_former_field_and_mark_the_sections_the_plugin_s() -> None:
    stage_input = load_schema("stage-input.schema.json")["properties"]
    stage_result = load_schema("stage-result.schema.json")["properties"]
    assert not FORMER_INPUT & set(stage_input)
    assert not FORMER_COMMANDS & set(stage_input["project"]["properties"])
    assert "pr" not in stage_input["work_item"]["properties"]["links"]["properties"]
    assert not FORMER_ARTIFACTS & set(stage_result["artifacts"]["properties"])
    assert not set(EVIDENCE_FACTS) & set(stage_result["evidence"]["properties"])
    assert stage_input["delivery"]["x-plugin"] == PLUGIN
    assert stage_result["artifacts"]["properties"]["delivery"]["x-plugin"] == PLUGIN


def test_the_recorded_reader_folds_every_evidence_fact_the_plugin_measures() -> None:
    from tranibor_contracts import recorded

    assert recorded.EVIDENCE_FACTS == EVIDENCE_FACTS
