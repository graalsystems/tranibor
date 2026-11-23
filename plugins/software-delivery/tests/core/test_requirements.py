# SPDX-License-Identifier: Apache-2.0
"""What the `software-delivery` plugin's gates, step effects and train require of a project's connectors,
and so its delivery workflows (ADR 0034, S19-01; moved from the core's suite, S83-05n).

The core deduces a project's requirements from what its workflows' gates, effects and carriers declare when
their extension registers them (`packages/core/tests/test_requirements.py`, on a stand-in plugin). These
are this plugin's declarations: a diff and a PR's checks read through the repository, a pull request opened
and merged through it, the train to production through a `cd`.
"""

from __future__ import annotations

import pytest
from tranibor_core import parse_workflow
from tranibor_core.dsl import NOT_DEPLOYED, TEMPLATE_NAMES, template_yaml
from tranibor_core.dsl.edition import edit
from tranibor_core.dsl.requirements import requirements
from tranibor_core.gates import gate_needs, known_gates

ARRIVAL = """apiVersion: choregos/v1
kind: Workflow
metadata: { name: arrival, version: 1 }
actors:
  coordinator: { type: agent, role: plan, model: "profile:standard" }
  hr: { type: human, group: hr, sla_hours: 24 }
states:
  request: { display: Request, kind: wait }
  plan: { display: Access plan }
  validated: { display: Validated, terminal: true }
transitions:
  - { id: t-plan, from: request, to: plan, by: coordinator, outputs: [plan], gates: [outputs_present] }
  - { id: t-validate, from: plan, to: validated, by: hr }
"""

#: The core workflows that deliver code to production; `default-request` (ADR 0110), the `blank`
#: skeleton (ADR 0112) and the catalogue's audits, studies and documentation (S59-02) do not.
SOFTWARE_TEMPLATES = [name for name in TEMPLATE_NAMES if name not in NOT_DEPLOYED]


def _capabilities(*texts: str) -> list[str]:
    return [e.capability for e in requirements(parse_workflow(t)[0] for t in texts)]


@pytest.mark.parametrize("name", SOFTWARE_TEMPLATES)
def test_a_software_template_requires_a_repository_and_a_train_not_a_ci(name: str) -> None:
    """The PR's checks are read through the repository: GitHub Actions is enough, no `ci` connector."""
    found = {e.capability: e for e in requirements([parse_workflow(template_yaml(name))[0]])}
    assert {"scm", "cd"} <= set(found)
    reasons = found["ci"].reasons if "ci" in found else ()
    assert all("provenance_signed" in reason for reason in reasons), reasons


def test_each_requirement_says_why_and_where_from() -> None:
    (scm,) = [
        e for e in requirements([parse_workflow(template_yaml("default-simple"))[0]]) if e.capability == "scm"
    ]
    assert any("default-simple: the guarantee scope_respected" in reason for reason in scm.reasons)
    assert len(set(scm.reasons)) == len(scm.reasons), "without duplicates"


def test_a_guarantee_that_reads_a_prs_checks_requires_a_repository_even_in_an_hr_process() -> None:
    with_ci = ARRIVAL.replace("gates: [outputs_present]", "gates: [outputs_present, ci_green]")
    assert "scm" in _capabilities(with_ci) and "ci" not in _capabilities(with_ci)


def test_each_of_the_plugin_s_guarantees_knows_what_it_reads() -> None:
    reads = {g: gate_needs(g) for g in known_gates()}
    assert reads["scope_respected"] == {"scm"}
    assert reads["ci_green"] == reads["scans_ok"] == {"scm"}, "the PR's checks, read through the repository"


def test_what_the_platform_does_is_required_by_the_effect_not_by_the_name() -> None:
    """#175: a renamed state keeps what it requires, because the effect is written (`does`, `production`)."""
    text = edit(
        template_yaml("default-simple"),
        [
            {"op": "rename_state", "from": "pr_open", "to": "review"},
            {"op": "rename_state", "from": "deployed_prod", "to": "in_service"},
        ],
    ).yaml
    workflow, _ = parse_workflow(text, strict=False)
    reasons = {e.capability: " ".join(e.reasons) for e in requirements([workflow])}
    assert "opens the pull request" in reasons["scm"]
    assert "in_service is production" in reasons["cd"]
