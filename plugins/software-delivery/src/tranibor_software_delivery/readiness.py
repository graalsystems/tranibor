# SPDX-License-Identifier: Apache-2.0
"""The readiness of a project that delivers code (ADR 0181, round D): its repository is set, and the runner
measures its tests. Asked only when a workflow needs the source control (`scm` among its requirements).

It was the core's `services/readiness.item_repository` until round D; the extension declares it through
`extensions.declare_readiness_item`, and the core evaluates it after the connectors' items, where it was.
"""

from __future__ import annotations

from typing import Any


def item_repository(f: Any) -> list[Any]:
    from tranibor_api.services.readiness import (
        has_repository,
        readiness_item,
        settings_fix,
        workflow_without_repository,
    )

    reasons = dict(f.requirements).get("scm")
    if reasons is None:
        return []
    fix = settings_fix(f.slug, "general")
    repo = f.config.repo
    if not has_repository(f):
        # Asked by a workflow, not by the project: one without a repository changes its workflow.
        return [
            readiness_item(
                "repository",
                "general",
                "Repository set",
                "todo",
                f"the project names no repository URL ({reasons[0] if reasons else 'a workflow needs one'}): "
                "set one, or make default a workflow that does not deliver code, such as `default-request`.",
                workflow_without_repository(f),
            )
        ]
    assert repo is not None
    items = [readiness_item("repository", "general", "Repository set", "done", f"`{repo.url}`.", fix)]
    measured = bool(repo.test_command) or bool(f.config.dod.facts)
    items.append(
        readiness_item(
            "repository-checks",
            "general",
            "Tests measured by the runner",
            "done",
            "a test command is set.",
            fix,
        )
        if measured
        else readiness_item(
            "repository-checks",
            "general",
            "Tests measured by the runner",
            "check",
            "no test command: the runner measures no tests, lint or types, only what the agent says.",
            fix,
        )
    )
    return items
