# SPDX-License-Identifier: Apache-2.0
"""`choregos admin temporal-keys` (ADR 0225): what each command asks the API, and what it prints.

The API is mocked with respx; `apps/api/tests/test_temporal_data_keys.py` proves the routes themselves.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import respx
from httpx import Response
from typer.testing import CliRunner

API = "http://api.test"
BASE = f"{API}/api/v1/orgs/acme/temporal-keys"


@pytest.fixture(autouse=True)
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tranibor_cli import client as client_module

    monkeypatch.setattr(client_module, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setenv("TRANIBOR_API_URL", API)
    monkeypatch.setenv("TRANIBOR_TOKEN", "chg_test")
    monkeypatch.setenv("TRANIBOR_ORG", "acme")
    monkeypatch.setenv("COLUMNS", "200")


def _run(*words: str, given: str | None = None) -> Any:
    from tranibor_cli.main import app

    return CliRunner().invoke(app, ["admin", "temporal-keys", *words], input=given)


def _key(**changes: Any) -> dict[str, Any]:
    return {
        "key_id": "dk-0123456789abcdef",
        "state": "active",
        "kek_id": "k1",
        "created_at": "2026-11-25T10:00:00Z",
    } | changes


@respx.mock
def test_list_shows_the_keys_and_never_a_key() -> None:
    respx.get(BASE).mock(
        return_value=Response(
            200,
            json={"org": "acme", "available": True, "keys": [_key(state="retired"), _key(key_id="dk-2")]},
        )
    )
    result = _run("list")
    assert result.exit_code == 0, result.output
    assert "dk-0123456789abcdef" in result.output and "retired" in result.output and "dk-2" in result.output


@respx.mock
def test_list_says_when_the_keys_are_derived() -> None:
    respx.get(BASE).mock(return_value=Response(200, json={"org": "acme", "available": False, "keys": []}))
    result = _run("list")
    assert result.exit_code == 0 and "cannot be shredded" in " ".join(result.output.split())


@respx.mock
def test_rotate_posts_the_rotation() -> None:
    route = respx.post(f"{BASE}/rotation").mock(return_value=Response(200, json=_key(key_id="dk-new")))
    result = _run("rotate")
    assert result.exit_code == 0, result.output
    assert route.called and "dk-new" in result.output


@respx.mock
def test_shred_asks_for_the_slug_and_sends_the_reason() -> None:
    route = respx.post(f"{BASE}/shredding").mock(
        return_value=Response(
            200, json={"org": "acme", "shredded": ["dk-1", "dk-2"], "not_erased": ["search attributes"]}
        )
    )
    result = _run("shred", "--reason", "the organisation left", given="acme\n")
    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content) == {
        "confirm": "acme",
        "reason": "the organisation left",
    }
    assert "2 Temporal data key(s) shredded" in result.output and "search attributes" in result.output


@respx.mock
def test_shred_with_confirm_does_not_prompt() -> None:
    route = respx.post(f"{BASE}/shredding").mock(
        return_value=Response(200, json={"org": "acme", "shredded": [], "not_erased": []})
    )
    result = _run("shred", "--reason", "again", "--confirm", "acme")
    assert result.exit_code == 0, result.output
    assert json.loads(route.calls.last.request.content)["confirm"] == "acme"
