"""The software-delivery stage roles are the plugin's, with their playbooks (ADR 0181, S83-12).

`fix_ci`, `address_review`, `release_notes` and `verify_prod` were members of the core's `StageRole` and of
the playbooks' `KNOWN_ROLES`, their prompts in the playbooks package. They are the plugin's now: its
`roles/` directory holds the four playbooks, and the extension declares them in the contracts' open list
`STAGE_ROLES` and the playbooks' search path. The strings do not change: stored workflows, the runs'
`stage_role` and recorded histories carry them.
"""

from __future__ import annotations

import pytest
import tranibor_playbooks
from tranibor_contracts import STAGE_ROLES, StageRole
from tranibor_core.dsl.parser import parse_workflow
from tranibor_playbooks import (
    INVARIANTS,
    KNOWN_ROLES,
    frame,
    playbook_instructions,
    playbook_origin,
    preview_playbook,
    render_playbook,
)
from tranibor_software_delivery import NAME
from tranibor_software_delivery.names import DeliveryRole
from tranibor_software_delivery.roles import ROLES_DIR, declare_roles

ROLES = sorted(role.value for role in DeliveryRole)


def test_the_four_roles_are_the_plugin_s_and_none_is_the_core_s() -> None:
    assert ROLES == ["address_review", "fix_ci", "release_notes", "verify_prod"]
    assert STAGE_ROLES.declared_by(NAME) == set(ROLES)
    assert not set(ROLES) & {role.value for role in StageRole}
    assert not set(ROLES) & set(KNOWN_ROLES)
    assert not set(ROLES) & set(tranibor_playbooks.roles_in(tranibor_playbooks.ROLES_DIR))
    assert declare_roles() == ROLES, "declaring again changes nothing"


@pytest.mark.parametrize("role", ROLES)
def test_each_role_renders_the_plugin_s_complete_prompt(role: str) -> None:
    rendered = render_playbook(
        role,
        ticket={"key": "acme#1", "title": "Title", "body": "Body"},
        spec="## Spec",
        allowed_paths=["src/**"],
    )
    assert "## Invariants" in rendered and "result.json" in rendered
    assert "{{" not in rendered, "no Jinja variable left unrendered"
    assert playbook_origin(role) == "plugin"
    assert (ROLES_DIR / role / "prompt.md").is_file()
    preview = preview_playbook(role)
    assert "<work item title>" in preview and "{%" not in preview
    variables = {"ticket": {"key": "acme#1", "title": "Title", "body": "Body"}, "allowed_paths": ["src/**"]}
    from tranibor_core.instructions import render_instructions

    registered = frame(render_instructions(playbook_instructions(role), **variables))
    assert registered.count(INVARIANTS) == 1


def test_a_workflow_naming_a_plugin_role_finds_its_playbook() -> None:
    _, report = parse_workflow(
        """
apiVersion: choregos/v1
kind: Workflow
metadata: { name: repair, version: 1 }
states:
  red: { kind: work, display: Red }
  done: { kind: terminal, display: Done }
actors:
  fixer: { type: agent, role: fix_ci }
transitions:
  - { id: t-fix, from: red, to: done, by: fixer }
""",
        strict=False,
    )
    assert not [issue for issue in report.warnings if issue.code == "role.playbook_not_found"]


def test_verify_prod_without_indicator_source_settles_for_the_measured_smoke_test() -> None:
    """S22-48: without a production indicator source, a measured smoke_ok is enough, and said."""
    rendered = render_playbook("verify_prod")
    assert "none configured for this project" in rendered
    assert "a measured `smoke_ok: true` is sufficient for a go" in rendered
    assert "Only the smoke test was available (smoke_ok)" in rendered
    assert "When in doubt, it is `no-go`" not in rendered


def test_verify_prod_with_an_indicator_source_keeps_the_strict_rule() -> None:
    rendered = render_playbook("verify_prod", production_indicators=["grafana_query"])
    assert "configured for this project: grafana_query." in rendered
    assert "SLO indicators before/after the promotion" in rendered
    assert "When in doubt, it is `no-go`" in rendered
    assert "Only the smoke test was available" not in rendered
