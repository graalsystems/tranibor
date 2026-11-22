# SPDX-License-Identifier: Apache-2.0
"""Two seams an extension's trade asks of the API (ADR 0181, round D, S83-05j): a readiness item of its
own (`declare_readiness_item`), and a hook on the inbound events its work items receive
(`declare_inbound_hook`). The `software-delivery` plugin declares its repository item and boards a merged
work item on its train through them; these tests use a plugin of their own."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from tranibor_api import extensions
from tranibor_api.schemas import ReadinessFix, ReadinessItem
from tranibor_api.services.readiness import Facts, evaluate
from tranibor_contracts import ProjectConfig

PLUGIN = "itsm-test"


def _forget() -> None:
    for name in [n for n, (owner, _) in extensions._READINESS_ITEMS.items() if owner == PLUGIN]:
        del extensions._READINESS_ITEMS[name]
    for kind in [k for k, (owner, _) in extensions._INBOUND_HOOKS.items() if owner == PLUGIN]:
        del extensions._INBOUND_HOOKS[kind]


@pytest.fixture
def clean() -> Iterator[None]:
    _forget()
    yield
    _forget()


def _facts() -> Facts:
    from datetime import UTC, datetime

    return Facts(
        slug="desk",
        status="active",
        provision_failure=None,
        config=ProjectConfig.model_validate({"slug": "desk", "org": "northwind"}),
        workflows=("default-request",),
        default_workflow="default-request",
        requirements=(),
        connectors={},
        provided=frozenset({"runtime", "gateway", "memory", "tracker"}),
        now=datetime(2026, 11, 21, tzinfo=UTC),
    )


def _queue(f: Facts) -> list[ReadinessItem]:
    fix = ReadinessFix(href=f"/p/{f.slug}/settings#general", by="project_owner")
    return [
        ReadinessItem(
            id="itsm-queue", section="general", title="Queue named", state="todo", detail="x", fix=fix
        )
    ]


def test_an_extension_s_readiness_item_is_evaluated_with_the_core_s(clean: None) -> None:
    assert "itsm-queue" not in [item.id for item in evaluate(_facts()).items]
    extensions.declare_readiness_item("queue", _queue, plugin=PLUGIN)
    extensions.declare_readiness_item("queue", _queue, plugin=PLUGIN)  # idempotent
    readiness = evaluate(_facts())
    assert [item.id for item in readiness.items].count("itsm-queue") == 1 and not readiness.ready
    with pytest.raises(RuntimeError, match="already declared by the plugin `itsm-test`"):
        extensions.declare_readiness_item("queue", _queue, plugin="another")


async def test_an_extension_s_inbound_hook_runs_on_its_event_type_and_names_the_outcome(clean: None) -> None:
    seen: list[str] = []

    async def page(session: Any, project: Any, item: Any, event: Any) -> str:
        seen.append(item.tracker_key)
        return "paged"

    event = SimpleNamespace(type="alert.fired")
    item = SimpleNamespace(tracker_key="INC-7")
    assert await extensions.run_inbound_hooks(None, None, item, event) is None, "nobody hooked it"
    extensions.declare_inbound_hook("alert.fired", page, plugin=PLUGIN)
    assert await extensions.run_inbound_hooks(None, None, item, event) == "paged" and seen == ["INC-7"]
    assert (
        await extensions.run_inbound_hooks(None, None, item, SimpleNamespace(type="tracker.item.created"))
        is None
    )
    with pytest.raises(RuntimeError, match="already hooked by the plugin `itsm-test`"):
        extensions.declare_inbound_hook("alert.fired", page, plugin="another")
