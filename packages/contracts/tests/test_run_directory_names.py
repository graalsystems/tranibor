"""R9a of ADR 0226: the run directory and the stage documents' `schema` take the name `tranibor`.

Each twin has its new-name case and its former-name case: the platform writes `.tranibor/` and
`tranibor/<Name>/v1`, and reads `.choregos/` and `choregos/<Name>/v1` until 0.32.0. A document stored
before the upgrade (a run's input or result, a Temporal history) keeps the id it was written with.
"""

from __future__ import annotations

import json

import pytest
import tranibor_contracts as contracts
from jsonschema import Draft202012Validator, RefResolver, ValidationError
from pydantic import ValidationError as ModelError
from tranibor_contracts import (
    STAGE_INPUT_SCHEMA,
    STAGE_RESULT_SCHEMA,
    ContextPack,
    Policy,
    StageResult,
)
from tranibor_contracts.demo_history import DemoHistory
from tranibor_contracts.policy import ScopePolicy
from tranibor_contracts.stage import CONTEXT_PACK_SCHEMA
from tranibor_contracts.wire import (
    FORMER_RUN_DIRECTORY,
    RUN_DIRECTORIES,
    RUN_DIRECTORY,
    run_path,
    run_paths,
    run_relative,
    schema_id,
    schema_ids,
)


def _validator(schema_name: str) -> Draft202012Validator:
    schema = contracts.load_schema(schema_name)
    store = {}
    for name in contracts.SCHEMA_FILES:
        sub = contracts.load_schema(name)
        store[sub["$id"]] = sub
        store[name] = sub
    return Draft202012Validator(
        schema, resolver=RefResolver(base_uri=schema["$id"], referrer=schema, store=store)
    )


# ─── the run directory ───────────────────────────────────────────────────────────────────────────────


def test_the_run_directory_is_written_under_the_new_name() -> None:
    assert (RUN_DIRECTORY, FORMER_RUN_DIRECTORY) == (".tranibor", ".choregos")
    assert RUN_DIRECTORIES == (".tranibor", ".choregos"), "the new one first"
    assert run_path("result.json") == ".tranibor/result.json"
    assert run_paths("outputs") == (".tranibor/outputs", ".choregos/outputs")


@pytest.mark.parametrize("directory", [".tranibor", ".choregos"], ids=["new", "former"])
def test_a_path_under_either_name_is_the_run_directory_s(directory: str) -> None:
    assert run_relative(f"{directory}/result.json") == "result.json"
    assert run_relative(f"./{directory}/outputs/deck.pptx") == "outputs/deck.pptx"
    assert run_relative(directory) == ""


def test_a_path_outside_both_is_not() -> None:
    for path in (
        "src/a.py",
        ".tranibor-x/result.json",
        ".choregosx/a",
        "docs/.tranibor/result.json",
        ".github/x",
    ):
        assert run_relative(path) is None, path


# ─── the contract's default deny_paths ───────────────────────────────────────────────────────────────


def test_the_default_deny_paths_hold_both_names() -> None:
    assert ScopePolicy().deny_paths == [".tranibor/**", ".choregos/**", ".github/workflows/**", "charts/**"]
    schema = contracts.load_schema("policy.schema.json")
    assert schema["properties"]["scope"]["properties"]["deny_paths"]["default"] == ScopePolicy().deny_paths
    policy = Policy.model_validate({"metadata": {"name": "p", "version": 1}})
    assert policy.scope.deny_paths[:2] == [".tranibor/**", ".choregos/**"]


# ─── the stage documents' schema ─────────────────────────────────────────────────────────────────────


def test_the_stage_documents_are_written_with_the_new_id() -> None:
    assert schema_id("StageResult") == STAGE_RESULT_SCHEMA == "tranibor/StageResult/v1"
    assert schema_ids("StageInput") == (STAGE_INPUT_SCHEMA, "choregos/StageInput/v1")
    assert CONTEXT_PACK_SCHEMA == "tranibor/ContextPack/v1"
    result = StageResult(status="done", summary="ok")  # type: ignore[call-arg]
    assert result.model_dump(by_alias=True)["schema"] == "tranibor/StageResult/v1"
    assert ContextPack(query="q").model_dump(by_alias=True)["schema"] == "tranibor/ContextPack/v1"


@pytest.mark.parametrize(
    "schema", ["tranibor/StageResult/v1", "choregos/StageResult/v1"], ids=["new", "former"]
)
def test_a_result_is_read_under_either_id_and_keeps_it(schema: str) -> None:
    """A result stored before the upgrade, re-read and re-serialised, is the same document (ADR 0008)."""
    stored = {"schema": schema, "status": "done", "summary": "ok"}
    result = StageResult.model_validate(stored)
    assert result.model_dump(mode="json", by_alias=True, exclude_none=True)["schema"] == schema
    _validator("stage-result.schema.json").validate(stored)


@pytest.mark.parametrize("name", ["StageInput", "ContextPack"])
@pytest.mark.parametrize("which", [0, 1], ids=["new", "former"])
def test_the_other_stage_documents_accept_either_id(name: str, which: int) -> None:
    schema = schema_ids(name)[which]
    file = {"StageInput": "stage-input.schema.json", "ContextPack": "context-pack.schema.json"}[name]
    assert schema in contracts.load_schema(file)["properties"]["schema"]["enum"]
    if name == "ContextPack":
        assert ContextPack.model_validate({"schema": schema, "query": "q"}).schema_ == schema


@pytest.mark.parametrize("which", [0, 1], ids=["new", "former"])
def test_a_demo_history_is_accepted_under_either_id(which: int) -> None:
    schema = schema_ids("DemoHistory")[which]
    document = json.loads((contracts.schemas_dir() / "examples" / "demo-history.example.json").read_text())
    document["schema"] = schema
    assert DemoHistory.model_validate(document).schema_ == schema
    _validator("demo-history.schema.json").validate(document)


@pytest.mark.parametrize(
    "schema", ["Choregos/StageResult/v1", "tranibor/StageResult/v2", "tranibor/StageInput/v1"]
)
def test_any_other_id_is_refused(schema: str) -> None:
    with pytest.raises(ModelError):
        StageResult.model_validate({"schema": schema, "status": "done", "summary": "ok"})
    with pytest.raises(ValidationError):
        _validator("stage-result.schema.json").validate({"schema": schema, "status": "done", "summary": "ok"})
