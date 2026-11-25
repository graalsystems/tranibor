# SPDX-License-Identifier: Apache-2.0
"""Former spellings the platform still READS, from documents it recorded or stored and cannot rewrite.

ADR 0181's windows closed on 2026-11-23, ahead of their dates (the project lead's decision, recorded in
the ADR): no contract writes or accepts a former name any more, and the rows that held one were
rewritten by Alembic data migrations. What no migration can reach stays readable (ADR 0098 §4):

- a stored workflow version is never rewritten, because its digest would move, and a running
  workflow carries its definition in its Temporal history, which is never rewritten either (ADR 0008).
  So a workflow read back from storage (the validation context `{"stored": True}`) reads an action
  approver's former role as its successor; a document sent to the API is refused with it.
- the stage contracts' former software-delivery fields (round C2): a stage input's `repo`, its
  project's `test_command`, `lint_command` and `typecheck_command` and its work item's `links.pr`; a stage
  result's `artifacts.branch`, `artifacts.commits` and `artifacts.pr_url`, and the nine software fields of
  its `evidence` (`tests_passed`... `diff_lines`). Recorded Temporal histories (`tests/replay/histories/`,
  and the histories of the workflows running in a deployment when it upgrades) carry them, and workflow
  code deserialises them on replay through Temporal's data converter, which passes no validation
  context: the reading cannot be limited to "stored" documents, it is always on, and only on these
  internal contracts (the runner, the orchestrator, the internal API), outside the frozen REST surface.
  The agents of the published `software-delivery` content plugin (1.3.0, signed, `plugins/offline/`) are
  also told to write `evidence.tests_passed` and `evidence.tests_run`.
- a stored workflow version that declares no `repairs:` or no `needs:` (written before ADR 0181 C1): the
  platform guessed from its agents' role names which transition repairs `ci_failed` and
  `changes_requested`, and which connector kinds an agent needs. Its version is never rewritten (its
  digest would move) and its Temporal history replays the choice the guess made, with or without the
  `software-delivery` plugin whose roles they are since S83-12: `GUESSED_REPAIR_ROLES`,
  `GUESSED_ROLES_IN_A_REPOSITORY` and `GUESSED_ROLES_IN_PRODUCTION`, read by `tranibor_core.dsl.engine` and
  `tranibor_core.dsl.requirements` for such a workflow only. A workflow that declares `repairs:` (or
  `needs:`) on any of its transitions (agents) is never guessed for.
- the policies of templates and content plugins: a template's `policy.yaml` (in a content plugin's archive
  already signed and published, or in a deployment's own plugins) may still write `scope.max_diff_files`,
  `scope.max_diff_lines`, `approvals.merge` or `release_train` at their former places. A project born from
  such a template reads it through `fold_policy`, and stores its text rewritten by `fold_policy_text`. A
  policy sent through the REST API is not such a document: it is refused
  (`tranibor_contracts.delivery.refuse_former_policy`), as is a project that writes a former field.

A stage document that writes both spellings keeps the successor's value for a link, and the former
field's value for a piece of evidence (what `Evidence.with_facts` wrote while both forms were written: the
field won over an agent's fact of the same name). Each spelling here is read only, never written; the
vocabulary guard does not read this module, for that reason
(`tests/architecture/test_contracts_speak_no_trade.py`).
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from pydantic import BaseModel

#: A stage result's former evidence fields, each now the named fact of the same name
#: (`tranibor_contracts.delivery.EVIDENCE_FACTS`). Kept here, not imported: `workflow` imports this module.
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

#: An action approver's former role, read as its successor in a stored or recorded workflow (ADR 0181 C1).
FORMER_APPROVER_ROLES: dict[str, str] = {"developer": "contributor", "release_captain": "approver"}


#: A stored workflow without `repairs:` (before ADR 0181 C1): the role of the agent whose transition
#: repairs each outcome. The roles are the `software-delivery` plugin's (S83-12); the guess replays.
GUESSED_REPAIR_ROLES: dict[str, str] = {"ci_failed": "fix_ci", "changes_requested": "address_review"}
#: A stored workflow without `needs:` (before ADR 0181 C1): the roles whose agent was taken to work in a
#: repository (it needs an `scm`) ...
GUESSED_ROLES_IN_A_REPOSITORY: frozenset[str] = frozenset(
    {"implement", "fix_ci", "address_review", "review", "release_notes", "architect", "document"}
)
#: ... and those taken to look at production (it needs a `cd`).
GUESSED_ROLES_IN_PRODUCTION: frozenset[str] = frozenset({"verify_prod"})


#: The relation of the link to the change request (`stage.CHANGE_REQUEST`).
_CHANGE_REQUEST = "change_request"
#: A stage input's project commands, by former name, and their name in `delivery.commands`.
FORMER_COMMANDS = {"test_command": "test", "lint_command": "lint", "typecheck_command": "typecheck"}
#: A stage input's work item's former link to its change request.
FORMER_CHANGE_REQUEST_LINK = "pr"
#: A stage result's former link to its change request.
FORMER_CHANGE_REQUEST_URL = "pr_url"


def _plain(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_unset=True)
    return dict(value) if isinstance(value, dict) else {}


def fold_stage_input(data: Any) -> Any:
    """A stage input written with `repo` and `project.*_command`: both go to its `delivery` section."""
    if not isinstance(data, dict):
        return data
    project = data.get("project")
    commands_found = isinstance(project, dict) and any(name in project for name in FORMER_COMMANDS)
    if "repo" not in data and not commands_found:
        return data
    data = dict(data)
    section = _plain(data.get("delivery"))
    repo = data.pop("repo", None)
    if repo is not None and section.get("repo") is None:
        section["repo"] = _plain(repo)
    if isinstance(project, dict) and commands_found:
        project = dict(project)
        commands = _plain(section.get("commands"))
        for former, name in FORMER_COMMANDS.items():
            value = project.pop(former, None)
            if value is not None and commands.get(name) is None:
                commands[name] = value
        data["project"] = project
        if any(value is not None for value in commands.values()):
            section["commands"] = commands
    if section.get("repo") is not None or any(
        v is not None for v in _plain(section.get("commands")).values()
    ):
        data["delivery"] = section
    return data


def fold_work_item_links(data: Any) -> Any:
    """A work item's links written with `pr`: it is the change request's link."""
    if not isinstance(data, dict) or FORMER_CHANGE_REQUEST_LINK not in data:
        return data
    data = dict(data)
    former = data.pop(FORMER_CHANGE_REQUEST_LINK)
    if former and not data.get(_CHANGE_REQUEST):
        data[_CHANGE_REQUEST] = str(former)
    return data


def fold_artifacts(data: Any) -> Any:
    """A stage result's artifacts written with `branch`, `commits` and `pr_url`: the first two go to the
    `delivery` section, the last is the `change_request` link."""
    if not isinstance(data, dict) or not ({"branch", "commits", FORMER_CHANGE_REQUEST_URL} & set(data)):
        return data
    data = dict(data)
    branch, commits = data.pop("branch", None), data.pop("commits", None) or []
    if branch is not None or commits:
        section = _plain(data.get("delivery"))
        if section.get("branch") is None and branch is not None:
            section["branch"] = branch
        if not section.get("commits") and commits:
            section["commits"] = list(commits)
        data["delivery"] = section
    url = data.pop(FORMER_CHANGE_REQUEST_URL, None)
    if url:
        links = [_plain(link) for link in data.get("links") or []]
        if not any(link.get("rel") == _CHANGE_REQUEST for link in links):
            links.insert(0, {"rel": _CHANGE_REQUEST, "url": url})
        data["links"] = links
    return data


def fold_evidence(data: Any) -> Any:
    """A stage result's evidence written with its software fields: each is the named fact (the field
    wins over a fact of the same name), and a field named in `measured` is measured as `facts.<name>`,
    with or without a value, as `is_measured` read it."""
    if not isinstance(data, dict):
        return data
    measured = list(data["measured"]) if isinstance(data.get("measured"), list) else None
    if not any(name in data for name in EVIDENCE_FACTS) and not set(EVIDENCE_FACTS) & set(measured or []):
        return data
    data = dict(data)
    facts = dict(data.get("facts") or {})
    for name in EVIDENCE_FACTS:
        value = data.pop(name, None)
        if measured is not None and name in measured:
            measured = [entry for entry in measured if entry != name]
            if f"facts.{name}" not in measured:
                measured.append(f"facts.{name}")
        elif value is not None and measured is not None:
            measured = [entry for entry in measured if entry != f"facts.{name}"]
        if value is not None:
            facts[name] = value
    data["facts"] = facts or None
    if measured is not None:
        data["measured"] = sorted(measured)
    return data


#: A policy's former fields, as (section, name): `None` for the root.
FORMER_POLICY_FIELDS: tuple[tuple[str | None, str], ...] = (
    ("scope", "max_diff_files"),
    ("scope", "max_diff_lines"),
    ("approvals", "merge"),
    (None, "release_train"),
)
DIFF_FIELDS = ("max_diff_files", "max_diff_lines")


def fold_policy(policy: Any) -> Any:
    """A template's policy written with the former fields: each goes to the `delivery` section, the
    former field winning over the section's (what the window's readers did); unchanged without any."""
    if not isinstance(policy, dict):
        return policy
    scope, approvals = policy.get("scope"), policy.get("approvals")
    found = (
        "release_train" in policy
        or (isinstance(scope, dict) and any(name in scope for name in DIFF_FIELDS))
        or (isinstance(approvals, dict) and "merge" in approvals)
    )
    if not found:
        return policy
    policy = dict(policy)
    section = dict(policy.get("delivery") or {})
    if isinstance(scope, dict):
        scope = dict(scope)
        bounds = dict(section.get("scope") or {})
        for name in DIFF_FIELDS:
            if name in scope:
                value = scope.pop(name)
                if value is not None:
                    bounds[name] = value
        policy["scope"] = scope
        if bounds:
            section["scope"] = bounds
    if isinstance(approvals, dict) and "merge" in approvals:
        approvals = dict(approvals)
        merge = approvals.pop("merge")
        policy["approvals"] = approvals
        if merge is not None:
            section["approvals"] = {**dict(section.get("approvals") or {}), "merge": merge}
    if "release_train" in policy:
        section["release_train"] = policy.pop("release_train")
    policy["delivery"] = section
    return policy


_TOP = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")


def _blocks(lines: list[str]) -> list[tuple[str | None, int, int, int]]:
    """The top-level blocks of a block-style YAML text: (key, first line, key line, end line). The
    comment lines right above a key are its block's; `None` for what precedes the first block."""
    keys = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _TOP.match(line))]
    starts = []
    for line_no, _key in keys:
        first = line_no
        while (
            first > 0 and lines[first - 1].lstrip().startswith("#") and not lines[first - 1].startswith(" ")
        ):
            first -= 1
        starts.append(first)
    blocks: list[tuple[str | None, int, int, int]] = [(None, 0, 0, starts[0] if starts else len(lines))]
    for index, ((line_no, key), first) in enumerate(zip(keys, starts, strict=True)):
        end = starts[index + 1] if index + 1 < len(starts) else len(lines)
        blocks.append((key, first, line_no, end))
    return blocks


