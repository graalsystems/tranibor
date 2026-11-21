"""The `delivery` sections of the project and the policy (ADR 0181 C2): either form is read, both are
written, the former field wins when both are written."""

from __future__ import annotations

from typing import Any

import jsonschema
from tranibor_contracts import Policy, ProjectConfig, load_schema
from tranibor_contracts.delivery import PLUGIN
from tranibor_contracts.window import C2_DELIVERY, WINDOW

REPO = {"url": "https://github.com/acme/api", "default_branch": "main"}
POLICY: dict[str, Any] = {"metadata": {"name": "default", "version": 1}}


def test_a_project_written_with_its_section_alone_reads_the_same() -> None:
    config = ProjectConfig.model_validate(
        {
            "slug": "api",
            "org": "acme",
            "delivery": {
                "repo": REPO,
                "repositories": [{**REPO, "name": "docs", "url": "https://github.com/acme/docs"}],
            },
        }
    )
    assert config.repo is not None and config.repo.url == REPO["url"]
    assert [r.name for r in config.repositories] == ["docs"]
    dump = config.model_dump(mode="json", exclude_none=True)
    assert "delivery" not in dump, "written under the former fields until the writers switch"
    assert ProjectConfig.model_validate(dump) == config
    assert config.delivery is not None and config.delivery.repo == dump["repo"]
    section = config.delivery.model_dump(mode="json", exclude_none=True)
    jsonschema.validate(section, load_schema("project.schema.json")["properties"]["delivery"])


def test_a_client_that_drops_a_former_field_drops_it() -> None:
    """A document read back and edited: the former field it removed is not taken from a section it
    did not send."""
    config = ProjectConfig.model_validate({"slug": "api", "org": "acme", "repo": REPO})
    edited = {k: v for k, v in config.model_dump(mode="json").items() if k != "repo"}
    assert ProjectConfig.model_validate(edited).repo is None


def test_a_project_without_software_has_no_section_and_the_former_field_wins() -> None:
    assert ProjectConfig(slug="desk", org="acme").delivery is None
    both = ProjectConfig.model_validate(
        {"slug": "api", "org": "acme", "repo": REPO, "delivery": {"repo": {**REPO, "url": "https://x/y"}}}
    )
    assert both.repo is not None and both.repo.url == REPO["url"]
    assert both.delivery is not None and both.delivery.repo == both.repo.model_dump(
        mode="json", exclude_none=True
    )


def test_a_policy_written_with_its_section_alone_reads_the_same() -> None:
    policy = Policy.model_validate(
        {
            **POLICY,
            "delivery": {
                "scope": {"max_diff_files": 7, "max_diff_lines": 500},
                "approvals": {"merge": {"required": "always", "group": "leads"}},
                "release_train": {"prod": {"mode": "auto_sync"}},
            },
        }
    )
    assert (policy.scope.max_diff_files, policy.scope.max_diff_lines) == (7, 500)
    assert policy.approvals.merge is not None and policy.approvals.merge.group == "leads"
    assert policy.release_train["prod"].mode == "auto_sync"
    section = policy.delivery.model_dump(mode="json")
    assert section["scope"] == {"max_diff_files": 7, "max_diff_lines": 500}
    schema = load_schema("policy.schema.json")
    jsonschema.validate(section, schema["properties"]["delivery"] | {"$defs": schema["$defs"]})
    assert "delivery" not in policy.model_dump(), "written under the former fields until the writers switch"
    written = Policy.model_validate(
        {**POLICY, "scope": {"max_diff_files": 9}, "delivery": {"scope": {"max_diff_files": 7}}}
    )
    assert written.scope.max_diff_files == 9, "the former field wins when both are written"


def test_the_policy_engine_reads_the_bounds_through_the_section() -> None:
    from tranibor_core.policy import PolicyEngine

    engine = PolicyEngine(Policy.model_validate({**POLICY, "delivery": {"scope": {"max_diff_files": 4}}}))
    assert engine.max_diff() == (4, 3000)


def test_the_sections_are_the_plugin_s_and_their_former_fields_on_the_window_list() -> None:
    for schema in ("project.schema.json", "policy.schema.json"):
        assert load_schema(schema)["properties"]["delivery"]["x-plugin"] == PLUGIN
    names = {entry.name for entry in WINDOW if entry.window is C2_DELIVERY}
    assert {"project.repo", "ProjectConfig.repositories", "project.gitops", "policy.release_train"} <= names
    assert {"policy.scope.max_diff_files", "policy.approvals.merge"} <= names
