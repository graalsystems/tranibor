# SPDX-License-Identifier: Apache-2.0
"""`choregos-relay`: the command the relay's image runs (S87-44, ADR 0230 §12).

Its configuration is the environment the chart writes (`TRANIBOR_RELAY_*`); it refuses what the relay
would refuse before opening anything; `serve` runs the relay until SIGTERM and writes what it serves,
once, and nothing about a connection.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from tranibor_sni_relay import server
from tranibor_sni_relay.passthrough import Relay, RelayConfigError

from ._kit import TestCA as CertificateAuthority
from ._kit import closed_without_a_byte, raw_phone

BASE = {"TRANIBOR_RELAY_DOMAIN": "relay.example.com", "TRANIBOR_RELAY_RETENTION_DAYS": "30"}


def _env(tmp_path: Path, **more: str) -> dict[str, str]:
    return {
        **BASE,
        "TRANIBOR_RELAY_METADATA_PATH": str(tmp_path / "metadata.jsonl"),
        "TRANIBOR_RELAY_SNI_PORT": "0",
        "TRANIBOR_RELAY_OWN_PORT": "0",
        **{f"TRANIBOR_RELAY_{name}": value for name, value in more.items()},
    }


# ───────────────────────── the configuration ─────────────────────────


def test_the_environment_the_chart_writes_is_the_relays_configuration(tmp_path: Path) -> None:
    config = server.relay_config(
        _env(
            tmp_path,
            OWN_HOST="relay-own.example.com",
            SNI_PORT="9443",
            OWN_PORT="8443",
            OWN_PUBLIC_PORT="443",
            VERIFICATION_URI="https://accounts.example.com/pair",
            IDLE_TIMEOUT_S="120",
            LIMIT_CONCURRENT_STREAMS="7",
            LIMIT_BYTES_PER_MONTH="107374182400",
        )
    )
    assert (config.domain, config.own_host) == ("relay.example.com", "relay-own.example.com")
    assert config.retention == timedelta(days=30)
    assert config.metadata_path == tmp_path / "metadata.jsonl"
    assert (config.sni_port, config.own_port, config.own_public_port) == (9443, 8443, 443)
    assert config.verification_uri == "https://accounts.example.com/pair"
    assert config.idle_timeout_s == 120.0
    assert config.limits.concurrent_streams == 7 and config.limits.bytes_per_month == 100 * 2**30
    # What is not set is the relay's own default.
    assert config.limits.streams_per_minute == 240 and config.own_tls is None


def test_the_defaults_are_the_relays_and_the_metadata_lines_have_a_place() -> None:
    config = server.relay_config(BASE)
    assert (config.sni_port, config.own_port, config.own_public_port) == (443, 8443, None)
    assert config.metadata_path == server.DEFAULT_METADATA_PATH
    assert config.own_host is None and config.verification_uri is None


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"TRANIBOR_RELAY_DOMAIN": ""}, "TRANIBOR_RELAY_DOMAIN"),
        ({"TRANIBOR_RELAY_DOMAIN": "Relay.Example.com"}, "lower case"),
        ({"TRANIBOR_RELAY_RETENTION_DAYS": ""}, "TRANIBOR_RELAY_RETENTION_DAYS"),
        ({"TRANIBOR_RELAY_RETENTION_DAYS": "0"}, "TRANIBOR_RELAY_RETENTION_DAYS"),
        ({"TRANIBOR_RELAY_RETENTION_DAYS": "thirty"}, "TRANIBOR_RELAY_RETENTION_DAYS"),
        ({"TRANIBOR_RELAY_SNI_PORT": "70000"}, "TRANIBOR_RELAY_SNI_PORT"),
        ({"TRANIBOR_RELAY_OWN_PUBLIC_PORT": "0"}, "TRANIBOR_RELAY_OWN_PUBLIC_PORT"),
        ({"TRANIBOR_RELAY_LIMIT_STREAMS_PER_MINUTE": "0"}, "TRANIBOR_RELAY_LIMIT_STREAMS_PER_MINUTE"),
        ({"TRANIBOR_RELAY_IDLE_TIMEOUT_S": "-1"}, "TRANIBOR_RELAY_IDLE_TIMEOUT_S"),
        ({"TRANIBOR_RELAY_IDLE_TIMEOUT_S": "soon"}, "TRANIBOR_RELAY_IDLE_TIMEOUT_S"),
        ({"TRANIBOR_RELAY_VERIFICATION_URI": "http://x.example/pair"}, "TRANIBOR_RELAY_VERIFICATION_URI"),
        ({"TRANIBOR_RELAY_OWN_TLS_CERT_FILE": "/nowhere.crt"}, "OWN_TLS_KEY_FILE"),
        (
            {"TRANIBOR_RELAY_OWN_TLS_CERT_FILE": "/nowhere.crt", "TRANIBOR_RELAY_OWN_TLS_KEY_FILE": "/k"},
            "do not load",
        ),
    ],
)
def test_a_configuration_the_relay_would_not_start_on_is_refused(changes: dict[str, str], named: str) -> None:
    with pytest.raises(RelayConfigError, match=re.escape(named)):
        server.relay_config({**BASE, **changes})


def test_the_own_host_may_hold_its_own_certificate(tmp_path: Path) -> None:
    issued = CertificateAuthority().issue(dns=("relay.example.com",))
    (tmp_path / "tls.crt").write_bytes(issued.cert_pem)
    (tmp_path / "tls.key").write_bytes(issued.key_pem)
    config = server.relay_config(
        {
            **BASE,
            "TRANIBOR_RELAY_OWN_TLS_CERT_FILE": str(tmp_path / "tls.crt"),
            "TRANIBOR_RELAY_OWN_TLS_KEY_FILE": str(tmp_path / "tls.key"),
        }
    )
    assert config.own_tls is not None


def test_check_says_what_the_relay_would_serve_and_refuses_with_code_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert server.main(["check"], _env(tmp_path, SNI_PORT="9443", OWN_PUBLIC_PORT="443")) == 0
    said = capsys.readouterr().err
    assert "would serve <id>.relay.example.com on the SNI listener :9443, without TLS" in said
    assert "https://relay.example.com " in said and "kept 30 days" in said
    assert server.main(["check"], {"TRANIBOR_RELAY_DOMAIN": "relay.example.com"}) == server.CANNOT_START
    assert "refused: TRANIBOR_RELAY_RETENTION_DAYS is required" in capsys.readouterr().err
    with pytest.raises(SystemExit) as version:
        server.main(["--version"])
    assert version.value.code == 0 and "choregos-relay" in capsys.readouterr().out


# ───────────────────────── serve ─────────────────────────


async def test_serve_runs_the_relay_and_writes_nothing_about_a_connection(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stop = asyncio.Event()
    seen: list[bool] = []

    async def phone_calls(relay: Relay) -> None:
        # A connection that is no ClientHello: closed, and its one metadata line written.
        reader, writer = await raw_phone(relay.sni_port, b"GET / HTTP/1.1\r\n\r\n")
        seen.append(await closed_without_a_byte(reader))
        writer.close()
        await relay.settle()
        stop.set()

    await asyncio.wait_for(
        server.serve(server.relay_config(_env(tmp_path)), stop=stop, started=phone_calls), timeout=15
    )
    assert seen == [True]
    (line,) = [json.loads(text) for text in (tmp_path / "metadata.jsonl").read_text().splitlines()]
    assert line["close_reason"] == "malformed_client_hello" and line["client_ip"] == "127.0.0.1"
    said = capsys.readouterr().err
    assert said.startswith("choregos-relay: serving <id>.relay.example.com") and "stopped" in said
    assert "127.0.0.1" not in said, "the command wrote a connection's address"


def test_an_error_the_loop_catches_is_written_without_its_connection(
    capsys: pytest.CaptureFixture[str],
) -> None:
    loop = asyncio.new_event_loop()
    try:
        server._quiet_loop_errors(
            loop, {"message": "from 203.0.113.9:51234", "exception": ConnectionResetError("203.0.113.9")}
        )
    finally:
        loop.close()
    said = capsys.readouterr().err
    assert "ConnectionResetError" in said and "203.0.113.9" not in said


def test_the_command_stops_on_sigterm_and_refuses_a_port_it_cannot_open(tmp_path: Path) -> None:
    environ = {**os.environ, **_env(tmp_path)}
    process = subprocess.Popen(
        [sys.executable, "-m", "tranibor_sni_relay", "serve"],
        env=environ,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stderr is not None
        started = process.stderr.readline()
        assert started.startswith("choregos-relay: serving"), started
        port = re.search(r"SNI listener :(\d+)", started)
        assert port is not None
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=15) == 0
        assert "choregos-relay: stopped" in process.stderr.read()
    finally:
        if process.poll() is None:
            process.kill()
    # A port another process holds: the command says so and exits 2.
    import socket

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = str(taken.getsockname()[1])
        refused = subprocess.run(
            [sys.executable, "-m", "tranibor_sni_relay", "serve"],
            env={**environ, "TRANIBOR_RELAY_SNI_PORT": busy},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    assert refused.returncode == server.CANNOT_START
    assert "choregos-relay: could not start" in refused.stderr


def test_a_relay_that_could_not_keep_its_lines_does_not_start(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "a-file").write_text("not a directory")
    environ = _env(tmp_path, METADATA_PATH=str(tmp_path / "a-file" / "metadata.jsonl"))
    assert server.main(["serve"], environ) == server.CANNOT_START
    assert "choregos-relay: could not start" in capsys.readouterr().err
