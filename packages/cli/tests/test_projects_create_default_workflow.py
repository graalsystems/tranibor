"""`choregos projects create --default-workflow`: the default is asked for in the creation call (S80-21)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

API = "http://api.test"


@pytest.fixture(autouse=True)
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_url": API, "token": "chg_x", "org": "acme"}))
    monkeypatch.setenv("TRANIBOR_CONFIG", str(path))
    import tranibor_cli.client as client_module

    monkeypatch.setattr(client_module, "CONFIG_PATH", path)
    return path


def _create(*args: str) -> tuple[Any, dict[str, Any]]:
    from tranibor_cli.main import app

    with respx.mock(base_url=API) as router:
        route = router.post("/api/v1/orgs/acme/projects").mock(
            return_value=Response(201, json={"id": "p1", "slug": "hr"})
        )
        router.put("/api/v1/projects/p1/connectors/tracker").mock(return_value=Response(200, json={}))
        result = CliRunner().invoke(app, ["projects", "create", "hr", *args])
        body = json.loads(route.calls.last.request.content) if route.called else {}
    return result, body


def test_the_default_workflow_travels_in_the_creation_request() -> None:
    result, body = _create("--default-workflow", "offboarding")
    assert result.exit_code == 0, result.output
    assert body["default_workflow"] == "offboarding"


def test_without_the_option_the_request_does_not_carry_one() -> None:
    result, body = _create()
    assert result.exit_code == 0, result.output
    assert "default_workflow" not in body
