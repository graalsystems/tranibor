"""The embedded LiteLLM and Ecphoria are a production tier when they rest on no bench (#2150, ADR 0195).

`templates/guard.yaml` refused both in `staging` and `prod` outside the single-node profile. Since S80-14 to
S80-16 they are what an operator runs by hand: a LiteLLM (`litellm-database` image, price sheets) whose
database is the operator's PostgreSQL, an Ecphoria on its volume. So, like the embedded Temporal in `server`
mode, the guard admits them unless they sit on a bench dependency: the gateway on the embedded PostgreSQL,
the memory on an emptyDir. Every other refusal stays. These tests render the whole chart with the
production preset, as an operator's Argo application does.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
PROD = CHART / "values" / "prod.yaml"
E = "choregos-ecphoria"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")

# Offline rendering: the default executor checks for its operator's CRD, which `helm template` lacks.
OFFLINE = ("tranibor-orchestrator.runner.skipOperatorCheck=true",)


def _render(*sets: str) -> subprocess.CompletedProcess[str]:
    cmd = ["helm", "template", "c", str(CHART), "-f", str(PROD)]
    for s in (*OFFLINE, *sets):
        cmd += ["--set", s]
    return subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=120)


def _docs(*sets: str) -> list[dict[str, Any]]:
    out = _render(*sets)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    (doc,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return doc


def _refused(*sets: str) -> str:
    out = _render(*sets)
    assert out.returncode != 0, "the render should have been refused"
    assert "global.environment=prod refused" in out.stderr
    return out.stderr


def test_the_embedded_gateway_on_the_operators_postgresql_renders_in_prod() -> None:
    docs = _docs("global.gateway.embedded=true", "global.database.embedded=false")
    gateway = _one(docs, "Deployment", "c-litellm")["spec"]["template"]["spec"]["containers"][0]
    url = {e["name"]: e for e in gateway["env"]}["DATABASE_URL"]["value"]
    assert "@choregos-pg-rw.choregos-data.svc:5432/litellm" in url, "its database is the operator's"
    assert not [d for d in docs if d["metadata"]["name"] == "c-postgresql"], "no bench PostgreSQL"


def test_the_embedded_gateway_on_the_embedded_postgresql_is_refused_and_says_why() -> None:
    stderr = _refused("global.gateway.embedded=true", "global.database.embedded=true")
    assert "global.gateway.embedded (on global.database.embedded, the bench PostgreSQL)" in stderr


@pytest.mark.parametrize(
    ("sets", "claim"),
    [((), "c-ecphoria"), ((f"{E}.persistence.existingClaim=ecphoria-data",), "ecphoria-data")],
    ids=["its-own-claim", "an-adopted-claim"],
)
def test_the_embedded_memory_on_a_volume_renders_in_prod(sets: tuple[str, ...], claim: str) -> None:
    docs = _docs("global.memory.embedded=true", f"{E}.persistence.enabled=true", *sets)
    pod = _one(docs, "Deployment", "c-ecphoria")["spec"]["template"]["spec"]
    (data,) = [v for v in pod["volumes"] if v["name"] == "data"]
    assert data == {"name": "data", "persistentVolumeClaim": {"claimName": claim}}


def test_the_embedded_memory_without_persistence_is_refused_and_says_why() -> None:
    stderr = _refused("global.memory.embedded=true", f"{E}.persistence.enabled=false")
    assert "global.memory.embedded (tranibor-ecphoria.persistence.enabled=false" in stderr


@pytest.mark.parametrize(
    ("value", "named"),
    [
        ("global.devSecrets=true", "global.devSecrets"),
        ("global.devLogin.enabled=true", "global.devLogin.enabled"),
        ("demoFakes.enabled=true", "demoFakes.enabled"),
        ("global.fakes=true", "global.fakes"),
        ("global.temporal.embedded=true", "global.temporal.embedded (tranibor-temporal.mode=dev"),
        ("global.database.embedded=true", "global.database.embedded"),
    ],
)
def test_the_bench_values_stay_refused_beside_an_accepted_gateway_and_memory(value: str, named: str) -> None:
    accepted = ("global.gateway.embedded=true", "global.memory.embedded=true")
    assert _render(*accepted).returncode == 0
    assert named in _refused(*accepted, value)
