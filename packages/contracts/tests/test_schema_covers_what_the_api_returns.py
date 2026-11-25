"""Two properties the API returns that the JSON Schemas it cites did not declare.

Found by fuzzing the API against `openapi.yaml` (docs/security-testing.md): `POST /orgs/{org}/projects`
returned a configuration with `dod`, and `GET /projects/{id}/policy` one with
`express_lane.approval.timeout_hours`. Both schemas close their objects (`additionalProperties: false`),
so the responses did not validate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
from tranibor_contracts import ProjectConfig, load_schema
from tranibor_contracts.delivery import ExpressLane, TrainApproval


def test_a_project_configuration_with_dod_facts_validates() -> None:
    config = ProjectConfig.model_validate({"slug": "api", "org": "acme", "dod": {"facts": {"ok": "true"}}})
    dump = config.model_dump(mode="json", exclude_none=True)
    assert dump["dod"] == {"facts": {"ok": "true"}}
    jsonschema.validate(dump["dod"], load_schema("project.schema.json")["properties"]["dod"])


def test_an_express_lane_approval_with_its_timeout_validates() -> None:
    lane = ExpressLane(approval=TrainApproval(group="captains", required=True, timeout_hours=6))
    dump = lane.model_dump(mode="json", exclude_none=True)
    assert dump["approval"]["timeout_hours"] == 6
    schema = load_schema("policy.schema.json")
    express = schema["$defs"]["trainEnv"]["properties"]["express_lane"]
    jsonschema.validate(dump, express)


def _openapi() -> dict[str, Any]:
    import yaml

    path = Path(__file__).resolve().parents[1] / "openapi.yaml"
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def test_a_problem_error_may_locate_by_index_and_carry_no_column() -> None:
    """`loc` of a validation error holds indexes (`["body", 0]`) and a policy error's `column` can be null."""
    item = _openapi()["components"]["schemas"]["Problem"]["properties"]["errors"]["items"]
    jsonschema.validate({"loc": ["body", 0, "name"], "msg": "required", "line": 1, "column": None}, item)


def test_a_workflow_validation_of_a_document_that_does_not_parse_has_no_graph_and_no_document() -> None:
    properties = _openapi()["components"]["schemas"]["WorkflowValidation"]["properties"]
    for name in ("graph", "document"):
        assert {"type": "null"} in properties[name]["oneOf"], name
