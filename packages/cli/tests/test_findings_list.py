"""`tranibor findings list` shows the title a workflow gives a finding's type (ADR 0217)."""

from __future__ import annotations

from typing import Any

import pytest
from tranibor_cli import main
from typer.testing import CliRunner


class _Client:
    def get(self, path: str, params: Any = None) -> dict[str, Any]:
        assert path == "/projects/warranty/findings"
        item = {"id": "f1f1f1f1-0000", "severity": "high", "status": "pending", "title": "Serial 4471"}
        return {
            "items": [
                {**item, "type": "duplicate-claim", "type_title": "Suspected duplicate claim"},
                {**item, "id": "f2f2f2f2-0000", "type": "security", "type_title": None},
            ]
        }


def test_the_list_shows_the_declared_title_beside_the_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main, "client", _Client)
    monkeypatch.setenv("COLUMNS", "200")
    shown = CliRunner().invoke(main.app, ["findings", "list", "warranty"])
    assert shown.exit_code == 0, shown.output
    assert "duplicate-claim (Suspected duplicate claim)" in shown.output
    assert "security" in shown.output and "security (" not in shown.output
