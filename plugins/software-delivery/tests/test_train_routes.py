"""The release train's routes are the `software-delivery` plugin's, served through `extensions.declare_router`
(ADR 0181, round D).

The eight operations of the `trains` tag, which `openapi.yaml` marks `x-plugin: software-delivery`, left the
API's routers for `tranibor_software_delivery.train.api`. The API serves them because the extension declared
them when it loaded, under the same paths and operation ids; without the extension it serves none of them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from tranibor_software_delivery.extension import register

ROOT = Path(__file__).resolve().parents[3]
TRAIN_OPERATIONS = {
    "listReleases",
    "getRelease",
    "getTrainStatus",
    "departTrain",
    "freezeTrain",
    "unfreezeTrain",
    "approveRelease",
    "abortRelease",
}


def _operation_ids(app: Any) -> set[str]:
    return {
        operation["operationId"]
        for item in app.openapi()["paths"].values()
        for method, operation in item.items()
        if method in {"get", "post", "put", "patch", "delete"} and "operationId" in operation
    }


@pytest.fixture
def without_the_routes(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """A deployment whose extension declares no route: the plugin's `declare_api_parts` does nothing."""
    from tranibor_api.extensions import reset
    from tranibor_software_delivery import extension

    reset()
    monkeypatch.setattr(extension, "declare_api_parts", lambda: None)
    try:
        yield
    finally:
        monkeypatch.undo()
        reset()
        register()


def test_the_api_serves_the_train_s_operations_through_the_extension() -> None:
    from tranibor_api.extensions import declared_routers
    from tranibor_api.main import create_app
    from tranibor_software_delivery.train.api import router

    app = create_app()
    assert router in declared_routers()
    assert _operation_ids(app) >= TRAIN_OPERATIONS
    paths = set(app.openapi()["paths"])
    assert {"/api/v1/projects/{id}/trains/{env}", "/api/v1/releases/{id}/approve"} <= paths


def test_without_the_extension_s_routes_the_api_serves_none_of_them(without_the_routes: None) -> None:
    from tranibor_api.main import create_app

    assert not TRAIN_OPERATIONS & _operation_ids(create_app())


def test_the_core_s_routers_hold_no_train_route() -> None:
    routers = ROOT / "apps" / "api" / "src" / "tranibor_api" / "routers"
    assert not (routers / "trains.py").exists()
    source = "\n".join(path.read_text(encoding="utf-8") for path in routers.rglob("*.py"))
    assert not [op for op in TRAIN_OPERATIONS if f'operation_id="{op}"' in source]
