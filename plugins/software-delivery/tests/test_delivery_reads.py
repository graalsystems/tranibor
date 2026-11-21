"""A run's diff, a project's DORA metrics and the project figures of software delivery are the plugin's
(ADR 0181, round D).

`getRunDiff` and `getProjectDora` left the core's `runs` and `costs` routers for
`tranibor_software_delivery.api`, served through `extensions.declare_router`; `Project.has_release_train` and
`ProjectStats.trains_pending` are filled by the extension through `extensions.declare_project_figure`, and
keep their default without it.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tranibor_software_delivery import NAME
from tranibor_software_delivery.extension import register

ROOT = Path(__file__).resolve().parents[3]
READS = {"getRunDiff", "getProjectDora"}


def _operation_ids(app: Any) -> set[str]:
    return {
        operation["operationId"]
        for item in app.openapi()["paths"].values()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"} and "operationId" in operation
    }


@pytest.fixture
def without_the_extension(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A deployment whose extension declares neither route nor figure."""
    from tranibor_api.extensions import reset
    from tranibor_software_delivery import extension

    reset()
    monkeypatch.setattr(extension, "declare_routes", lambda: None)
    try:
        yield
    finally:
        monkeypatch.undo()
        reset()
        register()


def test_the_api_serves_the_diff_and_the_dora_metrics_through_the_extension() -> None:
    from tranibor_api.main import create_app

    app = create_app()
    assert _operation_ids(app) >= READS
    tags = {
        operation["operationId"]: operation["tags"]
        for item in app.openapi()["paths"].values()
        for operation in item.values()
        if isinstance(operation, dict) and operation.get("operationId") in READS
    }
    assert tags == {"getRunDiff": ["runs"], "getProjectDora": ["costs"]}


def test_without_the_extension_the_api_serves_neither(without_the_extension: None) -> None:
    from tranibor_api.main import create_app

    assert not READS & _operation_ids(create_app())


def test_the_core_s_routers_hold_neither() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "apps" / "api" / "src" / "tranibor_api" / "routers").rglob("*.py")
    )
    assert not [op for op in READS if f'operation_id="{op}"' in source]


class _Session:
    """Answers every query with no workflow and two pending releases."""

    async def execute(self, statement: Any) -> Any:
        text = str(statement)
        return SimpleNamespace(
            scalars=lambda: iter([{"transitions": [{"via": "release_train"}]}]),
            scalar_one=lambda: 2 if "releases" in text else 0,
        )


async def test_the_extension_fills_the_project_figures() -> None:
    from tranibor_api.extensions import project_figures

    project = SimpleNamespace(id="p1")
    assert await project_figures(_Session(), project, "Project") == {"has_release_train": True}
    assert await project_figures(_Session(), project, "ProjectStats") == {"trains_pending": 2}


async def test_without_the_extension_the_figures_keep_their_default(without_the_extension: None) -> None:
    from tranibor_api.extensions import project_figures
    from tranibor_api.schemas import ProjectStats

    project = SimpleNamespace(id="p1")
    assert await project_figures(_Session(), project, "Project") == {}
    assert ProjectStats(active_work_items=0, cost_month_eur=0.0).trains_pending == 0


def test_a_plugin_fills_only_its_own_fields() -> None:
    from tranibor_api.extensions import declare_project_figure

    with pytest.raises(ValueError, match="x-plugin"):
        declare_project_figure("ProjectStats", "active_work_items", lambda s, p: 1, plugin=NAME)
    with pytest.raises(ValueError, match="x-plugin: itsm"):
        declare_project_figure("Project", "has_release_train", lambda s, p: True, plugin="itsm")
    with pytest.raises(ValueError, match="goes on"):
        declare_project_figure("Run", "x", lambda s, p: 1, plugin=NAME)
