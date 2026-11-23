"""The `delivery` sections of the project and the policy (ADR 0181 C2, the window closed on 2026-11-23):
the section is the one place the repositories, the GitOps, the diff bounds, the merge approval and the
release trains live, and a document that writes a former field is refused, naming the section."""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from pydantic import ValidationError
from tranibor_contracts import Policy, ProjectConfig, load_schema
from tranibor_contracts.delivery import PLUGIN, ProjectDelivery
from tranibor_contracts.window import WINDOW

REPO = {"url": "https://github.com/acme/api", "default_branch": "main"}
POLICY: dict[str, Any] = {"metadata": {"name": "default", "version": 1}}


def test_a_project_holds_its_repositories_in_its_section() -> None:
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
    repos = config.delivery_section
    assert repos.repo is not None and repos.repo.url == REPO["url"]
    assert repos.repository_names() == ["api", "docs"]
    dump = config.model_dump(mode="json", exclude_none=True)
    assert not {"repo", "repositories", "gitops"} & set(dump), "the section is the one place they are written"
    assert dump["delivery"]["repo"]["url"] == REPO["url"]
    assert ProjectConfig.model_validate(dump) == config
    jsonschema.validate(dump["delivery"], load_schema("project.schema.json")["properties"]["delivery"])


@pytest.mark.parametrize("former", ["repo", "repositories", "gitops"])
def test_a_project_written_with_a_former_field_is_refused_naming_its_section(former: str) -> None:
    value: Any = {"repo": REPO, "repositories": [], "gitops": {"apps": ["api"]}}[former]
    with pytest.raises(ValidationError, match=rf"`{former}` moved to the `delivery` section"):
        ProjectConfig.model_validate({"slug": "api", "org": "acme", former: value})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"slug": "api", "org": "acme", former: value}, load_schema("project.schema.json"))


def test_a_project_without_software_has_no_section() -> None:
    config = ProjectConfig.model_validate({"slug": "letters", "org": "acme"})
    assert config.delivery is None
    assert not config.delivery_section.has_repo and config.delivery_section.repository_names() == []
    assert "delivery" not in config.model_dump(mode="json", exclude_none=True)


def test_the_section_keeps_the_rules_of_several_repositories() -> None:
    """ADR 0124 §1, in the section: `repositories` without `repo`, one name twice, one URL twice."""
    other = {"name": "docs", "url": "https://github.com/acme/docs"}
    with pytest.raises(ValidationError, match=r"without `delivery\.repo`"):
        ProjectDelivery.model_validate({"repositories": [other]})
    with pytest.raises(ValidationError, match="two repositories named `api`"):
        ProjectDelivery.model_validate({"repo": REPO, "repositories": [{**other, "name": "api"}]})
    with pytest.raises(ValidationError, match="one repository under two names"):
        ProjectDelivery.model_validate(
            {"repo": REPO, "repositories": [{**other, "url": REPO["url"] + ".git"}]}
        )
    schema = load_schema("project.schema.json")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"slug": "api", "org": "acme", "delivery": {"repositories": [other]}}, schema)


def test_a_policy_holds_its_bounds_merge_and_trains_in_its_section() -> None:
    policy = Policy.model_validate(
        {
            **POLICY,
            "delivery": {
                "scope": {"max_diff_files": 7},
                "approvals": {"merge": {"required": "never"}},
                "release_train": {"prod": {"mode": "train"}},
            },
        }
    )
    assert (policy.delivery.scope.max_diff_files, policy.delivery.scope.max_diff_lines) == (7, 3000)
    assert policy.delivery.approvals.merge is not None
    assert policy.delivery.release_train["prod"].mode == "train"
    dump = policy.model_dump(mode="json", exclude_none=True)
    assert "release_train" not in dump and "merge" not in dump["approvals"]
    assert not {"max_diff_files", "max_diff_lines"} & set(dump["scope"])
    assert Policy.model_validate(dump) == policy
    jsonschema.validate(dump, load_schema("policy.schema.json"))


def test_a_policy_without_a_section_gets_the_default_bounds() -> None:
    from tranibor_core.policy import PolicyEngine

    policy = Policy.model_validate(POLICY)
    assert PolicyEngine(policy).max_diff() == (60, 3000)
    assert PolicyEngine(policy).train_envs() == []


@pytest.mark.parametrize(
    ("former", "path"),
    [
        ({"scope": {"max_diff_files": 7}}, "scope.max_diff_files"),
        ({"scope": {"max_diff_lines": 7}}, "scope.max_diff_lines"),
        ({"approvals": {"merge": {"required": "never"}}}, "approvals.merge"),
        ({"release_train": {"prod": {"mode": "train"}}}, "release_train"),
    ],
)
def test_a_policy_written_with_a_former_field_is_refused_naming_its_section(
    former: dict[str, Any], path: str
) -> None:
    with pytest.raises(ValidationError, match=rf"`{path}` moved to the `delivery` section"):
        Policy.model_validate({**POLICY, **former})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**POLICY, **former}, load_schema("policy.schema.json"))


def test_the_sections_are_the_plugin_s_and_no_former_field_is_on_the_window_list() -> None:
    for schema, name in (("project.schema.json", "project"), ("policy.schema.json", "policy")):
        assert load_schema(schema)["properties"]["delivery"]["x-plugin"] == PLUGIN, name
    assert load_schema("policy.schema.json")["$defs"]["trainEnv"]["x-plugin"] == PLUGIN
    assert not [
        entry for entry in WINDOW if entry.successor.startswith(("project.delivery", "policy.delivery"))
    ]


#: A template's policy written before the window closed: a content plugin's archive may ship one.
TEMPLATE_POLICY = """apiVersion: choregos/v1
kind: Policy
metadata: { name: delivery, version: 1 }
scope:
  deny_paths: [".choregos/**"]
  max_diff_files: 30   # the reviewers' reading
  max_diff_lines: 900
# Trains per environment.
release_train:
  prod: { mode: train }
"""


def test_a_template_s_policy_written_the_former_way_is_read_into_its_section() -> None:
    """The persisted-document reader (`recorded`): what a request would be refused for, a template's
    policy is read with, and its text is stored moved, comments kept."""
    from tranibor_contracts.recorded import fold_policy_text
    from tranibor_core.policy import parse_policy

    with pytest.raises(Exception, match="moved to the `delivery` section"):
        parse_policy(TEMPLATE_POLICY)
    policy = parse_policy(TEMPLATE_POLICY, recorded=True)
    assert (policy.delivery.scope.max_diff_files, policy.delivery.scope.max_diff_lines) == (30, 900)
    assert policy.delivery.release_train["prod"].mode == "train" and policy.scope.deny_paths == [
        ".choregos/**"
    ]
    moved = fold_policy_text(TEMPLATE_POLICY)
    assert "# the reviewers' reading" in moved and "# Trains per environment." in moved
    assert parse_policy(moved) == policy, "the stored text reads as the template, without the fold"
    assert fold_policy_text(moved) == moved
