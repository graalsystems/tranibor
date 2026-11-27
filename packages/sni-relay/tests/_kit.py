# SPDX-License-Identifier: Apache-2.0
"""The relay's bench: a test CA, the installation's TLS server behind its edge's PROXY listener, an
in-process relay, a phone, and the raw clients a test needs to misbehave on purpose.

Nothing here reaches the network: every listener is on 127.0.0.1, on a port the system picks.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import ipaddress
import json
import ssl
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID
from tranibor_contracts.relay import (
    PAIRING_INTERVAL_S,
    RELAY_PROTOCOL,
    hello_signature_claims,
    hello_signature_header,
)
from tranibor_sni_relay.agent import RelayAgent
from tranibor_sni_relay.passthrough import Relay, RelayConfig, RelayLimits
from websockets.asyncio.client import ClientConnection, connect

DOMAIN = "relay.test"
OWN_HOST = "127.0.0.1"
OWNER = "owner@example.test"
#: What the phone sends inside its TLS session: no byte of it may reach the relay's log.
SECRET = b"a passkey assertion only the installation may read"
_PROXY_V2_SIGNATURE = b"\r\n\r\n\x00\r\nQUIT\n"


class Clock:
    """The relay's wall clock: the real one, moved by a test when it must skip a wait."""

    def __init__(self) -> None:
        self.offset = 0.0

    def __call__(self) -> float:
        return time.time() + self.offset

    def advance(self, seconds: float) -> None:
        self.offset += seconds


# ───────────────────────── certificates ─────────────────────────


@dataclass(frozen=True)
class Issued:
    cert_pem: bytes
    key_pem: bytes
    der: bytes


class TestCA:
    """A certificate authority of the test only: the phone trusts it, nothing else does."""

    def __init__(self) -> None:
        self._key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "choregos relay test CA")])
        now = dt.datetime.now(dt.UTC)
        self._name = name
        self.cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self._key, hashes.SHA256())
        )
        self.pem = self.cert.public_bytes(serialization.Encoding.PEM)

    def issue(self, *, dns: tuple[str, ...] = (), ips: tuple[str, ...] = ()) -> Issued:
        key = ec.generate_private_key(ec.SECP256R1())
        names: list[x509.GeneralName] = [x509.DNSName(name) for name in dns]
        names += [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
        now = dt.datetime.now(dt.UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (dns or ips)[0])]))
            .issuer_name(self._name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(names), critical=False)
            .sign(self._key, hashes.SHA256())
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        return Issued(
            cert.public_bytes(serialization.Encoding.PEM),
            key_pem,
            cert.public_bytes(serialization.Encoding.DER),
        )

    def client_context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cadata=self.pem.decode())


def server_context(issued: Issued, folder: Path, name: str) -> ssl.SSLContext:
    cert, key = folder / f"{name}.crt", folder / f"{name}.key"
    cert.write_bytes(issued.cert_pem)
    key.write_bytes(issued.key_pem)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(cert, key)
    return context


# ───────────────────────── the installation: its edge's relay listener ─────────────────────────


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError):
        while chunk := await reader.read(65536):
            writer.write(chunk)
            await writer.drain()
    with contextlib.suppress(OSError):
        writer.close()


