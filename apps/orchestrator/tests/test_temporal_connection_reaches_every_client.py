"""TLS and the payload codec reach every Temporal client the platform builds (S80-06, ADR 0183).

The worker, the orchestrator's activities that start and signal workflows, and the API's client all
connect through `tranibor_core.temporal_connection.connect_temporal`. Each is driven here with the
deployment's variables set, and the arguments `Client.connect` receives are checked: the CA, the
client certificate and key, the server name, and a converter that carries the codec. A process
connected another way would talk in clear, or write payloads the others cannot read.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from temporalio.client import Client
from temporalio.service import TLSConfig
from tranibor_core.temporal_connection import AesGcmCodec


class _Stop(Exception):  # noqa: N818 - the test's stop signal
    pass


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """The variables the chart sets, and a `Client.connect` that records what it is given."""
    for name, content in {"ca.crt": b"CA", "tls.crt": b"CERT", "tls.key": b"KEY"}.items():
        (tmp_path / name).write_bytes(content)
    (tmp_path / "keys").write_text(f"k2:{base64.b64encode(os.urandom(32)).decode()}")
    monkeypatch.setenv("TRANIBOR_TEMPORAL_TLS", "true")
    monkeypatch.setenv("TRANIBOR_TEMPORAL_TLS_CA_FILE", str(tmp_path / "ca.crt"))
    monkeypatch.setenv("TRANIBOR_TEMPORAL_TLS_CERT_FILE", str(tmp_path / "tls.crt"))
    monkeypatch.setenv("TRANIBOR_TEMPORAL_TLS_KEY_FILE", str(tmp_path / "tls.key"))
    monkeypatch.setenv("TRANIBOR_TEMPORAL_TLS_SERVER_NAME", "frontend.temporal")
    monkeypatch.setenv("TRANIBOR_TEMPORAL_CODEC_KEYS_FILE", str(tmp_path / "keys"))
    monkeypatch.setenv("TRANIBOR_FAKES", "0")
    from tranibor_orchestrator.config import get_settings

    get_settings.cache_clear()
    calls: list[dict[str, Any]] = []

    async def connect(target: str, **options: Any) -> Any:
        calls.append({"target": target, **options})
        raise _Stop

    monkeypatch.setattr(Client, "connect", connect)
    monkeypatch.setattr("tranibor_adapters.load_extensions", list)
    yield calls
    get_settings.cache_clear()


async def _worker() -> None:
    from tranibor_orchestrator import worker

    await worker.run_worker(["orchestrator"])


async def _signal() -> None:
    from tranibor_orchestrator.train_client import signal_workflow

    await signal_workflow("wf-1", "abort", {})


async def _start_once() -> None:
    from tranibor_orchestrator.train_client import start_workflow_once

    await start_workflow_once("ReleaseTrain", "wf-1", {})


async def _restart() -> None:
    from tranibor_orchestrator.train_client import restart_interpreter

    await restart_interpreter("wf-1", {})


async def _api() -> None:
    from tranibor_api.temporal import RealTemporal

    await RealTemporal("temporal:7233", "default", "orchestrator").client()


@pytest.mark.parametrize(
    "connects",
    [_worker, _signal, _start_once, _restart, _api],
    ids=["worker", "signal", "start-once", "restart", "api"],
)
async def test_the_tls_settings_and_the_codec_reach_the_connect_call(
    deployment: list[dict[str, Any]], connects: Callable[[], Awaitable[None]]
) -> None:
    with pytest.raises(_Stop):
        await connects()
    (call,) = deployment
    assert call["tls"] == TLSConfig(
        server_root_ca_cert=b"CA", client_cert=b"CERT", client_private_key=b"KEY", domain="frontend.temporal"
    )
    codec = call["data_converter"].payload_codec
    assert isinstance(codec, AesGcmCodec) and codec.key_id == "k2"


async def test_the_worker_installs_the_organisations_data_keys_for_every_client(
    deployment: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0224: with a key wrapper (here the `TRANIBOR_SECRETS_KEKS` keyring), the worker's client and
    every client its activities connect afterwards seal an organisation's payloads under its data key."""
    from tranibor_core.temporal_connection import install_data_keys

    monkeypatch.setenv("TRANIBOR_SECRETS_KEKS", f"s1:{base64.b64encode(os.urandom(32)).decode()}")
    try:
        with pytest.raises(_Stop):
            await _worker()
        with pytest.raises(_Stop):
            await _signal()
        worker_codec, activity_codec = (call["data_converter"].payload_codec for call in deployment)
        assert isinstance(worker_codec, AesGcmCodec) and worker_codec.data_keys is not None
        assert isinstance(activity_codec, AesGcmCodec)
        assert activity_codec.data_keys is worker_codec.data_keys, "one cache per process"
    finally:
        install_data_keys(None)


async def test_without_a_key_wrapper_the_worker_keeps_the_derived_keys(
    deployment: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TRANIBOR_SECRETS_KEKS", raising=False)
    with pytest.raises(_Stop):
        await _worker()
    (call,) = deployment
    codec = call["data_converter"].payload_codec
    assert isinstance(codec, AesGcmCodec) and codec.data_keys is None
