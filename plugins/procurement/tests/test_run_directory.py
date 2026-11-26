# SPDX-License-Identifier: Apache-2.0
"""The job reads and writes the run directory its runner laid down (ADR 0226, R9a).

From 0.30.0 the runner lays `.tranibor/` (and a copy under the former `.choregos/`) and reads `job.json`
there first; a runner before 0.30.0 lays and reads `.choregos/` only, until 0.32.0.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from tranibor_procurement.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _laid(root: Path, directory: str) -> None:
    (root / directory / "inputs").mkdir(parents=True)
    shutil.copyfile(FIXTURES / "alderbrook-offer.pdf", root / directory / "inputs" / "alderbrook-offer.pdf")
    (root / directory / "fields.json").write_text(json.dumps({"number_format": "decimal_point"}))


def test_under_a_runner_of_0_30_the_job_writes_the_new_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _laid(tmp_path, ".tranibor")
    _laid(tmp_path, ".choregos")
    monkeypatch.setenv("TRANIBOR_WORKSPACE", str(tmp_path))
    assert main(["extract"]) == 0
    assert "alderbrook-offer.pdf" in (tmp_path / ".tranibor" / "job.json").read_text(encoding="utf-8")
    assert not (tmp_path / ".choregos" / "job.json").exists()


def test_under_a_runner_before_0_30_the_job_uses_the_former_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _laid(tmp_path, ".choregos")
    monkeypatch.setenv("TRANIBOR_WORKSPACE", str(tmp_path))
    assert main(["extract"]) == 0
    assert "alderbrook-offer.pdf" in (tmp_path / ".choregos" / "job.json").read_text(encoding="utf-8")
    assert not (tmp_path / ".tranibor").exists()