@dataclass
class Installation:
    """The installation's edge, as the relay listener sees the agent: a PROXY protocol v2 header, then
    TLS ended by a server holding `<id>.<domain>`'s key. It echoes what it reads, once decrypted."""

    proxied_from: list[str] = field(default_factory=list)
    received: list[bytes] = field(default_factory=list)
    port: int = 0
    _servers: list[asyncio.Server] = field(default_factory=list)

    async def start(self, context: ssl.SSLContext) -> None:
        tls = await asyncio.start_server(self._echo, "127.0.0.1", 0, ssl=context)
        front = await asyncio.start_server(self._front(tls.sockets[0].getsockname()[1]), "127.0.0.1", 0)
        self._servers = [tls, front]
        self.port = front.sockets[0].getsockname()[1]

    async def _echo(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(OSError, ssl.SSLError):
            while chunk := await reader.read(65536):
                self.received.append(chunk)
                writer.write(b"edge:" + chunk)
                await writer.drain()
        with contextlib.suppress(OSError, ssl.SSLError):
            writer.close()

    def _front(self, tls_port: int) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Any]:
        async def front(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            header = await reader.readexactly(16)
            assert header[:12] == _PROXY_V2_SIGNATURE, "the agent writes a PROXY v2 header first"
            address = await reader.readexactly(int.from_bytes(header[14:16], "big"))
            family = header[13] >> 4
            source = address[:4] if family == 1 else address[:16]
            self.proxied_from.append(str(ipaddress.ip_address(source)))
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", tls_port)
            await asyncio.gather(_pump(reader, up_writer), _pump(up_reader, writer))

        return front

    async def stop(self) -> None:
        for server in self._servers:
            server.close()


# ───────────────────────── the bench ─────────────────────────


@dataclass
class Bench:
    relay: Relay
    ca: TestCA
    clock: Clock
    folder: Path
    agents: list[asyncio.Task[Any]] = field(default_factory=list)

    @property
    def phone_context(self) -> ssl.SSLContext:
        return self.ca.client_context()

    def agent(self, target_port: int = 1, **options: Any) -> RelayAgent:
        return RelayAgent(
            relay=self.relay.origin,
            target=("127.0.0.1", target_port),
            ssl_context=self.ca.client_context(),
            **options,
        )

    async def pair(self, agent: RelayAgent) -> str:
        """Pairs `agent` through the relay's HTTP paths, the owner entering its user code."""
        pending = await agent.pair()
        assert pending.user_code is not None and pending.device_code is not None
        self.relay.registry.approve(pending.user_code, OWNER)
        self.clock.advance(PAIRING_INTERVAL_S)
        approved = await agent.poll(pending.device_code)
        assert approved.id is not None
        return approved.id

    async def connect(self, agent: RelayAgent, relay_id: str) -> asyncio.Task[Any]:
        """Runs `agent`'s control session until the relay welcomed it."""
        task = asyncio.create_task(agent.run(relay_id))
        self.agents.append(task)
        await asyncio.wait_for(agent.welcomed.wait(), 5)
        return task

    async def installation(self, relay_id: str) -> tuple[Installation, Issued]:
        """The installation's edge, its key made now: after the relay started, never handed to it."""
        issued = self.ca.issue(dns=(f"{relay_id}.{DOMAIN}",))
        edge = Installation()
        await edge.start(server_context(issued, self.folder, relay_id))
        return edge, issued

    def lines(self) -> list[dict[str, Any]]:
        path = self.relay.metadata.path
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def config(folder: Path, own_tls: ssl.SSLContext | None, **overrides: Any) -> RelayConfig:
    values: dict[str, Any] = {
        "domain": DOMAIN,
        "own_host": OWN_HOST,
        "metadata_path": folder / "relay-metadata.jsonl",
        "retention": dt.timedelta(days=7),
        "sni_host": "127.0.0.1",
        "sni_port": 0,
        "own_bind_host": "127.0.0.1",
        "own_port": 0,
        "own_tls": own_tls,
        "limits": RelayLimits(),
    }
    values.update(overrides)
    return RelayConfig(**values)


@contextlib.asynccontextmanager
async def bench(folder: Path, **overrides: Any) -> AsyncIterator[Bench]:
    ca = TestCA()
    own = server_context(ca.issue(ips=(OWN_HOST,)), folder, "relay-own-host")
    clock = Clock()
    relay = Relay(config(folder, own, **overrides), now=clock)
    await relay.start()
    kit = Bench(relay=relay, ca=ca, clock=clock, folder=folder)
    try:
        yield kit
    finally:
        for task in kit.agents:
            task.cancel()
        await asyncio.gather(*kit.agents, return_exceptions=True)
        await relay.stop()


# ───────────────────────── a phone, raw ─────────────────────────


def client_hello(server_name: str | None) -> bytes:
    """The ClientHello Python's own TLS stack sends for `server_name` (none when `None`)."""
    context = ssl.create_default_context()
    if server_name is None:
        context.check_hostname = False
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    tls = context.wrap_bio(incoming, outgoing, server_hostname=server_name)
    with contextlib.suppress(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


async def raw_phone(port: int, first: bytes) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(first)
    await writer.drain()
    return reader, writer


async def closed_without_a_byte(reader: asyncio.StreamReader, timeout: float = 5) -> bool:
    """Whether the relay closed the connection without sending anything back."""
    try:
        return await asyncio.wait_for(reader.read(65536), timeout) == b""
    except ConnectionResetError:
        return True


# ───────────────────────── raw clients of the relay's own host ─────────────────────────


async def dial_back(
    kit: Bench, stream: str, ticket: str
) -> tuple[int, bytes, asyncio.StreamReader, asyncio.StreamWriter]:
    """A dial-back made by hand: (status, body, reader, writer)."""
    reader, writer = await asyncio.open_connection(
        "127.0.0.1", kit.relay.own_port, ssl=kit.ca.client_context(), server_hostname=OWN_HOST
    )
    writer.write(
        (
            f"GET /relay/v1/data/{stream} HTTP/1.1\r\nHost: {OWN_HOST}\r\nConnection: Upgrade\r\n"
            f"Upgrade: choregos-relay-stream\r\nRelay-Ticket: {ticket}\r\n\r\n"
        ).encode()
    )
    await writer.drain()
    head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    status = int(head.split(b" ", 2)[1])
    length = 0
    for line in head.decode("latin-1").split("\r\n")[1:]:
        if line.lower().startswith("content-length:"):
            length = int(line.split(":", 1)[1])
    body = await reader.readexactly(length) if length else b""
    return status, body, reader, writer


@contextlib.asynccontextmanager
async def control(kit: Bench) -> AsyncIterator[ClientConnection]:
    """A control connection opened by hand, to send a hello of the test's making."""
    async with connect(
        kit.relay.control_url, ssl=kit.ca.client_context(), proxy=None, open_timeout=5
    ) as socket:
        yield socket


def signed_hello(
    key: Ed25519PrivateKey,
    *,
    url: str,
    relay_id: str,
    nonce: str,
    protocol: int = RELAY_PROTOCOL,
    iat: int | None = None,
) -> str:
    claims = hello_signature_claims(
        url=url,
        relay_id=relay_id,
        protocol=protocol,
        nonce=nonce,
        iat=int(time.time()) if iat is None else iat,
    )
    signature = jwt.encode(claims, key, algorithm="EdDSA", headers=hello_signature_header(relay_id))
    return json.dumps(
        {
            "type": "hello",
            "protocol": protocol,
            "agent_version": "0.30.0",
            "id": relay_id,
            "signature": signature,
        }
    )


async def answer(socket: ClientConnection) -> dict[str, Any]:
    message = json.loads(await asyncio.wait_for(socket.recv(), 5))
    assert isinstance(message, dict)
    return message


async def eventually(condition: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("the condition never held")
        await asyncio.sleep(0.01)
