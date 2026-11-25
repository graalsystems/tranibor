"""`tranibor demo import <project> <file>`: a demonstration history posted as it is (ADR 0216)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

API = "http://api.test"
EXAMPLE = (
    Path(__file__).resolve().parents[2] / "contracts" / "schemas" / "examples" / "demo-history.example.json"
)


@pytest.fixture(autouse=True)
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"api_url": API, "token": "chg_x", "org": "acme"}))
    monkeypatch.setenv("TRANIBOR_CONFIG", str(path))
    import tranibor_cli.client as client_module

    monkeypatch.setattr(client_module, "CONFIG_PATH", path)
    return path


def _run(*args: str) -> Any:
    from tranibor_cli.main import app

    return CliRunner().invoke(app, list(args))


COUNTS = {"work_items": 1, "runs": 1, "decisions": 1, "findings": 1}
NONE = {"work_items": 0, "runs": 0, "decisions": 0, "findings": 0}


@respx.mock
def test_the_file_is_posted_as_the_document_and_the_counts_are_printed(tmp_path: Path) -> None:
    route = respx.post(f"{API}/api/v1/projects/acme/billing/demo-history").mock(
        return_value=Response(200, json={"created": COUNTS, "unchanged": NONE, "cost_usd": 0.7035})
    )
    as_yaml = tmp_path / "history.yaml"
    import yaml

    as_yaml.write_text(yaml.safe_dump(json.loads(EXAMPLE.read_text())), encoding="utf-8")

    result = _run("demo", "import", "acme/billing", str(as_yaml))

    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == json.loads(EXAMPLE.read_text())
    text = " ".join(result.output.split())
    assert "1 work items, 1 runs, 1 decisions, 1 findings" in text
    assert "declared, not measured" in text


@respx.mock
def test_a_second_import_says_what_was_already_there() -> None:
    respx.post(f"{API}/api/v1/projects/p1/demo-history").mock(
        return_value=Response(200, json={"created": NONE, "unchanged": COUNTS, "cost_usd": 0.0})
    )

    result = _run("demo", "import", "p1", str(EXAMPLE))

    assert result.exit_code == 0, result.output
    assert "already there" in " ".join(result.output.split())


@respx.mock
def test_a_refusal_of_the_api_is_shown_and_exits_1() -> None:
    respx.post(f"{API}/api/v1/projects/p1/demo-history").mock(
        return_value=Response(
            409,
            json={"title": "Conflict", "status": 409, "detail": "this instance does not import a history"},
        )
    )

    result = _run("demo", "import", "p1", str(EXAMPLE))

    assert result.exit_code == 1
    assert "does not import a history" in " ".join(result.output.split())


def test_a_file_that_is_not_a_mapping_is_refused_before_any_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("- just\n- a list\n", encoding="utf-8")

    result = _run("demo", "import", "p1", str(bad))

    assert result.exit_code == 1
    assert "not a demonstration history" in " ".join(result.output.split())


@respx.mock
def test_the_waiting_requests_an_import_wrote_are_said() -> None:
    respx.post(f"{API}/api/v1/projects/p1/demo-history").mock(
        return_value=Response(
            200, json={"created": {**COUNTS, "pending": 2}, "unchanged": NONE, "cost_usd": 0.0}
        )
    )

    result = _run("demo", "import", "p1", str(EXAMPLE))

    assert result.exit_code == 0, result.output
    assert "waiting for a person, in the inbox: 2 requests" in " ".join(result.output.split())


@respx.mock
def test_remove_deletes_the_projects_imported_history_and_says_what_went() -> None:
    route = respx.delete(f"{API}/api/v1/projects/acme/billing/demo-history").mock(
        return_value=Response(
            200,
            json={
                "work_items": 2,
                "runs": 3,
                "requests": 2,
                "findings": 1,
                "cost_lines": 3,
                "cost_usd": 0.9535,
            },
        )
    )

    result = _run("demo", "remove", "acme/billing")

    assert result.exit_code == 0, result.output
    assert route.called
    text = " ".join(result.output.split())
    assert "2 work items, 3 runs, 2 requests, 1 findings, 3 cost lines ($0.95 declared)" in text


@respx.mock
def test_a_refused_removal_is_shown_and_exits_1() -> None:
    respx.delete(f"{API}/api/v1/projects/p1/demo-history").mock(
        return_value=Response(409, json={"title": "Conflict", "status": 409, "detail": "refused in `prod`"})
    )

    result = _run("demo", "remove", "p1")

    assert result.exit_code == 1
    assert "refused in `prod`" in " ".join(result.output.split())
