"""An agent never writes a file that holds a person's model subscription (S87-20, ADR 0227)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tranibor_contracts import Permissions
from tranibor_runner.guardrails import GuardRails


@pytest.mark.parametrize("name", ["src/.claude/.credentials.json", "src/.gemini/oauth_creds.json"])
def test_the_vendor_credential_files_are_refused(tmp_path: Path, name: str) -> None:
    """Claude Code's credentials file and gemini-cli's cached Google sign-in, even inside the scope."""
    rails = GuardRails(
        Permissions(write_paths=["src/**"], deny_paths=[]), ["src/**"], workspace=str(tmp_path)
    )
    decision = rails.check_write(str(tmp_path / name))
    assert not decision.allowed
    assert "sensitive file" in decision.reason
    assert rails.check_write(str(tmp_path / "src" / "notes.json")).allowed
