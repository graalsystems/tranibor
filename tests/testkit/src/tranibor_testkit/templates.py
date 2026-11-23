# SPDX-License-Identifier: Apache-2.0
"""Template conformance, shared by the core's suite and the `software-delivery` plugin's (S83-05n).

What a shipped template promises must exist: its connectors are registered, its workflows validate with no
warning, its default, routing and policy resolve, its actions call only what its manifest announces, and
the agents and skills it ships install. `tests/conformance/templates/test_templates.py` holds every shipped
template to it. A software delivery template (one that requires the `software-delivery` plugin's `scm`,
`ci` or `cd` connector, ADR 0181 round D) cannot hold to it where that plugin is not installed: those
templates are held to the same checks by the plugin's suite
(`plugins/software-delivery/tests/core/test_template_conformance.py`), the others by the core's, and both
suites assert that the two halves are every shipped template.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[4]
TEMPLATES = ROOT / "templates"
MANIFESTS = sorted(TEMPLATES.glob("*/manifest.yaml"))
#: The connector kinds the `software-delivery` plugin registers (ADR 0181 round D).
SOFTWARE_DELIVERY_KINDS = frozenset({"scm", "ci", "cd"})


def manifest(path: Path) -> dict[str, Any]:
    return dict(yaml.safe_load(path.read_text(encoding="utf-8")))


def needs_software_delivery(path: Path) -> bool:
    """Whether a template requires a connector of the `software-delivery` plugin's kinds."""
    return bool(set(manifest(path)["requires"]["connectors"]) & SOFTWARE_DELIVERY_KINDS)


#: The shipped templates the core holds to conformance, and those the plugin's suite does.
CORE_MANIFESTS = [path for path in MANIFESTS if not needs_software_delivery(path)]
SOFTWARE_DELIVERY_MANIFESTS = [path for path in MANIFESTS if needs_software_delivery(path)]


def assert_the_declared_connectors_exist(path: Path) -> None:
    """A template only promises registered adapters."""
    from tranibor_adapters import available

    was_fake = os.environ.pop("TRANIBOR_FAKES", None)
    try:
        for kind, type_name in manifest(path)["requires"]["connectors"].items():
            assert type_name in available(kind), (
                f"{path.parent.name} requires `{kind}: {type_name}`, known: {', '.join(available(kind))}"
            )
    finally:
        if was_fake is not None:
            os.environ["TRANIBOR_FAKES"] = was_fake


def workflows(path: Path) -> list[Any]:
    """The workflows the template ships, read and validated as at a project's birth."""
    from tranibor_core.dsl import load_template, parse_workflow

    defaults = manifest(path)["defaults"]
    refs = list(defaults.get("workflows") or [defaults["workflow"]])
    delivered = []
    for ref in refs:
        if str(ref).startswith("template:"):
            delivered.append(load_template(str(ref).removeprefix("template:").split("@")[0]))
            continue
        resolved = (path.parent / str(ref)).resolve()
        assert resolved.is_relative_to(path.parent.resolve()), f"{ref} leaves the template folder"
        workflow, report = parse_workflow(resolved.read_text(encoding="utf-8"), strict=False)
        assert report.valid, f"{path.parent.name}/{ref} : {report.as_dict()['errors']}"
        assert not report.warnings, f"{path.parent.name}/{ref} : {report.as_dict()['warnings']}"
        delivered.append(workflow)
    return delivered


def assert_the_workflows_the_default_the_routing_and_the_policy_exist(path: Path) -> None:
    from tranibor_core import load_preset

    defaults = manifest(path)["defaults"]
    names = [w.metadata.name for w in workflows(path)]
    assert len(set(names)) == len(names), f"two workflows with the same name: {names}"
    assert defaults.get("default_workflow", names[0]) in names
    for rule in defaults.get("routing") or []:
        assert rule["workflow"] in names, (
            f"the routing targets `{rule['workflow']}`, which the template does not ship"
        )

    policy_ref = str(defaults["policy"])
    if policy_ref.startswith("preset:"):
        assert load_preset(policy_ref.removeprefix("preset:")) is not None
        return
    # A policy of the template's own (S21-22): in its folder, and readable.
    from tranibor_core.policy import parse_policy

    resolved = (path.parent / policy_ref).resolve()
    assert resolved.is_relative_to(path.parent.resolve()), f"{policy_ref} leaves the template folder"
    assert parse_policy(resolved.read_text(encoding="utf-8")).metadata.name


