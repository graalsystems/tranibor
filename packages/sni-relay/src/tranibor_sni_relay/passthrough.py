# SPDX-License-Identifier: Apache-2.0
"""The relay server: it routes a phone's TLS by SNI to its installation and never terminates it.

Two listeners (ADR 0230 §3-4, `relay.yaml`):

- **the SNI listener** (443 in production, `*.<relay-domain>`), plain TCP. For each phone connection
  it checks the client address's rate before reading anything, reads the ClientHello within bounds
  (`sni.py`), and routes `<id>.<relay-domain>` to the id's one control session: the session is sent an
  order to dial back that names a stream, a single-use ticket and the phone's address, and no host or
  port. Once the installation's agent dials back, the ClientHello and then every byte are spliced both
  ways, unparsed. A connection without a server name, with a malformed or out-of-bounds ClientHello, a
  name outside the domain, an unknown or suspended id, no control session or past a limit is closed.
  No byte is stored: without a control session, the connection is closed (§9).
- **the relay's own host**, which has a certificate of its own (or sits behind a load balancer that
  holds it): the pairing, the control channel and the dial-backs, under `/relay/v1`.

The relay holds no certificate and no key for any `<id>` name: its configuration has no field for one,
and the SNI listener is opened without TLS. Every connection the SNI listener accepts ends with one
metadata line (`metadata.py`); the relay logs nothing else.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import re
import secrets
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Self

from tranibor_contracts.relay import (
    CLIENT_HELLO_MAX_BYTES,
    CLIENT_HELLO_TIMEOUT_S,
    HELLO_TIMEOUT_S,
    MAX_BODY_BYTES,
    PING_INTERVAL_S,
    RELAY_PREFIX,
    RELAY_PROTOCOL,
    STREAM_ID_PATTERN,
    STREAM_UPGRADE,
    TICKET_HEADER,
    TICKET_TTL_S,
    UNROUTED,
    RelayCloseCode,
    RelayCloseReason,
    RelayMetadataLine,
    RelayOpenStream,
    RelayProblemCode,
    protocol_window,
)

from .control import ControlChannel, SessionGoneError
from .limits import MonthlyBytes, RelayLimits, SlidingWindow
from .metadata import MetadataLog, RelayConfigError
from .pairing import PAIRING_PATH, POLL_PATH, PairingDoor, RelayRegistry
from .sni import read_client_hello
from .wire import (
    MAX_HEAD_BYTES,
    ByteLimitError,
    HeadError,
    RequestHead,
    Side,
    close_quietly,
    problem,
    read_head,
    response,
    splice,
)

CONTROL_PATH: Final = f"{RELAY_PREFIX}/control"
DATA_PREFIX: Final = f"{RELAY_PREFIX}/data/"
_ID_LABEL: Final = re.compile(r"^[a-z2-7]{12}$")
_HOST: Final = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$|^[0-9.]+$"
)

__all__ = ["Relay", "RelayConfig", "RelayConfigError", "RelayLimits"]


@dataclass(frozen=True)
class RelayConfig:
    """What the relay's operator sets. There is no field for a certificate or a key of the SNI
    listener: the relay has none for any `<id>` name (ADR 0230 §4)."""

    #: `<relay-domain>`: an installation's name is `<id>.<relay-domain>`.
    domain: str
    #: Where the metadata lines are written.
    metadata_path: Path
    #: How long a metadata line is kept. Required: a relay without one refuses to start (ADR 0230 §7).
    retention: timedelta | None
    #: The relay's own host, which agents dial (pairing, control, dial-backs). The domain by default.
    own_host: str | None = None
    #: The SNI listener; `None` listens on every interface.
    sni_host: str | None = None
    sni_port: int = 443
    #: The own host's listener; `None` listens on every interface.
    own_bind_host: str | None = None
    own_port: int = 8443
    #: The own host's TLS, its own certificate; `None` when a load balancer in front of it holds it.
    own_tls: ssl.SSLContext | None = None
    #: The port agents dial the own host on, when it is not the one bound (443 is left out of URLs).
    own_public_port: int | None = None
    #: The protocol this relay speaks; it serves it and the one before.
    protocol: int = RELAY_PROTOCOL
    limits: RelayLimits = field(default_factory=RelayLimits)
    #: Where the owner enters a user code; `https://<own host>/pair` by default.
    verification_uri: str | None = None
    client_hello_timeout_s: float = CLIENT_HELLO_TIMEOUT_S
    hello_timeout_s: float = HELLO_TIMEOUT_S
    ticket_ttl_s: float = TICKET_TTL_S
    ping_interval_s: float = PING_INTERVAL_S
    #: A spliced connection across which nothing crossed this long is closed.
    idle_timeout_s: float = 900.0
    #: How often the lines past the retention are deleted.
    prune_interval_s: float = 3600.0

    def __post_init__(self) -> None:
        for name in (self.domain, self.own_host or self.domain):
            if not _HOST.match(name):
                raise RelayConfigError(f"not a host name in lower case: {name!r}")


@dataclass
class _Stream:
    """A phone connection waiting for its installation's dial-back."""

    install_id: str
    ticket: str
    expires_at: float
    dialled: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]]
    finished: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Line:
    """A metadata line while its connection lasts."""

    client_ip: str
    started_at: float
    started: float = field(default_factory=time.monotonic)
    install_id: str | None = None
    bytes_in: int = 0
    bytes_out: int = 0
    reason: RelayCloseReason = RelayCloseReason.ERROR

    def closed(self) -> RelayMetadataLine:
        if self.install_id is None and self.reason not in UNROUTED:
            # Stopped before the ClientHello was read (the relay shutting down, a socket error): the
            # contract lets an unrouted line say only why the ClientHello was not had in time.
            self.reason = RelayCloseReason.CLIENT_HELLO_TIMEOUT
        duration_ms = int((time.monotonic() - self.started) * 1000)
        started_at = datetime.fromtimestamp(self.started_at, UTC)
        return RelayMetadataLine(
            install_id=self.install_id,
            started_at=started_at,
            ended_at=started_at + timedelta(milliseconds=duration_ms),
            client_ip=self.client_ip,
            bytes_in=self.bytes_in,
            bytes_out=self.bytes_out,
            duration_ms=duration_ms,
            close_reason=self.reason,
        )


