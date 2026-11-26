"""The CLI's profile is `~/.config/tranibor/config.json`; the former is read, never written (ADR 0226 R5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import respx
from httpx import Response
from tranibor_cli import client as client_module
from tranibor_cli.client import Profile
from typer.testing import CliRunner

API = "http://api.test"
#: The module's own values, read at import: the suite's fixtures point them at temporary files later.
DEFAULT, FORMER_PATH = client_module.DEFAULT_CONFIG_PATH, client_module.FORMER_CONFIG_PATH
FORMER = {"api_url": "http://former.test", "token": "chg_former", "org": "acme"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A home whose profile is the default one: `TRANIBOR_CONFIG` names nothing."""
    default = tmp_path / ".config" / "tranibor" / "config.json"
    monkeypatch.setattr(client_module, "DEFAULT_CONFIG_PATH", default)
    monkeypatch.setattr(
        client_module, "FORMER_CONFIG_PATH", tmp_path / ".config" / "choregos" / "config.json"
    )
    monkeypatch.setattr(client_module, "CONFIG_PATH", default)
    return tmp_path


def _write(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_the_profile_s_place_is_named_tranibor() -> None:
    assert DEFAULT.parts[-3:] == (".config", "tranibor", "config.json")
    assert FORMER_PATH.parts[-3:] == (".config", "choregos", "config.json")
    assert DEFAULT.parents[2] == FORMER_PATH.parents[2]


def test_the_former_profile_is_read_when_the_new_one_is_absent(home: Path) -> None:
    _write(client_module.FORMER_CONFIG_PATH, FORMER)
    assert client_module.profile_path() == client_module.FORMER_CONFIG_PATH
    assert Profile.load() == Profile(**FORMER)


def test_the_new_profile_wins_over_the_former(home: Path) -> None:
    _write(client_module.FORMER_CONFIG_PATH, FORMER)
    _write(client_module.DEFAULT_CONFIG_PATH, {**FORMER, "token": "chg_new"})
    assert client_module.profile_path() == client_module.DEFAULT_CONFIG_PATH
    assert Profile.load().token == "chg_new"


def test_saving_writes_the_new_file_and_leaves_the_former_as_it_was(home: Path) -> None:
    _write(client_module.FORMER_CONFIG_PATH, FORMER)
    before = client_module.FORMER_CONFIG_PATH.read_bytes()
    profile = Profile.load()
    profile.org = "globex"
    assert profile.save() == client_module.DEFAULT_CONFIG_PATH
    assert json.loads(client_module.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["org"] == "globex"
    assert client_module.FORMER_CONFIG_PATH.read_bytes() == before


def test_a_file_named_by_the_variable_is_never_swapped_for_the_former_one(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(client_module.FORMER_CONFIG_PATH, FORMER)
    monkeypatch.setattr(client_module, "CONFIG_PATH", home / "elsewhere.json")
    monkeypatch.delenv("TRANIBOR_TOKEN", raising=False)
    assert client_module.profile_path() is None
    assert Profile.load().token == ""


@respx.mock
def test_login_writes_the_new_profile_and_keeps_the_former_s_organisation(home: Path) -> None:
    from tranibor_cli.main import app

    _write(client_module.FORMER_CONFIG_PATH, {**FORMER, "api_url": API})
    before = client_module.FORMER_CONFIG_PATH.read_bytes()
    respx.get(f"{API}/api/v1/me").mock(return_value=Response(200, json={"email": "v@example.test"}))
    result = CliRunner().invoke(app, ["login", "--api-url", API, "--token", "chg_new"])
    assert result.exit_code == 0, result.output
    written = json.loads(client_module.DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    assert written == {"api_url": API, "token": "chg_new", "org": "acme"}
    assert client_module.FORMER_CONFIG_PATH.read_bytes() == before
