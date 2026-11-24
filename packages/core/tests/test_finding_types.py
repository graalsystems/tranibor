"""A workflow declares its own finding types (ADR 0217): the validator and the judgement."""

from __future__ import annotations

import pytest
from tranibor_contracts import Finding, Workflow
from tranibor_core import parse_workflow
from tranibor_core.finding_types import allowed, refusal, title_of

TEMPLATE = """
apiVersion: choregos/v1
kind: Workflow
metadata:
  name: warranty-claims
  version: 1
  finding_types:
{types}
initial: received
actors:
  assessor: {{ type: agent, role: claim_assessment }}
states:
  received: {{ display: Received }}
  assessed: {{ display: Assessed, terminal: true }}
transitions:
  - {{ id: t-assess, from: received, to: assessed, by: assessor }}
"""

DUPLICATE = "    - { name: duplicate-claim, title: Suspected duplicate claim, severities: [medium, high] }"


def _workflow(*types: str) -> Workflow:
    return parse_workflow(TEMPLATE.format(types="\n".join(types)), strict=False)[0]


def _codes(*types: str) -> set[str]:
    _, report = parse_workflow(TEMPLATE.format(types="\n".join(types)), strict=False)
    return {issue.code for issue in report.errors}


def test_a_declared_type_validates_and_a_workflow_without_any_is_unchanged() -> None:
    assert _codes(DUPLICATE) == set()
    plain = parse_workflow(TEMPLATE.format(types="").replace("  finding_types:\n", ""), strict=False)[0]
    assert plain.metadata.finding_types is None


def test_a_type_declared_twice_or_taken_by_the_core_is_refused_at_publication() -> None:
    assert "finding_types.duplicate" in _codes(DUPLICATE, DUPLICATE)
    assert "finding_types.taken" in _codes("    - { name: security, title: Security }")


@pytest.mark.parametrize("name", ["Duplicate Claim", "duplicate_claim", "9claim", "x" * 33])
def test_a_name_is_a_finding_types_name(name: str) -> None:
    with pytest.raises(Exception, match=r"finding_types|pattern|String should match"):
        parse_workflow(TEMPLATE.format(types=f"    - {{ name: {name!r}, title: T }}"), strict=False)


def test_the_workflows_types_come_after_the_registrys_and_carry_their_title() -> None:
    workflow = _workflow(DUPLICATE)
    types = allowed(workflow)
    assert {"security", "docs", "ux", "duplicate-claim"} <= set(types)
    assert types["duplicate-claim"].title == "Suspected duplicate claim"
    assert types["security"].title is None
    assert title_of(workflow, "duplicate-claim") == "Suspected duplicate claim"
    assert title_of(workflow, "security") is None and title_of(None, "duplicate-claim") is None


def test_an_undeclared_type_is_refused_naming_the_workflow_and_what_is_allowed() -> None:
    workflow = _workflow(DUPLICATE)
    assert refusal(workflow, "duplicate-claim", "high") is None
    why = refusal(workflow, "fraud-ring")
    assert why is not None and "`warranty-claims`" in why and "duplicate-claim" in why and "security" in why
    assert refusal(None, "duplicate-claim") is not None
    severity = refusal(workflow, "duplicate-claim", "low")
    assert severity is not None and "medium, high" in severity


def test_the_contract_reads_any_name_and_the_cores_types_come_back_as_their_member() -> None:
    from tranibor_contracts import FindingType

    custom = Finding(title="a finding", type="duplicate-claim", severity="high", evidence="x")
    assert custom.type == "duplicate-claim"
    core = Finding(title="a finding", type="security", severity="high", evidence="x")
    assert core.type is FindingType.SECURITY
