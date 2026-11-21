# SPDX-License-Identifier: Apache-2.0
"""The `delivery` sections: what the core's contracts carry for the `software-delivery` plugin (ADR 0181,
round C2).

A stage input's repository and the project's commands, a stage result's branch and commits, a project's
repositories and GitOps, a policy's diff bounds, merge approvals and release train: they are software
delivery's, not the core's. Each lives in a `delivery` section that the plugin owns. Every model here
says so (`__plugin__`), the schemas mark the same sections `x-plugin: software-delivery`, and neither is
on the core's frozen surfaces: the vocabulary guard does not read this module, as it does not read a
node marked `x-plugin`.

For one window the former fields stay beside their section, and a document written with either form
validates the same and is written back with both. The models keep the former fields, read a section
into them (`read_*`) and derive the section from them (`*_section`), so the two forms cannot disagree.
The window list (`tranibor_contracts.window`, window `C2-delivery`) names each former field and the
release that removes it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: The plugin that owns the sections of this module.
PLUGIN = "software-delivery"


class _Section(BaseModel):
    """Base: unknown fields are refused, like the schemas; each section is the plugin's."""

    model_config = ConfigDict(extra="forbid")
    __plugin__ = PLUGIN


class Repository(_Section):
    """The repository a stage works on (the former `StageInput.repo`)."""

    url: str
    base_branch: str
    work_branch: str
    clone_depth: int = Field(default=50, ge=1)


class Commands(_Section):
    """The project's commands the runner runs after a step (the former `ProjectRef.*_command`)."""

    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None


class StageDelivery(_Section):
    """A stage input's `delivery` section."""

    repo: Repository | None = None
    commands: Commands = Field(default_factory=Commands)


class ResultDelivery(_Section):
    """A stage result's `delivery` section (the former `Artifacts.branch` and `Artifacts.commits`)."""

    branch: str | None = None
    commits: list[str] = Field(default_factory=list)


#: The former fields of a stage input's project and their name in `Commands`.
COMMAND_FIELDS = {"test_command": "test", "lint_command": "lint", "typecheck_command": "typecheck"}


def read_stage_input(data: Any) -> Any:
    """A stage input document read under either form (ADR 0098 §4): the section's values go to the former
    fields the document did not write, and the section itself is not kept, since the model derives it
    from them (`stage_section`). One source of truth for the window: a copy that edits a former field
    cannot leave a stale section behind."""
    if not isinstance(data, dict) or "delivery" not in data:
        return data
    data = dict(data)
    section = data.pop("delivery") or {}
    if isinstance(section, BaseModel):
        section = section.model_dump()
    if "repo" not in data and section.get("repo") is not None:
        data["repo"] = section["repo"]
    commands = section.get("commands") or {}
    if any(commands.get(name) is not None for name in COMMAND_FIELDS.values()):
        project = data.get("project")
        project = (
            project.model_dump(exclude_unset=True) if isinstance(project, BaseModel) else dict(project or {})
        )
        for former, name in COMMAND_FIELDS.items():
            if former not in project and commands.get(name) is not None:
                project[former] = commands[name]
        data["project"] = project
    return data


def stage_section(stage_input: Any) -> StageDelivery | None:
    """A stage input's `delivery` section, derived from the former fields: `None` when it has none."""
    former = stage_input.repo
    commands = Commands(**{name: getattr(stage_input.project, f) for f, name in COMMAND_FIELDS.items()})
    if former is None and not commands.model_dump(exclude_none=True):
        return None
    repo = Repository.model_validate(former.model_dump()) if former is not None else None
    return StageDelivery(repo=repo, commands=commands)


def read_artifacts(data: Any) -> Any:
    """A stage result's artifacts read under either form: the section's branch and commits go to the
    former fields the document did not write."""
    if not isinstance(data, dict) or "delivery" not in data:
        return data
    data = dict(data)
    section = data.pop("delivery") or {}
    if isinstance(section, BaseModel):
        section = section.model_dump()
    for name in ("branch", "commits"):
        if name not in data and section.get(name):
            data[name] = section[name]
    return data


def result_section(artifacts: Any) -> ResultDelivery | None:
    """A stage result's `delivery` section, derived from the former fields: `None` when it has none."""
    if artifacts.branch is None and not artifacts.commits:
        return None
    return ResultDelivery(branch=artifacts.branch, commits=list(artifacts.commits))


def repository_of(stage_input: Any) -> Repository | None:
    """The repository a stage works on, read through its `delivery` section (ADR 0181 C2)."""
    section = stage_input.delivery
    return section.repo if section is not None else None


def commands_of(stage_input: Any) -> Commands:
    """The project's commands, read through the `delivery` section."""
    section = stage_input.delivery
    return section.commands if section is not None else Commands()


def without_commands(stage_input: Any) -> Any:
    """The same stage input with every project command set to the empty string: what the runner checks
    with when no repository was cloned."""
    project = stage_input.project.model_copy(update=dict.fromkeys(COMMAND_FIELDS, ""))
    return stage_input.model_copy(update={"project": project})


