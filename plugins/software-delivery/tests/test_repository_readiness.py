# SPDX-License-Identifier: Apache-2.0
"""The readiness of a project that delivers code (ADR 0181, round D, S83-05j): the `software-delivery`
plugin's repository item, declared through `extensions.declare_readiness_item`, and the way out the core's
connector items offer for its `scm` and `cd`. These were `apps/api/tests/test_readiness_items.py`'s."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tranibor_api.extensions import readiness_items
from tranibor_api.services.readiness import Facts, evaluate, item_connectors
from tranibor_contracts import ProjectConfig
from tranibor_software_delivery.readiness import item_repository

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


def facts(**changes: Any) -> Facts:
    config = changes.pop("config", None) or ProjectConfig.model_validate({"slug": "dev", "org": "northwind"})
    base: dict[str, Any] = {
        "slug": "dev",
        "status": "active",
        "provision_failure": None,
        "config": config,
        "workflows": ("dev-simple",),
        "default_workflow": "dev-simple",
        "requirements": (),
        "connectors": {},
        "provided": frozenset({"runtime", "gateway", "memory", "tracker"}),
        "now": NOW,
    }
    return Facts(**{**base, **changes})


def states(items: list[Any]) -> dict[str, str]:
    return {i.id: i.state for i in items}


def test_the_extension_declares_the_repository_item_and_the_core_evaluates_it() -> None:
    assert item_repository in readiness_items()
    required = (("scm", ("an agent works in a repository",)),)
    ids = [item.id for item in evaluate(facts(requirements=required)).items]
    assert "repository" in ids and ids.index("repository") > ids.index("connector-scm")
    assert "repository" not in [item.id for item in evaluate(facts()).items], (
        "asked only when a workflow needs it"
    )


def test_no_test_command_is_to_check() -> None:
    required = (("scm", ("an agent works in a repository",)),)
    bare = ProjectConfig.model_validate(
        {"slug": "dev", "org": "northwind", "delivery": {"repo": {"url": "https://github.com/a/b"}}}
    )
    measured = ProjectConfig.model_validate(
        {
            "slug": "dev",
            "org": "northwind",
            "delivery": {"repo": {"url": "https://github.com/a/b", "test_command": "pytest -q"}},
        }
    )
    assert states(item_repository(facts(requirements=required, config=bare))) == {
        "repository": "done",
        "repository-checks": "check",
    }
    assert (
        states(item_repository(facts(requirements=required, config=measured)))["repository-checks"] == "done"
    )
    assert states(item_repository(facts(requirements=required)))["repository"] == "todo", "no URL"
    assert item_repository(facts()) == [], "no scm required: nothing to say about a repository"


def test_without_a_repository_the_workflow_that_asks_for_one_is_named_and_can_be_changed() -> None:
    required = (
        ("scm", ("default-simple: the agent dev (implement) works in a repository",)),
        ("cd", ("default-simple: the state deployed_prod is production",)),
    )
    (repository,) = item_repository(facts(requirements=required))
    assert repository.state == "todo" and "default-simple: the agent dev" in repository.detail
    assert "a workflow that does not deliver code" in repository.detail
    scm, cd = item_connectors(facts(requirements=required))
    assert scm.state == cd.state == "todo"
    gesture = "/p/dev/workflows/new?template=default-request&default=1"
    assert scm.fix.href == cd.fix.href == repository.fix.href == gesture, "the way out is another workflow"
    published = facts(requirements=required, workflows=("default-simple", "default-request"))
    assert item_repository(published)[0].fix.href == "/p/dev/workflows", "published: make it the default"
    with_repo = ProjectConfig.model_validate(
        {
            "slug": "dev",
            "org": "northwind",
            "delivery": {"repo": {"url": "https://github.com/northwind/dev.git"}},
        }
    )
    (scm,) = item_connectors(facts(requirements=required[:1], config=with_repo))
    assert scm.fix.href == "/p/dev/settings#connections", "a project with a repository connects it"


def test_the_dod_facts_count_as_measured_checks() -> None:
    config = ProjectConfig.model_validate(
        {
            "slug": "dev",
            "org": "northwind",
            "dod": {"facts": {"ok": "true"}},
            "delivery": {"repo": {"url": "https://github.com/a/b"}},
        }
    )
    got = states(item_repository(facts(requirements=(("scm", ("x",)),), config=config)))
    assert got["repository-checks"] == "done"
