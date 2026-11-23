# SPDX-License-Identifier: Apache-2.0
"""The `delivery` sections: what the core's contracts carry for the `software-delivery` plugin (ADR 0181,
round C2).

A stage input's repository and the project's commands, a stage result's branch and commits, a project's
repositories and GitOps, a policy's diff bounds, merge approval and release trains: they are software
delivery's, not the core's. Each lives in a `delivery` section that the plugin owns. Every model here
says so (`__plugin__`), the schemas mark the same sections `x-plugin: software-delivery`, and neither is
on the core's frozen surfaces: the vocabulary guard does not read this module, as it does not read a
node marked `x-plugin`.

The section is the one place these values live: the former fields beside it left with the window
`C2-delivery` (closed early, 2026-11-23). A project or a policy that still writes one is refused, naming
the section that holds it (`refuse_former_project`, `refuse_former_policy`). What still reads the former
spellings of a stage input or a stage result, which recorded Temporal histories carry, is
`tranibor_contracts.recorded`.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .approval import ApprovalRule
from .workflow import Slug

#: The plugin that owns the sections of this module.
PLUGIN = "software-delivery"

#: A stage result's evidence of software delivery, as named measured facts (`evidence.facts`, ADR 0056):
#: what the runner measures after a step (the tests, the checks, the diff), `facts.<name>` in `measured`.
EVIDENCE_FACTS: tuple[str, ...] = (
    "tests_passed",
    "tests_run",
    "tests_failed",
    "coverage_delta",
    "lint",
    "typecheck",
    "security_scan",
    "diff_files",
    "diff_lines",
)


class _Section(BaseModel):
    """Base: unknown fields are refused, like the schemas; each section is the plugin's."""

    model_config = ConfigDict(extra="forbid")
    __plugin__ = PLUGIN


# --- The stage contracts ---------------------------------------------------------------------------


class Repository(_Section):
    """The repository a stage works on. **Absent** for a project without a repository: the runner then
    prepares an empty directory, clones nothing, pushes nothing, and the guarantees that read a diff
    refuse for lack of matter (ADR 0012, limit no. 1)."""

    url: str
    base_branch: str
    work_branch: str
    clone_depth: int = Field(default=50, ge=1)


class Commands(_Section):
    """The project's commands the runner runs after a step."""

    test: str | None = None
    lint: str | None = None
    typecheck: str | None = None


class StageDelivery(_Section):
    """A stage input's `delivery` section."""

    repo: Repository | None = None
    commands: Commands = Field(default_factory=Commands)


class ResultDelivery(_Section):
    """A stage result's `delivery` section: the branch the step pushed and its commits."""

    branch: str | None = None
    commits: list[str] = Field(default_factory=list)


def repository_of(stage_input: Any) -> Repository | None:
    """The repository a stage works on, from its `delivery` section."""
    section = stage_input.delivery
    return section.repo if section is not None else None


def commands_of(stage_input: Any) -> Commands:
    """The project's commands, from the `delivery` section."""
    section = stage_input.delivery
    return section.commands if section is not None else Commands()


def without_commands(stage_input: Any) -> Any:
    """The same stage input with every project command set to the empty string: what the runner checks
    with when no repository was cloned."""
    section = stage_input.delivery or StageDelivery()
    blank = section.model_copy(update={"commands": Commands(test="", lint="", typecheck="")})
    return stage_input.model_copy(update={"delivery": blank})


def branch_of(artifacts: Any) -> str | None:
    """A stage result's branch, from its `delivery` section."""
    section = artifacts.delivery
    return section.branch if section is not None else None


def commits_of(artifacts: Any) -> list[str]:
    """A stage result's commits, from its `delivery` section."""
    section = artifacts.delivery
    return list(section.commits) if section is not None else []


# --- The project ------------------------------------------------------------------------------------


class RepoConfig(_Section):
    #: The repository's name in its project (ADR 0124): what a work item's `fields.repository` names.
    #: Optional on the primary `repo`, named after the last segment of its URL when absent.
    name: Slug | None = None
    url: str
    default_branch: str = "main"
    language: Literal["python", "node", "go", "java", "dotnet", "rust", "other"] | None = None
    clone_depth: int = Field(default=50, ge=1)
    branch_prefix: str = "choregos/"
    test_command: str | None = None
    lint_command: str | None = None
    typecheck_command: str | None = None


class NamedRepository(RepoConfig):
    """Another repository of the project (ADR 0124): a work item works on it when its
    `fields.repository` names it. The fields of `RepoConfig`, its `name` required.

        delivery:
          repositories:
            - name: diamctl
              url: https://github.com/acme/diamctl.git
              test_command: make test
    """

    name: Slug


