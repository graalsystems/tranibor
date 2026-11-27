# SPDX-License-Identifier: Apache-2.0
"""The installation's relay agent, minimal: what the relay's tests need of the other side.

It pairs (a device code, signed with a key it made), holds the control session (a hello signed with
that key over the challenge's nonce), and for each order to dial back opens a new connection to the
relay's own host, presents the stream's ticket, writes a PROXY protocol v2 header carrying the phone's
address to its one target, and splices. It connects to two places only: the relay's own host, named
by `relay`, and its target, named by `target`. An order that names anything more (a host, a port) is
refused: `RelayOpenStream` is closed (ADR 0230 §3, `x-target`).

The full agent, its configuration, its certificate by TLS-ALPN-01 and `choregos local relay` are the
next story's (S87-43); this class is the part of it the relay is proven against.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import ssl
import time
from typing import Any, Final
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from tranibor_contracts.relay import (
    RELAY_PREFIX,
    RELAY_PROTOCOL,
    SIGNATURE_HEADER,
    STREAM_UPGRADE,
    TICKET_HEADER,
    RelayChallenge,
    RelayClose,
    RelayOpenStream,
    RelayPairingAnswer,
    RelayProblem,
    RelayWelcome,
    hello_signature_claims,
    hello_signature_header,
    request_signature_claims,
    request_signature_header,
)
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from . import __version__
from .metadata import library_logger
from .signing import encode_public, sign
from .wire import MAX_HEAD_BYTES, close_quietly, splice

#: The control session's WebSocket logs nothing either: its frames are the signed hello and the orders,
#: each with a phone's address (`library_logger`). Left to websockets' own logger, a process at DEBUG
#: would write them all.
_SILENT: Final = library_logger("tranibor_sni_relay.agent.websocket")

#: The PROXY protocol v2 signature (HAProxy's specification, §2.2).
PROXY_V2_SIGNATURE: Final = b"\r\n\r\n\x00\r\nQUIT\n"
_VERSION_COMMAND: Final = 0x21  # version 2, PROXY
_TCP4: Final = 0x11
_TCP6: Final = 0x21


class RelayRefusedError(Exception):
    """The relay answered a problem."""

    def __init__(self, problem: RelayProblem) -> None:
        super().__init__(f"{problem.status} {problem.code}")
        self.problem = problem


def proxy_v2_header(client_ip: str) -> bytes:
    """A PROXY protocol v2 header naming `client_ip` as the source, port 0, and no destination of
    note: the order names the phone's address, and nothing else."""
    source = ipaddress.ip_address(client_ip)
    if source.version == 4:
        family, addresses = _TCP4, source.packed + bytes(4)
    else:
        family, addresses = _TCP6, source.packed + bytes(16)
    body = addresses + bytes(4)  # source port, destination port
    return PROXY_V2_SIGNATURE + bytes([_VERSION_COMMAND, family]) + len(body).to_bytes(2, "big") + body