def _children(lines: list[str], head: int, end: int, names: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """The second-level entries `names` of a block-style top-level block (their lines, nested lines
    included), and the block's remaining lines from its key on."""
    kept: list[str] = [lines[head]]
    moved: list[str] = []
    index = head + 1
    taking = False
    while index < end:
        line = lines[index]
        child = re.match(r"^(\s+)([A-Za-z_][\w-]*)\s*:", line)
        if child and len(child.group(1)) == 2:
            taking = child.group(2) in names
        elif line.strip() and not line.startswith("   ") and not line.lstrip().startswith("#"):
            taking = False
        (moved if taking else kept).append(line)
        index += 1
    return moved, kept


def fold_policy_text(text: str) -> str:
    """A policy's YAML text with its former fields under `delivery:`; unchanged without any.

    Block style keeps its lines and its comments; anything else is rendered again from its data."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    wanted = fold_policy(data)
    if wanted is data:
        return text
    try:
        moved = _moved_text(text)
        if moved is not None and yaml.safe_load(moved) == wanted:
            return moved
    except (yaml.YAMLError, ValueError, IndexError):
        pass
    return yaml.safe_dump(wanted, sort_keys=False, allow_unicode=True, width=110)


def _moved_text(text: str) -> str | None:
    lines = text.splitlines()
    blocks = _blocks(lines)
    keys = [key for key, _, _, _ in blocks if key is not None]
    if "delivery" in keys or len(keys) != len(set(keys)):
        return None
    out: list[str] = []
    section: dict[str, list[str]] = {}
    for key, first, line_no, end in blocks:
        if key is None:
            out.extend(lines[first:end])
            continue
        found = _TOP.match(lines[line_no])
        assert found is not None
        rest = found.group(2).strip()
        inline = bool(rest) and not rest.startswith("#")
        if key == "release_train":
            section["release_train"] = lines[first:end]
            continue
        if key in ("scope", "approvals"):
            if inline:
                return None
            names = DIFF_FIELDS if key == "scope" else ("merge",)
            moved, kept = _children(lines, line_no, end, names)
            if moved:
                section[key] = [f"{key}:", *moved]
                # A block left with its key alone is an empty mapping, as the data says.
                if not any(line.strip() and not line.lstrip().startswith("#") for line in kept[1:]):
                    kept[0] = f"{key}: {{}}"
            out.extend(lines[first:line_no])
            out.extend(kept)
            continue
        out.extend(lines[first:end])
    if not section:
        return None
    while out and not out[-1].strip():
        out.pop()
    out.append("# The software-delivery plugin's section (ADR 0181 C2): diff bounds, merge approval, trains.")
    out.append("delivery:")
    for key in ("scope", "approvals", "release_train"):
        for line in section.get(key, []):
            out.append(f"  {line}" if line.strip() else line)
    return "\n".join(out) + "\n"


__all__ = [
    "FORMER_APPROVER_ROLES",
    "FORMER_CHANGE_REQUEST_LINK",
    "FORMER_CHANGE_REQUEST_URL",
    "FORMER_COMMANDS",
    "FORMER_POLICY_FIELDS",
    "fold_artifacts",
    "fold_evidence",
    "fold_policy",
    "fold_policy_text",
    "fold_stage_input",
    "fold_work_item_links",
]
