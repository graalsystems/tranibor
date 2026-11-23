# SPDX-License-Identifier: Apache-2.0
"""The stored documents write software delivery in their `delivery` sections only (ADR 0181 C2, S83-04r)

Round C2 of ADR 0181 gave the project, the policy and the stage contracts a `delivery` section, the
`software-delivery` plugin's, and kept the former fields beside it for a window. The windows closed early
(2026-11-23): the former fields left the contracts, and a project or a policy that writes one is refused.
This migration rewrites what the platform stored under the former fields:

- `projects.config`: `repo`, `repositories`, `gitops` → `delivery.{repo, repositories, gitops}`;
- `policies.json`: `scope.max_diff_files`, `scope.max_diff_lines`, `approvals.merge`, `release_train` →
  `delivery.scope.*`, `delivery.approvals.merge`, `delivery.release_train`; the section is written
  whole, as the contract dumps it;
- `policies.yaml`, the text the policy was written in, which people read and edit: the same move. A
  policy written in block style keeps its text, comments included: the lines of the former fields move
  under a `delivery:` block at the end. Any other text (flow style, a `delivery:` already there, anchors)
  is rendered again from its data, and loses its comments and its layout;
- `runs.result`: a stage result's `artifacts.branch`, `artifacts.commits` → `artifacts.delivery`,
  `artifacts.pr_url` → a `change_request` link, the nine software fields of `evidence` → named facts
  (`evidence.facts`, measured as `facts.<name>`);
- `runs.stage_input` and `agent_sessions.stage_input`: a stage input's `repo` → `delivery.repo`, its
  project's `*_command` → `delivery.commands`, its work item's `links.pr` → `links.change_request`.

A former field and its section both written: the former field wins, as the readers of the window read it
(`read_project`, `read_policy`); for a link the successor wins. The stage contracts' readers still fold
the former spellings (`tranibor_contracts.recorded`), because recorded Temporal histories carry them: the
rows are rewritten so that what the API returns is the new form. `templates.manifest` holds no project or
policy document (a published template names a preset), so it is left alone.

The transforms are inlined: a migration does not import application code, which changes later.

Idempotent: a document without a former field is not touched. The downgrade does nothing: the previous
release reads the `delivery` sections, the named facts and the `change_request` links (they were its
window's successors), so the rewritten rows stay readable by it.

Revision ID: 13e34c50cb88
Revises: c1a3e5f7b9d2
Create Date: 2026-11-23 12:00:00+00:00
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
import yaml
from alembic import op

revision: str = "13e34c50cb88"
down_revision: str | None = "c1a3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PROJECT_FIELDS = ("repo", "repositories", "gitops")
DIFF_FIELDS = ("max_diff_files", "max_diff_lines")
EVIDENCE_FACTS = (
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
COMMANDS = {"test_command": "test", "lint_command": "lint", "typecheck_command": "typecheck"}
CHANGE_REQUEST = "change_request"


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


# --- The documents ----------------------------------------------------------------------------------


def project_config(config: Any) -> Any:
    """A project's configuration with its former fields in its `delivery` section; unchanged without."""
    if not isinstance(config, dict) or not any(name in config for name in PROJECT_FIELDS):
        return config
    config = dict(config)
    section = dict(config.get("delivery") or {})
    for name in PROJECT_FIELDS:
        value = config.pop(name, None)
        if value not in (None, [], {}):
            section[name] = value
    if section:
        config["delivery"] = section
    else:
        config.pop("delivery", None)
    return config


def policy_document(policy: Any) -> Any:
    """A policy with its former fields in its `delivery` section; unchanged without."""
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


def stage_result(result: Any) -> Any:
    """A stage result with its software artifacts in `artifacts.delivery`, its change request as a link
    and its software evidence as named facts; unchanged without a former field."""
    if not isinstance(result, dict):
        return result
    changed = dict(result)
    artifacts = result.get("artifacts")
    if isinstance(artifacts, dict) and {"branch", "commits", "pr_url"} & set(artifacts):
        artifacts = dict(artifacts)
        branch, commits = artifacts.pop("branch", None), artifacts.pop("commits", None) or []
        if branch is not None or commits:
            section = dict(artifacts.get("delivery") or {})
            if section.get("branch") is None and branch is not None:
                section["branch"] = branch
            if not section.get("commits") and commits:
                section["commits"] = list(commits)
            artifacts["delivery"] = section
        url = artifacts.pop("pr_url", None)
        if url:
            links = [dict(link) for link in artifacts.get("links") or [] if isinstance(link, dict)]
            if not any(link.get("rel") == CHANGE_REQUEST for link in links):
                links.insert(0, {"rel": CHANGE_REQUEST, "url": url})
            artifacts["links"] = links
        changed["artifacts"] = artifacts
    evidence = result.get("evidence")
    measured = list(evidence["measured"]) if isinstance(evidence, dict) and isinstance(
        evidence.get("measured"), list
    ) else None
    if isinstance(evidence, dict) and (
        any(name in evidence for name in EVIDENCE_FACTS) or set(EVIDENCE_FACTS) & set(measured or [])
    ):
        evidence = dict(evidence)
        facts = dict(evidence.get("facts") or {})
        for name in EVIDENCE_FACTS:
            value = evidence.pop(name, None)
            if measured is not None and name in measured:
                measured = [entry for entry in measured if entry != name]
                if f"facts.{name}" not in measured:
                    measured.append(f"facts.{name}")
            elif value is not None and measured is not None:
                measured = [entry for entry in measured if entry != f"facts.{name}"]
            if value is not None:
                facts[name] = value
        evidence["facts"] = facts or None
        if measured is not None:
            evidence["measured"] = sorted(measured)
        changed["evidence"] = evidence
    return changed if changed != result else result


