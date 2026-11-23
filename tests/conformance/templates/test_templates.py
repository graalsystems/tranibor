"""Template conformance: what a template promises must exist.

A template that declares a nonexistent connector or a step nobody implements fails at
provisioning, at a customer's, at the worst moment. This suite says so beforehand.

The checks of a template's connectors, workflows, actions and agents are the test kit's
(`tranibor_testkit.templates`): a software delivery template (it requires the `software-delivery` plugin's
`scm`, `ci` or `cd`) is held to them by the plugin's suite
(`plugins/software-delivery/tests/core/test_template_conformance.py`), where the plugin is installed; every
other template here, with or without the plugins (ADR 0181, round D).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tranibor_testkit.templates import (
    CORE_MANIFESTS,
    MANIFESTS,
    SOFTWARE_DELIVERY_MANIFESTS,
    TEMPLATES,
    assert_actions_call_only_what_the_template_announces,
    assert_the_declared_connectors_exist,
    assert_the_template_agents_and_skills_are_installable,
    assert_the_workflows_the_default_the_routing_and_the_policy_exist,
    manifest,
)

pytestmark = pytest.mark.conformance

# Steps known to `provisioning._execute`, plus those explicitly ignored.
KNOWN_STEPS = {
    "github.install_app",
    "github.ensure_labels",
    "github.ensure_project_board",
    "github.ensure_issue_template",
    "github.ensure_webhooks",
    "aca.check_environment",
    "aca.check_identity",
    "gitops.write_project_manifests",
    "gitops.open_pr_or_commit",
    "argocd.wait_synced",
    "repo.scaffold_pr",
    "memory.create_tenant",
    "memory.initial_import",
    "gateway.create_team_and_budget",
    "notify.test_message",
}


def test_il_y_a_des_templates() -> None:
    assert MANIFESTS, "no template shipped"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_the_manifest_is_well_formed(path: Path) -> None:
    doc = manifest(path)
    assert doc["apiVersion"] == "choregos/v1" and doc["kind"] == "Template"
    metadata = doc["metadata"]
    assert metadata["name"] == path.parent.name, "the template name is the folder name"
    assert metadata["version"] and metadata["display"] and metadata["description"].strip()


def test_the_shipped_templates_are_split_between_the_core_and_the_plugin_suites() -> None:
    """Every shipped template is held to the connector, workflow, action and agent checks by one suite: the
    core's here, the `software-delivery` plugin's for the templates that require its kinds."""
    assert Path(__file__).resolve().parents[3] / "templates" == TEMPLATES
    assert CORE_MANIFESTS and SOFTWARE_DELIVERY_MANIFESTS
    assert sorted(CORE_MANIFESTS + SOFTWARE_DELIVERY_MANIFESTS) == MANIFESTS
    assert (
        Path(__file__).resolve().parents[3]
        / "plugins/software-delivery/tests/core/test_template_conformance.py"
    ).is_file()


@pytest.mark.parametrize("path", CORE_MANIFESTS, ids=lambda p: p.parent.name)
def test_the_declared_connectors_exist(path: Path) -> None:
    assert_the_declared_connectors_exist(path)


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_the_steps_are_implemented(path: Path) -> None:
    for step in manifest(path).get("steps", []):
        name = step if isinstance(step, str) else next(iter(step))
        assert name in KNOWN_STEPS, f"unknown step in {path.parent.name}: {name}"


@pytest.mark.parametrize("path", CORE_MANIFESTS, ids=lambda p: p.parent.name)
def test_the_workflows_the_default_the_routing_and_the_policy_exist(path: Path) -> None:
    assert_the_workflows_the_default_the_routing_and_the_policy_exist(path)


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_the_scaffold_exists_and_holds_what_the_manifest_announces(path: Path) -> None:
    doc = manifest(path)
    annonces: list[str] = []
    for step in doc.get("steps", []):
        if isinstance(step, dict) and "repo.scaffold_pr" in step:
            annonces = list((step["repo.scaffold_pr"] or {}).get("files", []))
    if not annonces and "scaffold" not in doc:
        return  # a template without a repository scaffolds nothing
    scaffold = (path.parent / str(doc.get("scaffold", "./scaffold")).lstrip("./")).resolve()
    assert scaffold.is_dir(), f"dossier de scaffold absent : {scaffold}"
    for file in annonces:
        candidats = [scaffold / file, scaffold / f"{file}.j2"]
        assert any(c.exists() for c in candidats), f"{file} announced but missing from the scaffold"


@pytest.mark.parametrize("path", CORE_MANIFESTS, ids=lambda p: p.parent.name)
def test_actions_call_only_what_the_template_announces(path: Path) -> None:
    assert_actions_call_only_what_the_template_announces(path)


@pytest.mark.parametrize("path", CORE_MANIFESTS, ids=lambda p: p.parent.name)
def test_the_template_agents_and_skills_are_installable(path: Path) -> None:
    assert_the_template_agents_and_skills_are_installable(path)


#: The extensions a template may ship (S20-09): any other would have nobody to validate it before a
#: customer, nor, probably, to install it. None since the ontology was removed (ADR 0181, O4-1).
EXTENSIONS_KNOWN: set[str] = set()


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_template_extensions_are_known(path: Path) -> None:
    """A template ships no extension that nothing in this repository validates."""
    from tranibor_api.services.templates import delivery

    unknown = set(delivery(manifest(path), path.parent).extensions) - EXTENSIONS_KNOWN
    assert not unknown, f"extensions nobody validates: {sorted(unknown)}"


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.parent.name)
def test_required_inputs_are_named_and_described(path: Path) -> None:
    for entry in manifest(path).get("inputs", []):
        assert entry.get("name") and entry.get("type")
        if entry.get("required"):
            assert entry.get("description"), f"entrée obligatoire sans description : {entry['name']}"
