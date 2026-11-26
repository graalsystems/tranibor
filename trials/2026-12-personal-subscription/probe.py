# SPDX-License-Identifier: Apache-2.0
"""S87-07: measure Claude Code on a person's own subscription, through the runner's own ACP client.

Run by the subscription's owner, on their own machine, with the token they made themselves
(`claude setup-token`). The token is read from the environment, given only to the unmodified
`claude-agent-acp` child, and never written anywhere: the fixtures this writes hold texts, codes and
host names, and the script checks it before writing (ADR 0227).

What it measures, in a per-run HOME as the runner's `workspace.agent_home` makes one, with
`CLAUDE_CODE_QUIET_ENV` and a runner-owned `CLAUDE_CONFIG_DIR`:

1. a prompt answered on the subscription (`signed_in`);
2. the ACP error of the same prompt with no token (`signed_out`);
3. that an `ANTHROPIC_API_KEY` beside the token takes precedence (`precedence`), with a key that is
   deliberately invalid so that nothing is billed;
4. `session/load` of the first session in a new process (`load_session`);
5. every host the CLI opened a tunnel to, through a CONNECT proxy of this script (`hosts`).

A usage limit cannot be reached on purpose: its texts stay those read from the SDK's source.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from tranibor_runner.acp.client import AcpClient
from tranibor_runner.backends.claude_code import CLAUDE_CODE_QUIET_ENV

HERE = Path(__file__).resolve().parent
OUT = HERE.parents[1] / "packages" / "runner" / "tests" / "fixtures" / "subscription" / "measured.json"
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"  # noqa: S105 - a variable name, not a value
PROMPT = "Answer with the single word OK."


class ConnectRecorder:
    """A CONNECT proxy that records the host of every tunnel and relays bytes it cannot read (TLS)."""

    def __init__(self) -> None:
        self.hosts: list[str] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode("latin-1")
        target = head.split(" ")[1]
        host, _, port = target.rpartition(":")
        self.hosts.append(host)
        upstream_reader, upstream_writer = await asyncio.open_connection(host, int(port))
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            dst.close()

        await asyncio.gather(pipe(reader, upstream_writer), pipe(upstream_reader, writer))


def _environment(home: Path, proxy_port: int, *, token: str | None, api_key: str | None) -> dict[str, str]:
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude-runner"),
        "HTTPS_PROXY": f"http://127.0.0.1:{proxy_port}",
        **CLAUDE_CODE_QUIET_ENV,
    }
    if token:
        env[TOKEN_VARIABLE] = token
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


async def _turn(env: dict[str, str], cwd: Path, *, load: str | None = None) -> dict[str, Any]:
    client = AcpClient(["claude-agent-acp"], cwd=str(cwd), env=env)
    async with client:
        init = await client.initialize()
        out: dict[str, Any] = {"load_session": bool((init.get("agentCapabilities") or {}).get("loadSession"))}
        try:
            if load:
                await client.load_session(load, str(cwd))
            else:
                await client.new_session(str(cwd))
            outcome = await client.prompt(PROMPT, timeout=180.0)
            out.update(ok=True, session=client.session_id, stop_reason=str(outcome.stop_reason))
        except Exception as exc:  # the error IS the measurement
            out.update(ok=False, error=str(exc)[:500])
        return out


def _clean(value: Any, token: str) -> Any:
    text = json.dumps(value)
    if token and token in text:
        raise SystemExit("refused to write: a measurement holds the token")
    return value


async def main() -> int:
    token = os.environ.get(TOKEN_VARIABLE, "").strip()
    if not token:
        print(f"set {TOKEN_VARIABLE} to the token `claude setup-token` printed for you", file=sys.stderr)
        return 2
    # `AcpClient` starts the agent with this process's own environment beneath the one given
    # (`agent_base_environment`): whatever the owner's shell holds would take part in every measurement.
    for variable in (
        TOKEN_VARIABLE,
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "HTTPS_PROXY",
    ):
        os.environ.pop(variable, None)
    recorder = ConnectRecorder()
    port = await recorder.start()
    with tempfile.TemporaryDirectory(prefix="choregos-probe-") as tmp:
        home, cwd = Path(tmp) / "home", Path(tmp) / "work"
        home.mkdir()
        cwd.mkdir()
        signed_in = await _turn(_environment(home, port, token=token, api_key=None), cwd)
        reloaded = (
            await _turn(
                _environment(home, port, token=token, api_key=None), cwd, load=signed_in.get("session")
            )
            if signed_in.get("ok")
            else {"skipped": "no first session"}
        )
        signed_out = await _turn(_environment(Path(tmp) / "home2", port, token=None, api_key=None), cwd)
        precedence = await _turn(
            _environment(home, port, token=token, api_key="sk-ant-api03-" + "x" * 40), cwd
        )
    result = {
        "provenance": "measured",
        "signed_in": signed_in,
        "load_session": reloaded,
        "signed_out": signed_out,
        "precedence_with_an_invalid_api_key": precedence,
        "hosts": sorted(set(recorder.hosts)),
    }
    OUT.write_text(json.dumps(_clean(result, token), indent=2) + "\n", encoding="utf-8")
    print(f"written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
