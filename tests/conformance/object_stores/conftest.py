"""The real servers the object store conformance suite runs against (S87-17), in Docker.

Opt-in, like the other Docker suites (`tests/conformance/hosted_real`): the general CI job has a
daemon too and must not pull these images. `TRANIBOR_TEST_OBJECT_STORE_DOCKER=1` asks for them; it
is then an error, not a skip, when Docker cannot run them, so a job that asks cannot pass by skipping.
Without it, the Docker tests skip and the emulators' run (always on) remains.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from collections.abc import Iterator

import httpx
import pytest

ASKED = os.environ.get("TRANIBOR_TEST_OBJECT_STORE_DOCKER") == "1"
AZURITE = "mcr.microsoft.com/azure-storage/azurite:3.35.0"
FAKE_GCS = "fsouza/fake-gcs-server:1.52.2"
#: The S3 server of the local install (`compose.yaml`), for the session token's proof.
RUSTFS = "rustfs/rustfs:1.0.1"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, check=False
    )
    return probe.returncode == 0


def _unavailable(reason: str) -> None:
    if ASKED:
        pytest.fail(f"TRANIBOR_TEST_OBJECT_STORE_DOCKER=1, but {reason}")
    pytest.skip(f"{reason} (set TRANIBOR_TEST_OBJECT_STORE_DOCKER=1 to run the real servers)")


def _wait(url: str, deadline: float = 90.0) -> None:
    start = time.monotonic()
    while time.monotonic() - start < deadline:
        try:
            httpx.get(url, timeout=2.0)
            return
        except httpx.HTTPError:
            time.sleep(0.5)
    raise TimeoutError(f"{url} did not answer within {deadline} s")


def run_container(image: str, port: int, *arguments: str, env: dict[str, str] | None = None) -> Iterator[str]:
    """Starts `image`, publishes `port` on the loopback, yields `http://127.0.0.1:<port>`, removes it."""
    if not ASKED:
        _unavailable("the real servers were not asked for")
    if not _docker_ready():
        _unavailable("no reachable Docker daemon")
    flags = [item for name, value in (env or {}).items() for item in ("-e", f"{name}={value}")]
    started = subprocess.run(
        ["docker", "run", "-d", "--rm", "-p", f"127.0.0.1::{port}", *flags, image, *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        _unavailable(f"`docker run {image}` failed: {started.stderr.strip()[:300]}")
    container = started.stdout.strip()
    try:
        published = subprocess.run(
            ["docker", "port", container, str(port)], capture_output=True, text=True, check=True
        ).stdout.splitlines()[0]
        base = f"http://127.0.0.1:{published.rsplit(':', 1)[1]}"
        _wait(base)
        yield base
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)


@pytest.fixture(scope="module")
def azurite() -> Iterator[str]:
    yield from run_container(AZURITE, 10000, "azurite-blob", "--blobHost", "0.0.0.0", "--loose")


@pytest.fixture(scope="module")
def fake_gcs() -> Iterator[str]:
    yield from run_container(FAKE_GCS, 4443, "-scheme", "http", "-port", "4443")


@pytest.fixture(scope="module")
def rustfs() -> Iterator[str]:
    yield from run_container(
        RUSTFS,
        9000,
        "/data",
        env={"RUSTFS_ACCESS_KEY": "conformance", "RUSTFS_SECRET_KEY": "conformance-secret"},
    )