def assert_actions_call_only_what_the_template_announces(path: Path) -> None:
    """Every `connector.call` names a connector the manifest announces (`org_connectors`), an
    operation its capability declares, with the arguments its schema requires — and nothing else. An
    `mcp` server discovers its tools: only the connector name is judged. A template only calls
    core effects: a plugin's are not everywhere. `work_item.file` (ADR 0197) names a project, not a
    connector: the project's own `accepts_items_from` judges it."""
    from tranibor_adapters import connector_types

    announced = dict(manifest(path)["requires"].get("org_connectors") or {})
    operations = {
        kind: {o.name: o for o in spec.operations} for kind, _, spec in connector_types() if spec.operations
    }
    for workflow in workflows(path):
        for transition in workflow.transitions:
            action = transition.action
            if action is None:
                continue
            for effect in action.effects:
                specs = [
                    effect.model_dump(by_alias=True),
                    *([effect.compensate] if effect.compensate else []),
                ]
                for spec in specs:
                    name = str(spec["effect"])
                    assert name in {"connector.call", "check", "verifier", "work_item.file"}, (
                        f"{transition.key}: effect `{name}` outside the core"
                    )
                    if name != "connector.call":
                        continue
                    call = dict(spec.get("with") or {})
                    connector = call["connector"]
                    where = f"{workflow.metadata.name}/{transition.key} : {connector}"
                    assert connector in announced, f"{where} is not announced in `requires.org_connectors`"
                    capability = announced[connector]
                    if capability == "mcp":
                        continue
                    operation = operations.get(capability, {}).get(call["operation"])
                    assert operation is not None, (
                        f"{where} : `{call['operation']}` is not an operation of `{capability}`"
                    )
                    schema = operation.input_schema or {}
                    arguments = set(dict(call.get("arguments") or {}))
                    missing = set(schema.get("required", [])) - arguments
                    assert not missing, f"{where}/{call['operation']} : {sorted(missing)} missing"
                    extra = arguments - set(schema.get("properties", {}))
                    assert not extra, f"{where}/{call['operation']}: {sorted(extra)} outside the schema"


def assert_the_template_agents_and_skills_are_installable(path: Path) -> None:
    from tranibor_api.schemas.agents import AgentCreate
    from tranibor_api.services.agents import version_errors
    from tranibor_api.services.skills import validate
    from tranibor_api.services.templates import delivery

    delivered = delivery(manifest(path), path.parent)
    skills = {validate(files).name for files in delivered.skills}
    agents = set()
    for document in delivered.agents:
        agent = AgentCreate.model_validate(document)
        version_errors(agent.spec)
        agents.add(agent.slug)
        orphans = {s.slug for s in agent.spec.skills} - skills
        assert not orphans, f"{agent.slug} names skills the template does not ship: {sorted(orphans)}"
    for workflow in workflows(path):
        for name, actor in workflow.actors.items():
            reference = getattr(actor, "agent", None)
            if reference:
                slug = reference.split("@")[0]
                assert slug in agents, (
                    f"{workflow.metadata.name}/{name} names agent `{slug}`, which the template does not ship"
                )


__all__ = [
    "CORE_MANIFESTS",
    "MANIFESTS",
    "SOFTWARE_DELIVERY_KINDS",
    "SOFTWARE_DELIVERY_MANIFESTS",
    "TEMPLATES",
    "assert_actions_call_only_what_the_template_announces",
    "assert_the_declared_connectors_exist",
    "assert_the_template_agents_and_skills_are_installable",
    "assert_the_workflows_the_default_the_routing_and_the_policy_exist",
    "manifest",
    "needs_software_delivery",
    "workflows",
]