def name_of_url(url: str) -> str:
    """The name a repository gets from its URL's last segment, as a slug: `.../Infra.git` → `infra`.

    `primary` when nothing of the segment is left: a slug cannot be empty."""
    last = url.strip().removesuffix("/").removesuffix(".git").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    slug = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", last.lower())).strip("-")[:63].strip("-")
    return slug or "primary"


def _same_url(url: str) -> str:
    """A URL as two declarations of the same repository compare: no `.git`, no trailing slash, no case."""
    return url.strip().removesuffix("/").removesuffix(".git").lower()


class GitopsConfig(_Section):
    repo_url: str | None = None
    path_prefix: str = "apps"
    apps: list[str] = Field(default_factory=list)


class ProjectDelivery(_Section):
    """A project's `delivery` section: its repositories and its GitOps.

    The repository is **optional**: a trade that processes case files, qualifies profiles or drafts
    letters has none (limit no. 1 of ADR 0012). Without one the agent works in an empty directory,
    nothing is cloned or pushed, and the guarantees that read a diff refuse, having nothing to read."""

    repo: RepoConfig | None = None
    #: The project's other repositories (ADR 0124): `repo` stays the primary, the one a work item that
    #: names none works on. Refused without `repo`, with two equal names (the primary's included) and
    #: with two names for one URL.
    repositories: list[NamedRepository] = Field(default_factory=list)
    gitops: GitopsConfig | None = None

    @model_validator(mode="after")
    def _repositories_named_once(self) -> ProjectDelivery:
        """ADR 0124 §1: the primary is explicit, a name designates one repository, a URL has one name."""
        if not self.repositories:
            return self
        if self.repo is None:
            raise ValueError(
                "`delivery.repositories` without `delivery.repo`: the primary repository is the one a work "
                "item that names none works on, declare it in `delivery.repo`"
            )
        names = self.repository_names()
        if twice := sorted({n for n in names if names.count(n) > 1}):
            hint = (
                f" (the primary is named `{self.primary_name}` after its URL: give `repo.name`)"
                if self.repo.name is None and self.primary_name in twice
                else ""
            )
            raise ValueError(f"two repositories named {', '.join(f'`{n}`' for n in twice)}{hint}")
        urls = [_same_url(r.url) for r in (self.repo, *self.repositories)]
        if twice_url := sorted({u for u in urls if urls.count(u) > 1}):
            raise ValueError(f"one repository under two names: {', '.join(twice_url)}")
        return self

    @property
    def has_repo(self) -> bool:
        """Does the project have a repository? Everything that talks to git goes through here."""
        return self.repo is not None

    @property
    def primary_name(self) -> str | None:
        """The primary repository's name (ADR 0124): `repo.name`, else its URL's last segment."""
        if self.repo is None:
            return None
        return self.repo.name or name_of_url(self.repo.url)

    def repository_names(self) -> list[str]:
        """Every repository's name, the primary's first; empty for a project without a repository."""
        if self.repo is None:
            return []
        return [str(self.primary_name), *(r.name for r in self.repositories)]

    def repository_named(self, name: str | None) -> RepoConfig | None:
        """The repository `name` designates, the primary for `None`; `None` when the project holds none
        by that name."""
        if self.repo is None:
            return None
        if name is None or name == self.primary_name:
            return self.repo
        return next((r for r in self.repositories if r.name == name), None)

    @property
    def repo_url(self) -> str:
        """The repository URL. Raises if the project has none: the caller must have checked.

        Returning an empty string would clone "nothing" and fail three layers further on, on a
        message that would not mention the missing repository."""
        if self.repo is None:
            raise ValueError("the project has no repository (`delivery.repo`)")
        return self.repo.url

    def branch_for(self, key: str, slug_hint: str = "", repo: RepoConfig | None = None) -> str:
        """Deterministic branch name for a work item, with the prefix of the repository it works on
        (`repo`, ADR 0124; the primary when not given)."""
        target = repo or self.repo
        if target is None:
            raise ValueError("the project has no repository (`delivery.repo`)")
        number = key.rsplit("#", 1)[-1] if "#" in key else key.replace("/", "-")
        suffix = f"-{slug_hint}" if slug_hint else ""
        return f"{target.branch_prefix}{number}{suffix}"


#: The fields of a project the section holds since ADR 0181 C2: a document that writes one at its
#: root is refused, naming the section.
PROJECT_FIELDS = ("repo", "repositories", "gitops")


