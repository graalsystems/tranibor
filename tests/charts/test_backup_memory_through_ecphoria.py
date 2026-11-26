"""The memory is backed up and restored by Ecphoria itself, while it runs (S80-41, #2185).

Proven on a k3d cluster before this: the backup's `files` step copied Ecphoria's DuckDB files while
it ran, without their WAL (and without `vectors/` or the lexical index's WAL). After the restore
Ecphoria logged `falling back to in-memory cognition store`, answered every search with nothing, and
nothing in the restore said so. The files were good only when Ecphoria was stopped first.

Ecphoria has its own backup (`POST /api/v1/admin/backup`): its stores exported (DuckDB `EXPORT
DATABASE`, its vector index, `VACUUM INTO` of its state) under `<data dir>/backups/<stamp>/`, with a
`manifest.json` of SHA-256 digests written last; and its own restore (`POST /api/v1/admin/restore`,
a directory it can read), which checks the digests and swaps each store in while it runs. The
chart's `memory` steps call them with the platform's admin key, and carry the directory between
Ecphoria's volume and the backup. The steps' scripts are run here against a scripted Ecphoria.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import pathlib
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

CHART = pathlib.Path(__file__).resolve().parents[2] / "charts" / "tranibor"
PROFILE = CHART / "values" / "single-node.yaml"
SITE = ("backup.target.s3.url=s3://backups/choregos", "backup.target.s3.endpoint=http://s3:8333")
TOKEN = "admin-key"

pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="helm absent")


def _docs(*sets: str) -> list[dict[str, Any]]:
    command = ["helm", "template", "c", str(CHART), "-n", "choregos", "-f", str(PROFILE)]
    for value in (*SITE, *sets):
        command += ["--set", value]
    out = subprocess.run(command, check=False, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    return [d for d in yaml.safe_load_all(out.stdout) if d]


def _one(docs: list[dict[str, Any]], kind: str, name: str) -> dict[str, Any]:
    (found,) = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    return found


def _steps(docs: list[dict[str, Any]], cronjob: str) -> dict[str, dict[str, Any]]:
    pod = _one(docs, "CronJob", cronjob)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return {c["name"]: c for c in pod["initContainers"]}


def _mounts(step: dict[str, Any]) -> dict[str, str]:
    return {m["name"]: m["mountPath"] for m in step.get("volumeMounts", [])}


@pytest.fixture(scope="module")
def docs() -> list[dict[str, Any]]:
    return _docs()


@pytest.fixture(scope="module")
def scripts(docs: list[dict[str, Any]]) -> dict[str, str]:
    return dict(_one(docs, "ConfigMap", "c-backup")["data"])


# ───────────────────────── what the chart renders ─────────────────────────


@pytest.mark.parametrize(
    ("cronjob", "script"), [("c-backup", "memory.py"), ("c-restore", "restore-memory.py")]
)
def test_the_memory_steps_speak_to_ecphoria_with_its_admin_key(
    docs: list[dict[str, Any]], cronjob: str, script: str
) -> None:
    step = _steps(docs, cronjob)["memory"]
    assert step["command"] == ["python", f"/scripts/{script}"]
    env = {e["name"]: e for e in step["env"]}
    assert env["MEMORY_URL"]["value"] == "http://c-ecphoria:8432"
    assert env["MEMORY_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "c-memory", "key": "token"}
    # The key the chart generates is an admin key of no tenant: `/admin/*` refuses any other role.
    secret = _one(docs, "Secret", "c-memory")["stringData"]
    assert secret["api-keys"] == f"{secret['token']}@:admin"
    assert _mounts(step)["memory"] == "/memory"


def test_no_step_copies_or_puts_back_ecphoria_s_files(
    scripts: dict[str, str], docs: list[dict[str, Any]]
) -> None:
    """The bug of #2185: the DuckDB files and their WAL, copied while Ecphoria ran."""
    for name in ("files.py", "restore-files.py"):
        assert "/memory" not in scripts[name], name
        assert ".wal" not in scripts[name] and "duckdb" not in scripts[name].lower(), name
    for cronjob in ("c-backup", "c-restore"):
        steps = _steps(docs, cronjob)
        assert "memory" not in _mounts(steps["files"]), cronjob
        users = [name for name, step in steps.items() if "memory" in _mounts(step)]
        assert users == ["memory"], cronjob
    assert "/api/v1/admin/backup" in scripts["memory.py"]
    assert "/api/v1/admin/restore" in scripts["restore-memory.py"]
    # The lexical index is derived: rebuilt after a restore, not carried.
    assert "/api/v1/admin/reindex" in scripts["restore-memory.py"]