def stage_input(document: Any) -> Any:
    """A stage input with its repository and its project's commands in its `delivery` section and its
    work item's change request under `change_request`; unchanged without a former field."""
    if not isinstance(document, dict):
        return document
    changed = dict(document)
    project = document.get("project")
    found_commands = isinstance(project, dict) and any(name in project for name in COMMANDS)
    if "repo" in document or found_commands:
        section = dict(document.get("delivery") or {})
        repo = changed.pop("repo", None)
        if repo is not None and section.get("repo") is None:
            section["repo"] = repo
        if isinstance(project, dict) and found_commands:
            project = dict(project)
            commands = dict(section.get("commands") or {})
            for former, name in COMMANDS.items():
                value = project.pop(former, None)
                if value is not None and commands.get(name) is None:
                    commands[name] = value
            changed["project"] = project
            if any(value is not None for value in commands.values()):
                section["commands"] = commands
        if section.get("repo") is not None or any(
            value is not None for value in dict(section.get("commands") or {}).values()
        ):
            changed["delivery"] = section
    work_item = document.get("work_item")
    links = work_item.get("links") if isinstance(work_item, dict) else None
    if isinstance(links, dict) and "pr" in links:
        links = dict(links)
        former = links.pop("pr")
        if former and not links.get(CHANGE_REQUEST):
            links[CHANGE_REQUEST] = str(former)
        changed["work_item"] = {**dict(work_item or {}), "links": links}
    return changed if changed != document else document


# --- The policy's text ------------------------------------------------------------------------------

_TOP = re.compile(r"^([A-Za-z_][\w-]*)\s*:(.*)$")


def _blocks(lines: list[str]) -> list[tuple[str | None, int, int, int]]:
    """The top-level blocks of a block-style YAML text: (key, first line, key line, end line). The
    comment lines right above a key are its block's; `None` for what precedes the first block."""
    keys = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _TOP.match(line))]
    starts = []
    for line_no, _key in keys:
        first = line_no
        while first > 0 and lines[first - 1].lstrip().startswith("#") and not lines[first - 1].startswith(" "):
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


def policy_text(text: str) -> str:
    """The policy's YAML text with its former fields under `delivery:`; unchanged without any.

    Block style keeps its lines and its comments; anything else is rendered again from its data."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text
    wanted = policy_document(data)
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


# --- The rows ---------------------------------------------------------------------------------------


def _rewrite(table: str, column: str, transform: Any, *, key: str = "id") -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT {key}, {column} FROM {table} WHERE {column} IS NOT NULL")).fetchall()
    target = sa.table(table, sa.column(key), sa.column(column, sa.JSON))
    for row_id, stored in rows:
        document = _json(stored)
        rewritten = transform(document)
        if rewritten is not document and rewritten != document:
            bind.execute(target.update().where(target.c[key] == row_id).values({column: rewritten}))


def _rewrite_policy_text() -> None:
    bind = op.get_bind()
    target = sa.table("policies", sa.column("id"), sa.column("yaml", sa.Text))
    for row_id, text in bind.execute(sa.text("SELECT id, yaml FROM policies")).fetchall():
        if isinstance(text, str):
            rewritten = policy_text(text)
            if rewritten != text:
                bind.execute(target.update().where(target.c.id == row_id).values(yaml=rewritten))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Forced RLS: the migrating role sees no row without this (S39-27, S40-22e).
        bind.execute(sa.text("SELECT set_config('app.current_orgs', '*', true)"))
    _rewrite("projects", "config", project_config)
    _rewrite("policies", "json", policy_document)
    _rewrite_policy_text()
    _rewrite("runs", "result", stage_result)
    _rewrite("runs", "stage_input", stage_input)
    _rewrite("agent_sessions", "stage_input", stage_input)


def downgrade() -> None:
    """Nothing: the previous release reads the `delivery` sections, the named facts and the links."""