class _ClosedError(Exception):
    """The phone's connection is closed for this reason."""

    def __init__(self, reason: RelayCloseReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


class Relay:
    """The relay server. `start`, then `stop`; or `async with`."""

    def __init__(
        self,
        config: RelayConfig,
        *,
        registry: RelayRegistry | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._now = now
        self.metadata = MetadataLog(config.metadata_path, config.retention, now)
        self.registry = registry if registry is not None else RelayRegistry(domain=config.domain, now=now)
        self.own_host = config.own_host or config.domain
        window = protocol_window(config.protocol)
        self.control = ControlChannel(
            self.registry,
            url=lambda: self.control_url,
            window=window,
            hello_timeout_s=config.hello_timeout_s,
            ping_interval_s=config.ping_interval_s,
            hellos_per_minute=config.limits.hellos_per_minute,
            now=now,
        )
        self.pairing = PairingDoor(
            self.registry,
            origin=lambda: self.origin,
            window=window,
            verification_uri=config.verification_uri or f"https://{self.own_host}/pair",
            pairings_per_minute=config.limits.pairings_per_minute,
            now=now,
        )
        self._per_address = SlidingWindow(config.limits.client_connections_per_minute, 60, now)
        self._per_install = SlidingWindow(config.limits.streams_per_minute, 60, now)
        self._monthly = MonthlyBytes(config.limits.bytes_per_month, now)
        self._streams: dict[str, _Stream] = {}
        self._open: dict[str, int] = {}
        self._phones: set[asyncio.Task[None]] = set()
        self._handlers: set[asyncio.Task[None]] = set()
        self._servers: list[asyncio.Server] = []
        self._pruner: asyncio.Task[None] | None = None
        self._own_port = config.own_port

    # ───────────────────────── addresses ─────────────────────────

    @property
    def sni_port(self) -> int:
        port: int = self._servers[0].sockets[0].getsockname()[1]
        return port

    @property
    def own_port(self) -> int:
        return self._own_port

    @property
    def origin(self) -> str:
        """The own host's public URL, which a pairing signature binds: scheme and host, a default port
        left out."""
        port = self.config.own_public_port or self._own_port
        return f"https://{self.own_host}" + ("" if port == 443 else f":{port}")

    @property
    def control_url(self) -> str:
        """The control channel's URL, which a hello's signature binds."""
        return "wss://" + self.origin.removeprefix("https://") + CONTROL_PATH

    # ───────────────────────── life ─────────────────────────

    async def start(self) -> None:
        await self.metadata.prune()
        sni = await asyncio.start_server(
            self._tracked(self._serve_phone, self._phones),
            self.config.sni_host,
            self.config.sni_port,
            ssl=None,
        )
        own = await asyncio.start_server(
            self._tracked(self._serve_own, self._handlers),
            self.config.own_bind_host,
            self.config.own_port,
            ssl=self.config.own_tls,
            limit=MAX_HEAD_BYTES,
        )
        self._servers = [sni, own]
        self._own_port = own.sockets[0].getsockname()[1]
        self._pruner = asyncio.create_task(self._prune_forever())

    async def stop(self) -> None:
        for server in self._servers:
            server.close()
        tasks = [*self._phones, *self._handlers, *([self._pruner] if self._pruner else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for server in self._servers:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(2):
                    await server.wait_closed()

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def settle(self) -> None:
        """Waits until every phone connection accepted so far has ended and its line is written."""
        while pending := [task for task in self._phones if not task.done()]:
            await asyncio.gather(*pending, return_exceptions=True)

    def pending_streams(self) -> int:
        """Phone connections waiting for their dial-back."""
        return len(self._streams)

    async def suspend(self, relay_id: str, reason: str) -> None:
        """Suspends an id: its live session is closed, its hellos refused, its ClientHellos closed."""
        self.registry.suspend(relay_id, reason)
        await self.control.end(relay_id, RelayCloseCode.SUSPENDED)

    def _tracked(
        self,
        serve: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
        into: set[asyncio.Task[None]],
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            task = asyncio.current_task()
            assert task is not None
            into.add(task)
            try:
                await serve(reader, writer)
            finally:
                into.discard(task)

        return handler

    async def _prune_forever(self) -> None:
        while True:
            await asyncio.sleep(self.config.prune_interval_s)
            await self.metadata.prune()

    # ───────────────────────── the SNI listener ─────────────────────────

    async def _serve_phone(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        line = _Line(client_ip=str(peer[0]) if peer else "0.0.0.0", started_at=self._now())  # noqa: S104 - an address, not a bind
        try:
            line.reason = await self._route(reader, writer, line)
        except _ClosedError as closed:
            line.reason = closed.reason
        except asyncio.CancelledError:
            line.reason = RelayCloseReason.RELAY_SHUTDOWN
            raise
        except OSError:
            line.reason = RelayCloseReason.ERROR
        finally:
            await close_quietly(writer)
            await self.metadata.write(line.closed())

    def _install_id(self, server_name: str) -> str:
        """The id a server name routes to: exactly `<id>.<relay-domain>`."""
        label, dot, rest = server_name.partition(".")
        if not dot or rest != self.config.domain:
            raise _ClosedError(RelayCloseReason.OUTSIDE_DOMAIN)
        if not _ID_LABEL.match(label) or self.registry.installation(label) is None:
            raise _ClosedError(RelayCloseReason.UNKNOWN_ID)
        return label

    async def _route(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, line: _Line
    ) -> RelayCloseReason:
        if not self._per_address.allow(line.client_ip):
            raise _ClosedError(RelayCloseReason.LIMIT_CLIENT_CONNECTIONS_PER_MINUTE)
        hello = await read_client_hello(
            reader, max_bytes=CLIENT_HELLO_MAX_BYTES, timeout_s=self.config.client_hello_timeout_s
        )
        line.bytes_in = len(hello.data)
        if hello.refusal is not None:
            raise _ClosedError(hello.refusal)
        assert hello.server_name is not None
        relay_id = self._install_id(hello.server_name)
        line.install_id = relay_id
        installation = self.registry.installation(relay_id)
        if installation is None or installation.suspended is not None:
            raise _ClosedError(RelayCloseReason.SUSPENDED if installation else RelayCloseReason.UNKNOWN_ID)
        session = self.control.session(relay_id)
        if session is None:
            raise _ClosedError(RelayCloseReason.NO_CONTROL_SESSION)
        if self._open.get(relay_id, 0) >= self.config.limits.concurrent_streams:
            raise _ClosedError(RelayCloseReason.LIMIT_CONCURRENT_STREAMS)
        if not self._monthly.available(relay_id):
            raise _ClosedError(RelayCloseReason.LIMIT_BYTES_PER_MONTH)
        if not self._per_install.allow(relay_id):
            raise _ClosedError(RelayCloseReason.LIMIT_STREAMS_PER_MINUTE)
        self._open[relay_id] = self._open.get(relay_id, 0) + 1
        try:
            return await self._stream(reader, writer, line, relay_id, hello.data)
        finally:
            self._open[relay_id] -= 1
            if not self._open[relay_id]:
                del self._open[relay_id]

    async def _stream(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        line: _Line,
        relay_id: str,
        first: bytes,
    ) -> RelayCloseReason:
        stream_id = secrets.token_urlsafe(16)
        stream = _Stream(
            install_id=relay_id,
            ticket=secrets.token_urlsafe(32),
            expires_at=self._now() + self.config.ticket_ttl_s,
            dialled=asyncio.get_running_loop().create_future(),
        )
        self._streams[stream_id] = stream
        try:
            session = self.control.session(relay_id)
            if session is None:
                raise _ClosedError(RelayCloseReason.NO_CONTROL_SESSION)
            try:
                await session.open_stream(
                    RelayOpenStream(
                        type="open_stream", stream=stream_id, ticket=stream.ticket, client_ip=line.client_ip
                    )
                )
            except SessionGoneError as gone:
                raise _ClosedError(RelayCloseReason.NO_CONTROL_SESSION) from gone
            try:
                async with asyncio.timeout(self.config.ticket_ttl_s):
                    agent = await asyncio.shield(stream.dialled)
            except TimeoutError as late:
                raise _ClosedError(RelayCloseReason.DIAL_BACK_TIMEOUT) from late
            return await self._splice(reader, writer, agent, line, relay_id, first)
        finally:
            self._streams.pop(stream_id, None)
            stream.finished.set()
            if not stream.dialled.done():
                stream.dialled.cancel()

    async def _splice(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        agent: tuple[asyncio.StreamReader, asyncio.StreamWriter],
        line: _Line,
        relay_id: str,
        first: bytes,
    ) -> RelayCloseReason:
        try:
            agent[1].write(first)
            await agent[1].drain()
        except OSError:
            await close_quietly(agent[1])
            return RelayCloseReason.INSTALLATION_CLOSED
        if not self._monthly.add(relay_id, len(first)):
            await close_quietly(agent[1])
            return RelayCloseReason.LIMIT_BYTES_PER_MONTH

        def inward(size: int) -> bool:
            line.bytes_in += size
            return self._monthly.add(relay_id, size)

        def outward(size: int) -> bool:
            line.bytes_out += size
            return self._monthly.add(relay_id, size)

        ended = await splice(
            (reader, writer),
            agent,
            near_counted=inward,
            far_counted=outward,
            idle_s=self.config.idle_timeout_s,
        )
        if isinstance(ended, ByteLimitError):
            return RelayCloseReason.LIMIT_BYTES_PER_MONTH
        if isinstance(ended, TimeoutError):
            return RelayCloseReason.IDLE_TIMEOUT
        if isinstance(ended, OSError):
            return RelayCloseReason.ERROR
        return RelayCloseReason.CLIENT_CLOSED if ended is Side.NEAR else RelayCloseReason.INSTALLATION_CLOSED

    # ───────────────────────── the relay's own host ─────────────────────────

    async def _serve_own(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        client_ip = str(peer[0]) if peer else ""
        upgraded = False
        try:
            head = await read_head(reader)
            if head.path == CONTROL_PATH and head.method == "GET":
                await self.control.serve(head, reader, writer, client_ip)
            elif head.path.startswith(DATA_PREFIX) and head.method == "GET":
                upgraded = await self._dial_back(head, reader, writer)
            elif head.path in (PAIRING_PATH, POLL_PATH) and head.method == "POST":
                writer.write(await self._pairing(head, reader, client_ip))
            else:
                writer.write(response(404))
            if not upgraded:
                with contextlib.suppress(OSError, RuntimeError):
                    await writer.drain()
        except HeadError:
            writer.write(response(400))
        except OSError:
            pass
        finally:
            if not upgraded:
                await close_quietly(writer)

    async def _pairing(self, head: RequestHead, reader: asyncio.StreamReader, client_ip: str) -> bytes:
        length = head.header("content-length")
        if head.header("transfer-encoding") is not None or length is None or not length.isdigit():
            return response(411)
        if int(length) > MAX_BODY_BYTES["pairing" if head.path == PAIRING_PATH else "pairing_poll"]:
            return problem(RelayProblemCode.BODY_TOO_LARGE, now=self._now())
        try:
            async with asyncio.timeout(10):
                body = await reader.readexactly(int(length))
        except (TimeoutError, asyncio.IncompleteReadError):
            return response(400)
        return self.pairing.answer(head.path, body, head.header("relay-signature"), client_ip)

    async def _dial_back(
        self, head: RequestHead, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> bool:
        """`GET /relay/v1/data/{stream}`: the ticket opens its stream once; then the connection is the
        phone's, until the splice ends. Whether the connection was handed over."""
        stream_id = head.path.removeprefix(DATA_PREFIX)
        upgrade = (head.header("upgrade") or "").lower()
        connection = {token.strip().lower() for token in (head.header("connection") or "").split(",")}
        if upgrade != STREAM_UPGRADE or "upgrade" not in connection:
            writer.write(response(400))
            return False
        stream = self._claim(stream_id, head.header(TICKET_HEADER.lower()) or "")
        if stream is None:
            writer.write(problem(RelayProblemCode.TICKET_REFUSED, now=self._now()))
            return False
        writer.write(response(101, headers={"Connection": "Upgrade", "Upgrade": STREAM_UPGRADE}))
        stream.dialled.set_result((reader, writer))
        await stream.finished.wait()
        return True

    def _claim(self, stream_id: str, ticket: str) -> _Stream | None:
        """The stream `ticket` opens, once: unknown, used, expired or another stream's is refused alike."""
        if not re.fullmatch(STREAM_ID_PATTERN, stream_id):
            return None
        stream = self._streams.get(stream_id)
        if stream is None or not hmac.compare_digest(stream.ticket.encode(), ticket.encode()):
            return None
        del self._streams[stream_id]
        if self._now() > stream.expires_at or stream.dialled.done():
            return None
        return stream