class RelayAgent:
    """One installation's side of the relay."""

    def __init__(
        self,
        *,
        relay: str,
        target: tuple[str, int],
        key: Ed25519PrivateKey | None = None,
        ssl_context: ssl.SSLContext | None = None,
        dial_back: bool = True,
    ) -> None:
        parts = urlsplit(relay)
        if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/"):
            raise ValueError("the relay is named by its own host's https:// origin")
        self.relay = relay.rstrip("/")
        self.target = target
        self.key = key if key is not None else Ed25519PrivateKey.generate()
        self._ssl = ssl_context if ssl_context is not None else ssl.create_default_context()
        self._host = parts.hostname
        self._port = parts.port or 443
        self._dial_back = dial_back
        self.welcomed = asyncio.Event()
        self.welcome: RelayWelcome | None = None
        #: The orders taken, and the ones refused (as they arrived).
        self.orders: list[RelayOpenStream] = []
        self.refused: list[str] = []
        #: The streams dialled back, and how many connections to the target were opened.
        self.dialled: list[str] = []
        self.targets_opened = 0
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def signing_key(self) -> str:
        return encode_public(self.key.public_key())

    @property
    def control_url(self) -> str:
        return "wss://" + self.relay.removeprefix("https://") + f"{RELAY_PREFIX}/control"

    # ───────────────────────── the pairing ─────────────────────────

    async def pair(self) -> RelayPairingAnswer:
        """Asks for a device code for this agent's key."""
        body = {"protocol": RELAY_PROTOCOL, "agent_version": __version__, "signing_key": self.signing_key}
        return await self._signed_post(f"{RELAY_PREFIX}/pairing", body)

    async def poll(self, device_code: str) -> RelayPairingAnswer:
        """Where the pairing of `device_code` stands."""
        return await self._signed_post(
            f"{RELAY_PREFIX}/pairing/poll", {"protocol": RELAY_PROTOCOL, "device_code": device_code}
        )

    async def _signed_post(self, path: str, document: dict[str, Any]) -> RelayPairingAnswer:
        url = self.relay + path
        body = json.dumps(document).encode()
        claims = request_signature_claims(method="POST", url=url, body=body, iat=int(time.time()))
        headers = {
            SIGNATURE_HEADER: sign(self.key, claims, request_signature_header()),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            verify=self._ssl, trust_env=False, follow_redirects=False, timeout=10
        ) as client:
            answered = await client.post(url, content=body, headers=headers)
        if answered.status_code >= 400:
            raise RelayRefusedError(RelayProblem.model_validate_json(answered.content))
        return RelayPairingAnswer.model_validate_json(answered.content)

    # ───────────────────────── the control session ─────────────────────────

    async def run(self, relay_id: str) -> RelayClose | None:
        """Holds the control session until the relay closes it; the `close` it sent, if any."""
        async with connect(
            self.control_url, ssl=self._ssl, proxy=None, open_timeout=10, logger=_SILENT
        ) as socket:
            challenge = RelayChallenge.model_validate_json(await socket.recv())
            claims = hello_signature_claims(
                url=self.control_url,
                relay_id=relay_id,
                protocol=RELAY_PROTOCOL,
                nonce=challenge.nonce,
                iat=int(time.time()),
            )
            hello = {
                "type": "hello",
                "protocol": RELAY_PROTOCOL,
                "agent_version": __version__,
                "id": relay_id,
                "signature": sign(self.key, claims, hello_signature_header(relay_id)),
            }
            await socket.send(json.dumps(hello))
            try:
                async for message in socket:
                    closed = self._read(message)
                    if closed is not None:
                        return closed
            except ConnectionClosed:
                pass
            finally:
                for task in list(self._tasks):  # this minimal agent's streams end with its session
                    task.cancel()
        return None

    def _read(self, message: str | bytes) -> RelayClose | None:
        try:
            kind = json.loads(message).get("type")
        except (ValueError, AttributeError):
            return None
        if kind == "welcome":
            self.welcome = RelayWelcome.model_validate_json(message)
            self.welcomed.set()
        elif kind == "close":
            return RelayClose.model_validate_json(message)
        elif kind == "open_stream":
            try:
                order = RelayOpenStream.model_validate_json(message)
            except ValidationError:
                self.refused.append(
                    message if isinstance(message, str) else message.decode("utf-8", "replace")
                )
                return None
            self.orders.append(order)
            if self._dial_back:
                task = asyncio.create_task(self._serve(order))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        return None

    # ───────────────────────── a dial-back ─────────────────────────

    async def _serve(self, order: RelayOpenStream) -> None:
        """Dials back for `order`, then splices it to the target behind a PROXY v2 header."""
        relay = await self._open_dial_back(order)
        if relay is None:
            return
        try:
            target = await asyncio.open_connection(*self.target)
        except OSError:
            await close_quietly(relay[1])
            return
        self.targets_opened += 1
        target[1].write(proxy_v2_header(str(order.client_ip)))
        await splice(relay, target)

    async def _open_dial_back(
        self, order: RelayOpenStream
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        self.dialled.append(order.stream)
        try:
            reader, writer = await asyncio.open_connection(
                self._host, self._port, ssl=self._ssl, server_hostname=self._host, limit=MAX_HEAD_BYTES
            )
        except OSError:
            return None
        writer.write(
            (
                f"GET {RELAY_PREFIX}/data/{order.stream} HTTP/1.1\r\nHost: {self._host}\r\n"
                f"Connection: Upgrade\r\nUpgrade: {STREAM_UPGRADE}\r\n{TICKET_HEADER}: {order.ticket}\r\n\r\n"
            ).encode()
        )
        try:
            async with asyncio.timeout(10):
                head = await reader.readuntil(b"\r\n\r\n")
        except (TimeoutError, asyncio.LimitOverrunError, asyncio.IncompleteReadError, OSError):
            await close_quietly(writer)
            return None
        if not head.startswith(b"HTTP/1.1 101 "):
            with contextlib.suppress(OSError):
                await close_quietly(writer)
            return None
        return reader, writer
