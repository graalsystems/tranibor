"""The backup and the restore mount the claims the embedded Temporal and memory write (S80-27, #2153).

Both owners take an adopted volume (`persistence.existingClaim`, S80-16) and Temporal's claim follows
its Service name (`global.temporal.serviceName`). The backup used to mount `<release>-temporal` and
`<release>-ecphoria` whatever the values said: after an adoption it waited on a claim that does not
exist, or copied a stale one. The claim names now come from one helper per owner
(`choregos.temporalClaim`, `choregos.memoryClaim`) that the owner's template and the backup both call.

The memory's DuckDB files have a single writer, Ecphoria: one replica, recreated rather than rolled,
and neither the backup nor the restore opens or copies them: Ecphoria backs its stores up and restores
them itself (S80-41, #2185). Temporal's SQLite copy needs a read-write mount (the online backup takes
its shared lock through `-shm`, ADR 0184), so that mount is not read-only.

Temporal has a claim only in `dev` mode (SQLite). In `server` mode (ADR 0195) its state is two
databases on `global.database`, the chart renders no claim, and the backup used to wait on one that
did not exist (#2156): it now mounts none, and the `database` step's `pg_dump` covers Temporal when
it is on the embedded PostgreSQL.
"""

from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
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
        ("tranibor-ecphoria.persistence.existingClaim=ecphoria-data",),
        "c-temporal",
        "ecphoria-data",
    ),
    "adopted temporal claim": (
        ("tranibor-temporal.persistence.existingClaim=temporal-data",),
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
    docs = _docs("tranibor-ecphoria.persistence.existingClaim=ecphoria-data")
    (ecphoria,) = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "c-ecphoria"]
    assert ecphoria["spec"]["replicas"] == 1
    # Never an old and a new Ecphoria pod on the volume at once.
    assert ecphoria["spec"]["strategy"] == {"type": "Recreate"}
    for job in JOBS:
        cronjob = _one(docs, "CronJob", job)
        # Never two backup (or restore) pods on the volume at once.
        assert cronjob["spec"]["concurrencyPolicy"] == "Forbid", job
    scripts = _one(docs, "ConfigMap", "c-backup")["data"]
    # Ecphoria backs its stores up itself, while it runs (S80-41, #2185): no script opens a DuckDB
    # file, and none copies one (`tests/charts/test_backup_memory_through_ecphoria.py`).
    for name in ("files.py", "memory.py", "restore-files.py", "restore-memory.py"):
        assert not re.search(r"\bduckdb\b", scripts[name], re.IGNORECASE), name


def _temporal_mounts(pod: dict[str, Any]) -> list[str]:
    containers = [*pod.get("initContainers", []), *pod["containers"]]
    return [c["name"] for c in containers for m in c.get("volumeMounts", []) if m["name"] == "temporal"]


def test_only_a_dev_mode_temporal_has_a_claim_the_backup_mounts() -> None:
    """`dev` keeps Temporal in a SQLite file on its claim; `server` keeps it in PostgreSQL (#2156)."""
    dev = _docs("tranibor-temporal.mode=dev")
    server = _docs("tranibor-temporal.mode=server")
    for job in JOBS:
        dev_pod = _job_pod(_one(dev, "CronJob", job))
        assert _claims(dev_pod)["temporal"] == "c-temporal", job
        assert _temporal_mounts(dev_pod) == ["files"], job
        server_pod = _job_pod(_one(server, "CronJob", job))
        assert "temporal" not in _claims(server_pod), job
        assert "temporal" not in {v["name"] for v in server_pod["volumes"]}, job
        assert _temporal_mounts(server_pod) == [], job
        # The memory is still backed up, by its own step (Ecphoria's backup, S80-41).
        assert _claims(server_pod)["memory"] == "c-ecphoria", job


def test_in_server_mode_the_backup_pod_spec_holds_and_the_database_step_dumps_temporal() -> None:
    docs = _docs("tranibor-temporal.mode=server")
    rendered = {d["metadata"]["name"] for d in docs if d["kind"] == "PersistentVolumeClaim"}
    assert "c-temporal" not in rendered
    for job in JOBS:
        pod = _job_pod(_one(docs, "CronJob", job))
        names = [v["name"] for v in pod["volumes"]]
        assert len(names) == len(set(names)), job
        # Every claim the pod waits on exists: otherwise it stays Pending for ever.
        assert set(_claims(pod).values()) <= rendered, job
        for container in [*pod["initContainers"], *pod["containers"]]:
            for mount in container.get("volumeMounts", []):
                assert mount["name"] in names, f"{job}/{container['name']}: {mount['name']}"
    # Temporal's state is on the embedded PostgreSQL, whose every database the backup dumps.
    (temporal,) = [d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "c-temporal"]
    env = {e["name"]: e.get("value") for e in temporal["spec"]["template"]["spec"]["containers"][0]["env"]}
    backup = _job_pod(_one(docs, "CronJob", "c-backup"))
    (database,) = [c for c in backup["initContainers"] if c["name"] == "database"]
    pg = {e["name"]: e.get("value") for e in database["env"]}
    assert env["POSTGRES_SEEDS"] == pg["PGHOST"] == "c-postgresql"
    dump = _one(docs, "ConfigMap", "c-backup")["data"]["database.sh"]
    assert "FROM pg_database WHERE NOT datistemplate AND datname <> 'postgres'" in dump


def test_a_server_mode_temporal_alone_is_nothing_for_the_backup_to_back_up() -> None:
    command = ["helm", "template", "c", str(CHART), "-n", "choregos", "-f", str(PROFILE)]
    for value in (
        *SITE,
        "tranibor-temporal.mode=server",
        "global.database.embedded=false",
        "global.memory.embedded=false",
    ):
        command += ["--set", value]
    out = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    assert out.returncode != 0
    assert "backup.enabled=true with nothing to back up" in out.stderr
