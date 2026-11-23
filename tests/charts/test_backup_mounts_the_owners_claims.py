"""The backup and the restore mount the claims the embedded Temporal and memory write (S80-27, #2153).

Both owners take an adopted volume (`persistence.existingClaim`, S80-16) and Temporal's claim follows
its Service name (`global.temporal.serviceName`). The backup used to mount `<release>-temporal` and
`<release>-ecphoria` whatever the values said: after an adoption it waited on a claim that does not
exist, or copied a stale one. The claim names now come from one helper per owner
(`choregos.temporalClaim`, `choregos.memoryClaim`) that the owner's template and the backup both call.

The memory's DuckDB files have a single writer, Ecphoria: one replica, recreated rather than rolled,
and the backup copies the files without opening them. The SQLite copies need a read-write mount (the
online backup takes its shared lock through `-shm`, ADR 0184), so the mount is not read-only.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "choregos"
PROFILE = CHART / "values" / "single-node.yaml"
SITE = ("backup.target.s3.url=s3://backups/choregos", "backup.target.s3.endpoint=http://s3:8333")
JOBS = ("c-backup", "c-restore")

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _docs(*sets: str) -> list[dict[str, Any]]:
    command = ["helm", "template", "c", str(CHART), "-n", "choregos", "-f", str(PROFILE)]
    for value in (*SITE, *sets):
        command += ["--set", value]
    out = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} rendered"
    return found[0]


def _claims(pod: dict[str, Any]) -> dict[str, str]:
    return {
        v["name"]: v["persistentVolumeClaim"]["claimName"]
        for v in pod["volumes"]
        if "persistentVolumeClaim" in v
    }


def _job_pod(cronjob: dict[str, Any]) -> dict[str, Any]:
    pod: dict[str, Any] = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return pod


def _owner_claim(docs: list[dict[str, Any]], component: str) -> str:
    (owner,) = [
        d
        for d in docs
        if d["kind"] == "Deployment"
        and d["spec"]["template"]["metadata"]["labels"].get("app.kubernetes.io/name") == component
    ]
    return _claims(owner["spec"]["template"]["spec"])["data"]


CASES = {
    "defaults": ((), "c-temporal", "c-ecphoria"),
    "adopted memory claim": (
        ("choregos-ecphoria.persistence.existingClaim=ecphoria-data",),
        "c-temporal",
        "ecphoria-data",
    ),
    "adopted temporal claim": (
        ("choregos-temporal.persistence.existingClaim=temporal-data",),
        "temporal-data",
        "c-ecphoria",
    ),
    "temporal service name kept": (
        ("global.temporal.serviceName=temporal",),
        "temporal",
        "c-ecphoria",
    ),
    "memory service name kept": (
        ("global.memory.serviceName=ecphoria",),
        "c-temporal",
        "c-ecphoria",
    ),
}


@pytest.mark.parametrize(("sets", "temporal", "memory"), CASES.values(), ids=CASES.keys())
def test_the_backup_and_the_restore_mount_the_claims_the_owners_mount(
    sets: tuple[str, ...], temporal: str, memory: str
) -> None:
    docs = _docs(*sets)
    assert _owner_claim(docs, "choregos-temporal") == temporal
    assert _owner_claim(docs, "choregos-ecphoria") == memory
    rendered = {d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim"}
    adopted = {s.split("=", 1)[1] for s in sets if ".persistence.existingClaim=" in s}
    for job in JOBS:
        claims = _claims(_job_pod(_one(docs, "CronJob", job)))
        assert claims["temporal"] == temporal, job
        assert claims["memory"] == memory, job
        # Every claim a job waits on is one the chart renders or one the operator said exists.
        assert {temporal, memory} <= rendered | adopted, job
    assert not adopted & rendered, "a claim is rendered over an adopted volume"


def test_the_memory_has_one_writer_and_the_backup_never_opens_its_duckdb_files() -> None:
    docs = _docs("choregos-ecphoria.persistence.existingClaim=ecphoria-data")
    (ecphoria,) = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "c-ecphoria"]
    assert ecphoria["spec"]["replicas"] == 1
    # Never an old and a new Ecphoria pod on the volume at once.
    assert ecphoria["spec"]["strategy"] == {"type": "Recreate"}
    for job in JOBS:
        cronjob = _one(docs, "CronJob", job)
        # Never two backup (or restore) pods on the volume at once.
        assert cronjob["spec"]["concurrencyPolicy"] == "Forbid", job
    files = _one(docs, "ConfigMap", "c-backup")["data"]["files.py"]
    # The DuckDB files are copied as files (ADR 0184); opening them would make the backup a writer.
    assert not re.search(r"\bduckdb\b", files), "the backup opens a DuckDB file"
    assert "shutil.copy2(path, target)" in files
    backup = _job_pod(_one(docs, "CronJob", "c-backup"))
    (step,) = [c for c in backup["initContainers"] if c["name"] == "files"]
    (mount,) = [m for m in step["volumeMounts"] if m["name"] == "memory"]
    # Read-write by design: `state.db`'s online SQLite copy takes its shared lock through `-shm`.
    assert mount["mountPath"] == "/memory"
    assert not mount.get("readOnly", False)
