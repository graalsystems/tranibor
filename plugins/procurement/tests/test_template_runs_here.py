# SPDX-License-Identifier: Apache-2.0
"""The published templates run on this commit's half of the plugin (S85-10, ADR 0202).

The templates are the plugin's content (the platform's plugin repository, read here from the archive
the core ships offline); their job steps run a subcommand this distribution declares, in the job image
the release builds, and the schemas their gates carry are the ones this distribution ships, byte for
byte after canonical JSON: the agent that writes `offers_structured` is judged by the very schema the
job that reads it was written against.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml
from tranibor_core.agent_completeness import missing_components
from tranibor_core.dsl import parse_workflow

from .content import (
    CONTRACT_RENEWALS,
    OFFER_COMPARISON,
    OPERATIONS,
    OPERATIONS_ROOT,
    ROOT,
    SUPPLIER_ONBOARDING,
)

PLUGIN = Path(__file__).resolve().parents[1]
SCHEMAS = PLUGIN / "src" / "tranibor_procurement" / "schemas"
WORKFLOWS = {
    "offer-comparison": OFFER_COMPARISON,
    "supplier-onboarding": SUPPLIER_ONBOARDING,
    "contract-renewals": CONTRACT_RENEWALS,
}


def _workflow(path: Path) -> Any:
    workflow, report = parse_workflow(path.read_text(encoding="utf-8"))
    assert workflow is not None, report
    return workflow


def _jobs(path: Path) -> list[Any]:
    return [a for a in _workflow(path).actors.values() if getattr(a, "type", None) == "job"]


def _subcommands() -> set[str]:
    """The subcommands `tranibor-procurement` declares, from its own parser."""
    import argparse

    from tranibor_procurement.cli import _parser

    (action,) = [a for a in _parser()._actions if isinstance(a, argparse._SubParsersAction)]
    return set(action.choices)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_the_distribution_declares_the_command_the_jobs_run() -> None:
    scripts = tomllib.loads((PLUGIN / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert "tranibor-procurement" in scripts
    assert _subcommands() == {"extract", "compare", "tier", "supplier-check", "renewals"}


@pytest.mark.parametrize("name", sorted(WORKFLOWS))
def test_every_job_step_runs_a_subcommand_this_distribution_declares_in_the_plugins_image(name: str) -> None:
    jobs = _jobs(WORKFLOWS[name])
    assert jobs, name
    assert {job.image for job in jobs} == {"procurement"}
    assert {job.command[0] for job in jobs} == {"tranibor-procurement"}
    assert {job.command[1] for job in jobs} <= _subcommands()


def test_offer_comparison_runs_extract_compare_and_tier() -> None:
    jobs = _jobs(OFFER_COMPARISON)
    assert [job.command[1] for job in jobs] == ["extract", "compare", "tier"]
    (tier,) = [job for job in jobs if job.command[1] == "tier"]
    assert "--committee-from" in tier.command, "the threshold is in the workflow, never a field"


def test_the_onboarding_and_the_renewals_call_the_purchasing_connector_the_template_requires() -> None:
    manifest = yaml.safe_load((OPERATIONS / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["requires"]["org_connectors"] == {"purchasing": "mcp"}
    for path, command in ((SUPPLIER_ONBOARDING, "supplier-check"), (CONTRACT_RENEWALS, "renewals")):
        (job,) = [j for j in _jobs(path) if j.command[1] == command]
        assert next(t.connector for t in job.tools) == "purchasing"


@pytest.mark.parametrize(
    ("workflow", "transition_id", "output", "schema"),
    [
        (OFFER_COMPARISON, "t-read", "offers_structured", "offers_structured"),
        (SUPPLIER_ONBOARDING, "t-file", "supplier_file", "supplier_file"),
    ],
)
def test_the_schema_a_gate_carries_is_the_one_this_distribution_ships(
    workflow: Path, transition_id: str, output: str, schema: str
) -> None:
    (transition,) = [t for t in _workflow(workflow).transitions if t.id == transition_id]
    (gate,) = [g for g in transition.gates if g.name == "output_schema"]
    carried = dict(gate.params or {})["schemas"][output]
    shipped = json.loads((SCHEMAS / f"{schema}.schema.json").read_text(encoding="utf-8"))
    assert _canonical(carried) == _canonical(shipped)


def test_a_drifted_schema_is_caught() -> None:
    """The comparison above must fail when one side moves: a property renamed in the carried copy."""
    (transition,) = [t for t in _workflow(OFFER_COMPARISON).transitions if t.id == "t-read"]
    (gate,) = [g for g in transition.gates if g.name == "output_schema"]
    carried = json.loads(_canonical(dict(gate.params or {})["schemas"]["offers_structured"]))
    carried["properties"]["suppliers"]["items"]["required"].remove("stated_total")
    shipped = json.loads((SCHEMAS / "offers_structured.schema.json").read_text(encoding="utf-8"))
    assert _canonical(carried) != _canonical(shipped)


@pytest.mark.parametrize(
    "path",
    sorted([*(ROOT / "agents").glob("*.yaml"), *(OPERATIONS_ROOT / "agents").glob("*.yaml")]),
    ids=lambda p: p.stem,
)
def test_a_plugin_agent_declares_its_five_components(path: Path) -> None:
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert missing_components(document["agent"]["spec"]) == []