def branch_of(artifacts: Any) -> str | None:
    """A stage result's branch, read through its `delivery` section."""
    section = artifacts.delivery
    return section.branch if section is not None else None


def commits_of(artifacts: Any) -> list[str]:
    """A stage result's commits, read through its `delivery` section."""
    section = artifacts.delivery
    return list(section.commits) if section is not None else []


class ProjectDelivery(_Section):
    """A project's `delivery` section: its repositories and its GitOps (the former `repo`, `repositories`
    and `gitops`). Read into the former fields, which validate them (`read_project`)."""

    repo: dict[str, Any] | None = None
    repositories: list[dict[str, Any]] = Field(default_factory=list)
    gitops: dict[str, Any] | None = None


class DiffBounds(_Section):
    """A policy's bounds on a diff (the former `scope.max_diff_files` and `scope.max_diff_lines`)."""

    max_diff_files: int | None = None
    max_diff_lines: int | None = None


class DeliveryApprovals(_Section):
    """A policy's approval of a merge (the former `approvals.merge`)."""

    merge: dict[str, Any] | None = None


class PolicyDelivery(_Section):
    """A policy's `delivery` section: its diff bounds, its merge approval and its release trains."""

    scope: DiffBounds = Field(default_factory=DiffBounds)
    approvals: DeliveryApprovals = Field(default_factory=DeliveryApprovals)
    release_train: dict[str, dict[str, Any]] = Field(default_factory=dict)


#: The former fields of a project the section holds.
PROJECT_FIELDS = ("repo", "repositories", "gitops")
#: The former fields of a policy's `scope` the section holds.
DIFF_FIELDS = ("max_diff_files", "max_diff_lines")


def _section_of(data: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    data = dict(data)
    section = data.pop("delivery") or {}
    if isinstance(section, BaseModel):
        section = section.model_dump()
    return data, dict(section)


def read_project(data: Any) -> Any:
    """A project configuration read under either form: the section's values go to the former fields the
    document did not write."""
    if not isinstance(data, dict) or "delivery" not in data:
        return data
    data, section = _section_of(data)
    for name in PROJECT_FIELDS:
        if name not in data and section.get(name):
            data[name] = section[name]
    return data


def project_section(config: Any) -> ProjectDelivery | None:
    """A project's `delivery` section, derived from the former fields: `None` when it has none."""
    if config.repo is None and not config.repositories and config.gitops is None:
        return None
    return ProjectDelivery(
        repo=config.repo.model_dump(mode="json", exclude_none=True) if config.repo is not None else None,
        repositories=[r.model_dump(mode="json", exclude_none=True) for r in config.repositories],
        gitops=config.gitops.model_dump(mode="json", exclude_none=True)
        if config.gitops is not None
        else None,
    )


def read_policy(data: Any) -> Any:
    """A policy read under either form: the section's values go to the former fields the document did
    not write (`scope.max_diff_*`, `approvals.merge`, `release_train`)."""
    if not isinstance(data, dict) or "delivery" not in data:
        return data
    data, section = _section_of(data)
    scope = dict(data.get("scope") or {})
    for name in DIFF_FIELDS:
        found = (section.get("scope") or {}).get(name)
        if name not in scope and found is not None:
            scope[name] = found
    if scope:
        data["scope"] = scope
    merge = (section.get("approvals") or {}).get("merge")
    approvals = dict(data.get("approvals") or {})
    if "merge" not in approvals and merge is not None:
        approvals["merge"] = merge
        data["approvals"] = approvals
    if "release_train" not in data and section.get("release_train"):
        data["release_train"] = section["release_train"]
    return data


def policy_section(policy: Any) -> PolicyDelivery:
    """A policy's `delivery` section, derived from the former fields."""
    merge = policy.approvals.merge
    return PolicyDelivery(
        scope=DiffBounds(
            max_diff_files=policy.scope.max_diff_files, max_diff_lines=policy.scope.max_diff_lines
        ),
        approvals=DeliveryApprovals(
            merge=merge.model_dump(mode="json", exclude_none=True) if merge is not None else None
        ),
        release_train={
            env: train.model_dump(mode="json", exclude_none=True)
            for env, train in policy.release_train.items()
        },
    )


__all__ = [
    "COMMAND_FIELDS",
    "DIFF_FIELDS",
    "PLUGIN",
    "PROJECT_FIELDS",
    "Commands",
    "DeliveryApprovals",
    "DiffBounds",
    "PolicyDelivery",
    "ProjectDelivery",
    "Repository",
    "ResultDelivery",
    "StageDelivery",
    "branch_of",
    "commands_of",
    "commits_of",
    "policy_section",
    "project_section",
    "read_artifacts",
    "read_policy",
    "read_project",
    "read_stage_input",
    "repository_of",
    "result_section",
    "stage_section",
    "without_commands",
]