def refuse_former_project(data: Any) -> Any:
    """A project that writes a former field at its root is refused, naming where it lives now."""
    if isinstance(data, dict) and (found := [name for name in PROJECT_FIELDS if name in data]):
        raise ValueError(
            ", ".join(f"`{name}`" for name in found)
            + " moved to the `delivery` section of the project (`delivery."
            + found[0]
            + "`, ADR 0181): the software-delivery plugin's"
        )
    return data


def stage_section(repo: Repository | None, configured: RepoConfig | None) -> StageDelivery | None:
    """A stage input's `delivery` section: what the runner clones (`repo`, `None` when it clones
    nothing), and the commands it measures after the stage, those of the repository the work item works
    on (`configured`); `None` for a project without software."""
    commands = (
        Commands(
            test=configured.test_command,
            lint=configured.lint_command,
            typecheck=configured.typecheck_command,
        )
        if configured is not None
        else Commands()
    )
    if repo is None and not commands.model_dump(exclude_none=True):
        return None
    return StageDelivery(repo=repo, commands=commands)


# --- The policy -------------------------------------------------------------------------------------


class DiffBounds(_Section):
    """A policy's bounds on a diff."""

    max_diff_files: int = Field(default=60, ge=1)
    max_diff_lines: int = Field(default=3000, ge=1)


class DeliveryApprovals(_Section):
    """A policy's approval of a merge."""

    merge: ApprovalRule | None = None


class TrainApproval(_Section):
    group: str | None = None
    required: bool = False
    timeout_hours: int = Field(default=4, ge=1)


class CanarySpec(_Section):
    steps: list[int] = Field(default_factory=list)
    analysis: str | None = None
    step_minutes: list[int] = Field(default_factory=list)


class ExpressLane(_Section):
    label: str = "hotfix"
    soak_minutes: int = Field(default=5, ge=0)
    skip_schedule: bool = True
    approval: TrainApproval | None = None


class TerraformSpec(_Section):
    via: Literal["atlantis", "none"] = "none"
    apply_requires_approval: bool = True


class TrainEnvPolicy(_Section):
    mode: Literal["auto_sync", "train"] = "train"
    schedule: str | None = None
    timezone: str = "Europe/Paris"
    windows: list[str] = Field(default_factory=list)
    batch_min: int = Field(default=1, ge=1)
    batch_max: int = Field(default=8, ge=1)
    soak_minutes: int = Field(default=10, ge=0)
    approval: TrainApproval | None = None
    canary: CanarySpec | None = None
    express_lane: ExpressLane | None = None
    freeze: bool = False
    auto_rollback: bool = True
    freeze_on_rollback: bool = True
    terraform: TerraformSpec | None = None


class PolicyDelivery(_Section):
    """A policy's `delivery` section: its diff bounds, its merge approval and its release trains."""

    scope: DiffBounds = Field(default_factory=DiffBounds)
    approvals: DeliveryApprovals = Field(default_factory=DeliveryApprovals)
    release_train: dict[str, TrainEnvPolicy] = Field(default_factory=dict)


#: The fields of a policy the section holds since ADR 0181 C2, as (section, name): `None` for the root.
POLICY_FIELDS: tuple[tuple[str | None, str], ...] = (
    ("scope", "max_diff_files"),
    ("scope", "max_diff_lines"),
    ("approvals", "merge"),
    (None, "release_train"),
)


def refuse_former_policy(data: Any) -> Any:
    """A policy that writes a former field is refused, naming where it lives now."""
    if not isinstance(data, dict):
        return data
    found = [
        f"{parent}.{name}" if parent else name
        for parent, name in POLICY_FIELDS
        if (name in data if parent is None else isinstance(data.get(parent), dict) and name in data[parent])
    ]
    if found:
        raise ValueError(
            ", ".join(f"`{path}`" for path in found)
            + " moved to the `delivery` section of the policy (`delivery."
            + found[0]
            + "`, ADR 0181): the software-delivery plugin's"
        )
    return data


__all__ = [
    "EVIDENCE_FACTS",
    "PLUGIN",
    "POLICY_FIELDS",
    "PROJECT_FIELDS",
    "CanarySpec",
    "Commands",
    "DeliveryApprovals",
    "DiffBounds",
    "ExpressLane",
    "GitopsConfig",
    "NamedRepository",
    "PolicyDelivery",
    "ProjectDelivery",
    "RepoConfig",
    "Repository",
    "ResultDelivery",
    "StageDelivery",
    "TerraformSpec",
    "TrainApproval",
    "TrainEnvPolicy",
    "branch_of",
    "commands_of",
    "commits_of",
    "name_of_url",
    "refuse_former_policy",
    "refuse_former_project",
    "repository_of",
    "stage_section",
    "without_commands",
]
