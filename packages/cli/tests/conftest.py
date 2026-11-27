"""CLI fixtures: no test meets the version check (S62-08) unless it asks for it."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

try:  # pytest-textual-snapshot 1.0 names the extension `_file_extension`; syrupy 5+ reads `file_extension`
    from pytest_textual_snapshot import SVGImageExtension

    SVGImageExtension.file_extension = "svg"
except ImportError:  # the plugin is a test dependency of the workspace's snapshots only
    pass

OPENAPI = Path(__file__).resolve().parents[3] / "packages" / "contracts" / "openapi.yaml"


@pytest.fixture(autouse=True)
def _no_real_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # `client.CONFIG_PATH` is read from the environment when the module is first imported: a suite that
    # imports it at collection (the workspace's) would freeze the developer's real profile for the ones
    # that import it later. A suite that wants a profile file points `CONFIG_PATH` at its own.
    from tranibor_cli import client

    monkeypatch.setattr(client, "CONFIG_PATH", tmp_path / "no-profile.json")
    # The former profile (ADR 0226 R5) is read only beside the default one; never the developer's either.
    monkeypatch.setattr(client, "FORMER_CONFIG_PATH", tmp_path / "no-former-profile.json")
    # Everything else derived from the home directory (profile, version-check cache, tokens) too (#1777).
    home = tmp_path_factory.mktemp("home")  # not under `tmp_path`: tests list that directory
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.delenv("TRANIBOR_CONFIG", raising=False)


#: The width every CLI test's output is drawn at, whatever the terminal that runs the suite (#2280).
TEST_COLUMNS = "200"


@pytest.fixture(autouse=True)
def _a_terminal_width_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    # Rich wraps at the terminal's width: `COLUMNS` when it is set, else the size of the process's terminal.
    # A console built with no width reads `COLUMNS` at each print, so the suite sets it, and a test that
    # wants a narrow terminal sets its own. A console built while `COLUMNS` was set (the CLI's module-level
    # ones, at import, under `COLUMNS=60 pytest` or inside an earlier test) keeps that width for good:
    # each test gives it back the width of the environment.
    from rich.console import Console

    monkeypatch.setenv("COLUMNS", TEST_COLUMNS)
    for name, module in list(sys.modules.items()):
        if not (name == "tranibor_cli" or name.startswith("tranibor_cli.")):
            continue
        for value in list(vars(module).values()):
            if isinstance(value, Console):
                monkeypatch.setattr(value, "_width", None)


@pytest.fixture(autouse=True)
def _currency_read_afresh() -> None:
    # The organisation's currency is read once a process (S67-25): each test reads it again.
    from tranibor_cli import money

    money.forget()


@pytest.fixture(autouse=True)
def _no_version_check(monkeypatch: pytest.MonkeyPatch) -> None:
    # The check reads `GET /edition` once a process and writes under ~/.cache: the suites mock
    # their own routes and must not write there. `test_self_update.py` turns it back on.
    monkeypatch.setenv("TRANIBOR_NO_VERSION_CHECK", "1")


@pytest.fixture(scope="session")
def contract() -> Any:
    """`contract(name, payload)`: the payload a test's fake API serves, validated against `openapi.yaml`'s
    schema of that name, so a fake cannot drift from what the platform answers (ADR 0005)."""
    import jsonschema
    import yaml
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    document = yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))
    registry = Registry().with_resource("openapi.yaml", Resource.from_contents(document, DRAFT202012))

    def check(name: str, payload: Any) -> Any:
        schema = {"$ref": f"openapi.yaml#/components/schemas/{name}"}
        jsonschema.Draft202012Validator(schema, registry=registry).validate(payload)
        return payload

    return check
