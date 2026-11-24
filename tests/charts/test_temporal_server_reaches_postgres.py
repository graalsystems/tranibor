"""In `server` mode the embedded Temporal may reach the embedded PostgreSQL (S80-32).

Proven on a k3d cluster: with `choregos-temporal.mode: server` on the single-node profile the Temporal
pod logged "Waiting for PostgreSQL to startup" forever, because `choregos-postgresql-ingress` listed
a fixed set of clients and `choregos-temporal` was not one of them. In `dev` mode Temporal uses
SQLite and needs no such rule.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "choregos"
PROFILE = CHART / "values" / "single-node.yaml"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _clients(*sets: str) -> set[str]:
    command = ["helm", "template", "c", str(CHART), "-n", "choregos", "-f", str(PROFILE)]
    sets = ("backup.target.s3.url=s3://backups/choregos", "backup.target.s3.endpoint=http://s3:8333", *sets)
    for value in sets:
        command += ["--set", value]
    out = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    docs: list[dict[str, Any]] = [d for d in yaml.safe_load_all(out.stdout) if d]
    policy = next(
        d
        for d in docs
        if d["kind"] == "NetworkPolicy" and d["metadata"]["name"] == "choregos-postgresql-ingress"
    )
    names: set[str] = set()
    for rule in policy["spec"]["ingress"]:
        for peer in rule["from"]:
            for expression in peer["podSelector"].get("matchExpressions", []):
                names.update(expression["values"])
    return names


def test_server_mode_temporal_is_a_postgresql_client() -> None:
    assert "choregos-temporal" in _clients("choregos-temporal.mode=server")


def test_dev_mode_temporal_is_not() -> None:
    assert "choregos-temporal" not in _clients("choregos-temporal.mode=dev")