def test_ecphoria_s_data_directory_is_the_volume_the_steps_mount(
    docs: list[dict[str, Any]], scripts: dict[str, str]
) -> None:
    """Ecphoria names its backup in its own paths; the steps find it on the same claim."""
    deployment = _one(docs, "Deployment", "c-ecphoria")["spec"]["template"]["spec"]
    (container,) = deployment["containers"]
    env = {e["name"]: e.get("value") for e in container["env"]}
    assert env["ECPHORIA_STORAGE__DATA_DIR"] == "/data"
    assert {m["name"]: m["mountPath"] for m in container["volumeMounts"]}["data"] == "/data"
    claim = next(v for v in deployment["volumes"] if v["name"] == "data")["persistentVolumeClaim"]
    for cronjob in ("c-backup", "c-restore"):
        pod = _one(docs, "CronJob", cronjob)["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        assert next(v for v in pod["volumes"] if v["name"] == "memory")["persistentVolumeClaim"] == claim
    assert 'DATA = pathlib.PurePosixPath("/data")' in scripts["ecphoria_admin.py"]
    assert 'VOLUME = pathlib.Path("/memory")' in scripts["ecphoria_admin.py"]


def test_the_backup_and_the_restore_may_reach_ecphoria(docs: list[dict[str, Any]]) -> None:
    """Egress is denied by default in the namespace: the steps' pods must be let out to port 8432."""
    platform = _one(docs, "NetworkPolicy", "tranibor-allow-platform")["spec"]
    (excluded,) = platform["podSelector"]["matchExpressions"]
    assert excluded["operator"] == "NotIn"
    for pod in ("choregos-backup", "choregos-restore"):
        assert pod not in excluded["values"]
    to_memory = [
        rule
        for rule in platform["egress"]
        if {"protocol": "TCP", "port": 8432} in rule.get("ports", [])
        and any(
            (peer.get("podSelector") or {}).get("matchLabels", {}).get("app.kubernetes.io/name")
            == "choregos-ecphoria"
            for peer in rule["to"]
        )
    ]
    assert to_memory, platform["egress"]
    # And no ingress policy of its own keeps them out of Ecphoria's pods (the namespace's
    # `tranibor-allow-ingress` admits every pod of the namespace).
    for policy in (d for d in docs if d["kind"] == "NetworkPolicy"):
        spec = policy["spec"]
        selected = spec["podSelector"].get("matchLabels", {}).get("app.kubernetes.io/name")
        if "Ingress" in spec.get("policyTypes", []) and selected == "choregos-ecphoria":
            pytest.fail(f"{policy['metadata']['name']} restricts Ecphoria's ingress: admit the backup")


def test_a_memory_without_a_volume_has_no_memory_step() -> None:
    """Nothing to carry: Ecphoria's backup would land in a directory no other pod can read."""
    docs = _docs("tranibor-ecphoria.persistence.enabled=false")
    for cronjob in ("c-backup", "c-restore"):
        assert "memory" not in _steps(docs, cronjob), cronjob


# ───────────────────────── the scripts, against a scripted Ecphoria ─────────────────────────


def _digest(path: pathlib.Path) -> str:
    """Ecphoria's `sha256_path`, written again from its source (`ecphoria-core`, engine/mod.rs)."""
    digest = hashlib.sha256()
    if path.is_dir():
        for file in sorted(f for f in path.rglob("*") if f.is_file()):
            digest.update(str(file.relative_to(path)).encode() + b"\0" + file.read_bytes())
    else:
        digest.update(path.read_bytes())
    return digest.hexdigest()


class Ecphoria:
    """Ecphoria's admin API as the steps use it, over a directory that stands for its volume."""

    def __init__(self, volume: pathlib.Path) -> None:
        self.volume = volume
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.restored: list[pathlib.Path] = []
        self.corrupt = False

    def backup(self) -> dict[str, Any]:
        stamp = "20261123T020000Z"
        target = self.volume / "backups" / stamp
        (target / "episodic_export").mkdir(parents=True)
        (target / "episodic_export" / "episodic.csv").write_text("e1,record\n")
        (target / "episodic_export" / "load.sql").write_text("COPY episodic FROM 'episodic.csv';\n")
        (target / "vectors" / "sub").mkdir(parents=True)
        (target / "vectors" / "index.usearch").write_bytes(b"\x01\x02")
        (target / "vectors" / "sub" / "meta.json").write_text("{}")
        (target / "state.db").write_bytes(b"SQLite format 3\0state")
        artifacts = [
            {"path": name, "sha256": _digest(target / name)}
            for name in ("episodic_export", "vectors", "state.db")
        ]
        manifest = {"format_version": 1, "counts": {"memories": 1}, "artifacts": artifacts}
        (target / "manifest.json").write_text(json.dumps(manifest))
        if self.corrupt:
            (target / "vectors" / "index.usearch").write_bytes(b"\x01\x03")
        return {"status": "ok", "path": f"/data/backups/{stamp}", "timestamp": stamp, "pruned": 0}

    def restore(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        directory = self.volume / pathlib.PurePosixPath(body["path"]).relative_to("/data")
        manifest = json.loads((directory / "manifest.json").read_text())
        for artifact in manifest["artifacts"]:
            if _digest(directory / artifact["path"]) != artifact["sha256"]:
                return 500, {"error": "RESTORE_ERROR"}
        self.restored.append(directory)
        return 200, {"status": "ok", "restored_from": body["path"]}

    def answer(self, path: str, body: dict[str, Any], token: str) -> tuple[int, dict[str, Any]]:
        self.calls.append((path, body, token))
        if token != f"Bearer {TOKEN}":
            return 401, {"error": "UNAUTHORIZED"}
        if path == "/api/v1/admin/backup":
            return 200, self.backup()
        if path == "/api/v1/admin/restore":
            return self.restore(body)
        if path == "/api/v1/admin/reindex":
            return 200, {"reindexed": 0, "pending": 0, "lexical_indexed": 1}
        return 404, {}


@pytest.fixture
def ecphoria(tmp_path: pathlib.Path) -> Iterator[tuple[Ecphoria, str]]:
    fake = Ecphoria(tmp_path / "memory")
    fake.volume.mkdir()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            status, body = fake.answer(
                self.path, json.loads(raw or b"{}"), self.headers.get("Authorization", "")
            )
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_: Any) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()


def _run(
    scripts: dict[str, str], tmp_path: pathlib.Path, name: str, url: str
) -> subprocess.CompletedProcess[str]:
    """A step's script as rendered, the pod's roots (`/backup`, `/restore`, `/memory`) moved under
    `tmp_path`; Ecphoria's own paths (`/data`) stay what Ecphoria answers."""
    folder = tmp_path / "scripts"
    folder.mkdir(exist_ok=True)
    for script in ("ecphoria_admin.py", name):
        text = scripts[script]
        for root in ("/backup", "/restore", "/memory"):
            text = text.replace(f'"{root}', f'"{tmp_path}{root}')
        (folder / script).write_text(text)
    env = {**os.environ, "MEMORY_URL": url, "MEMORY_TOKEN": TOKEN}
    return subprocess.run(
        [sys.executable, str(folder / name)], check=False, capture_output=True, text=True, env=env, timeout=60
    )


def test_the_backup_step_copies_ecphoria_s_own_backup_and_checks_it(
    scripts: dict[str, str], tmp_path: pathlib.Path, ecphoria: tuple[Ecphoria, str]
) -> None:
    fake, url = ecphoria
    (tmp_path / "backup" / "S1").mkdir(parents=True)
    (tmp_path / "backup" / ".current").write_text("S1\n")
    done = _run(scripts, tmp_path, "memory.py", url)
    assert done.returncode == 0, done.stderr
    assert [path for path, _body, _token in fake.calls] == ["/api/v1/admin/backup"]
    copy = tmp_path / "backup" / "S1" / "memory" / "ecphoria"
    assert json.loads((copy / "manifest.json").read_text())["counts"] == {"memories": 1}
    assert (copy / "vectors" / "sub" / "meta.json").is_file()
    # Copied, then removed from Ecphoria's volume: it would only take its room there.
    assert list((fake.volume / "backups").iterdir()) == []
    assert "3 artifacts" in done.stdout


def test_a_backup_that_does_not_match_its_manifest_fails_the_step(
    scripts: dict[str, str], tmp_path: pathlib.Path, ecphoria: tuple[Ecphoria, str]
) -> None:
    fake, url = ecphoria
    fake.corrupt = True
    (tmp_path / "backup" / "S1").mkdir(parents=True)
    (tmp_path / "backup" / ".current").write_text("S1\n")
    done = _run(scripts, tmp_path, "memory.py", url)
    assert done.returncode != 0
    assert "vectors does not match its manifest" in done.stderr


def test_a_refused_key_fails_the_step_with_what_ecphoria_said(
    scripts: dict[str, str], tmp_path: pathlib.Path, ecphoria: tuple[Ecphoria, str]
) -> None:
    _fake, url = ecphoria
    (tmp_path / "backup" / "S1").mkdir(parents=True)
    (tmp_path / "backup" / ".current").write_text("S1\n")
    admin = scripts["ecphoria_admin.py"].replace(
        'TOKEN = os.environ["MEMORY_TOKEN"]', 'TOKEN = "not-the-key"'
    )
    scripts = {**scripts, "ecphoria_admin.py": admin}
    done = _run(scripts, tmp_path, "memory.py", url)
    assert done.returncode != 0
    assert "/api/v1/admin/backup: Ecphoria answered 401" in done.stderr


def test_the_restore_step_stages_the_backup_on_ecphoria_s_volume_and_asks_it_to_restore(
    scripts: dict[str, str], tmp_path: pathlib.Path, ecphoria: tuple[Ecphoria, str]
) -> None:
    fake, url = ecphoria
    # A backup as the backup step left it.
    (tmp_path / "backup" / "S1").mkdir(parents=True)
    (tmp_path / "backup" / ".current").write_text("S1\n")
    assert _run(scripts, tmp_path, "memory.py", url).returncode == 0
    fake.calls.clear()
    (tmp_path / "restore").mkdir()
    (tmp_path / "restore" / ".dir").write_text(f"{tmp_path}/backup/S1\n")
    done = _run(scripts, tmp_path, "restore-memory.py", url)
    assert done.returncode == 0, done.stderr
    paths = [path for path, _body, _token in fake.calls]
    assert paths == ["/api/v1/admin/restore", "/api/v1/admin/reindex"]
    # Ecphoria was named the staged directory in its own paths, and found it whole.
    assert fake.calls[0][1] == {"path": "/data/restore/S1"}
    assert fake.restored == [fake.volume / "restore" / "S1"]
    # The staged copy is gone once Ecphoria has it.
    assert not (fake.volume / "restore" / "S1").exists()


def test_a_backup_holding_ecphoria_s_files_is_not_put_back_blindly(
    scripts: dict[str, str], tmp_path: pathlib.Path, ecphoria: tuple[Ecphoria, str]
) -> None:
    """A backup made before S80-41: its DuckDB files may not replay, and Ecphoria would run empty."""
    fake, url = ecphoria
    (tmp_path / "backup" / "S0" / "memory").mkdir(parents=True)
    (tmp_path / "backup" / "S0" / "memory" / "cognition.duckdb").write_bytes(b"DUCK")
    (tmp_path / "restore").mkdir()
    (tmp_path / "restore" / ".dir").write_text(f"{tmp_path}/backup/S0\n")
    done = _run(scripts, tmp_path, "restore-memory.py", url)
    assert done.returncode != 0
    assert "not Ecphoria's own backup" in done.stderr
    assert fake.calls == []
    assert not (fake.volume / "cognition.duckdb").exists()


def test_a_backup_without_a_memory_leaves_ecphoria_as_it_is(
    scripts: dict[str, str], tmp_path: pathlib.Path, ecphoria: tuple[Ecphoria, str]
) -> None:
    fake, url = ecphoria
    (tmp_path / "backup" / "S2").mkdir(parents=True)
    (tmp_path / "restore").mkdir()
    (tmp_path / "restore" / ".dir").write_text(f"{tmp_path}/backup/S2\n")
    done = _run(scripts, tmp_path, "restore-memory.py", url)
    assert done.returncode == 0, done.stderr
    assert fake.calls == []
