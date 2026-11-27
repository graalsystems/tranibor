# SPDX-License-Identifier: Apache-2.0
"""The control channel: one signed WebSocket session per installation (`x-control`, ADR 0230 §3).

The relay sends a challenge; the agent answers a hello signed by the installation's Ed25519 key,
binding this relay's control URL, the id, the protocol, the challenge's nonce and an `iat` within 60
seconds of the relay's clock. The hello is judged in the contract's order (`x-hello-signature`); a
refused one is a `close` message, and leaves the id's live session as it was. A hello that passes
opens the id's one session, and a live one is sent `close` `replaced`. From then on the relay sends
one `open_stream` per phone connection, and the agent sends nothing.

The WebSocket is driven by websockets' Sans-I/O protocol over the relay's own connection, since the
same host also serves an upgrade no WebSocket server hands over (`x-data`). Its logger is the relay's,
pinned and out of any logging configuration's reach (`metadata.library_logger`): the relay logs nothing
but its metadata lines (ADR 0230 §7).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
from collections import deque
from collections.abc import Callable
from typing import Any, Final

from pydantic import ValidationError
from tranibor_contracts.relay import (
    IAT_WINDOW_S,
    MAX_CONTROL_MESSAGE_BYTES,
    MAX_MISSED_PONGS,
    RelayCloseCode,
    RelayHello,
    RelayOpenStream,
    RelayProblemCode,
    RelayProtocolWindow,
    hello_signature_header,
    is_served,
)
from websockets.exceptions import InvalidState, ProtocolError
from websockets.frames import CloseCode, Frame, Opcode
from websockets.http11 import Request
from websockets.server import ServerProtocol

from .limits import SlidingWindow
from .metadata import library_logger
from .pairing import Installation, RelayRegistry
from .signing import SignatureError, verified
from .wire import CHUNK, RequestHead, iso, problem, response

#: The relay's WebSocket logs nothing: not a frame, not a close (ADR 0230 §7), whatever the process's
#: logging configuration (`library_logger`).
_SILENT: Final = library_logger("tranibor_sni_relay.websocket")
#: How long a closing session waits for the agent's close frame before it drops the connection.
CLOSE_TIMEOUT_S: Final = 2.0
#: The frames the relay reads: a little above the largest message, so that a larger one is refused
#: with a `close` message rather than by the framing alone.
_MAX_FRAME: Final = 4 * MAX_CONTROL_MESSAGE_BYTES
#: Refusals of a hello, which close with a policy violation; the rest close normally.
_NORMAL: Final = frozenset({RelayCloseCode.REPLACED, RelayCloseCode.SUSPENDED, RelayCloseCode.HELLO_TIMEOUT})


class SessionGoneError(Exception):
    """The session closed before the message could be sent."""


class _Socket:
    """One server-side WebSocket over an asyncio stream. One reader only: the coroutine serving it."""

    def __init__(
        self, protocol: ServerProtocol, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._protocol = protocol
        self._reader = reader
        self._writer = writer
        self._events: deque[Frame] = deque()
        self._eof = False
        self._closing = False
        self._deadline: asyncio.TimerHandle | None = None

    @property
    def closing(self) -> bool:
        return self._closing

    async def flush(self) -> None:
        ended = False
        for chunk in self._protocol.data_to_send():
            if chunk:
                self._writer.write(chunk)
            else:  # the closing handshake is over: the server ends the TCP connection
                ended = True
        with contextlib.suppress(OSError, RuntimeError):
            await self._writer.drain()
        if ended:
            self._writer.close()
            return
        if self._protocol.close_expected() and self._deadline is None:
            self._deadline = asyncio.get_running_loop().call_later(CLOSE_TIMEOUT_S, self._writer.close)

    async def frame(self) -> Frame | None:
        """The next frame the agent sent; `None` once the connection ended."""
        while not self._events:
            if self._eof:
                return None
            try:
                chunk = await self._reader.read(CHUNK)
            except OSError:
                chunk = b""
            if chunk:
                self._protocol.receive_data(chunk)
            else:
                self._eof = True
                with contextlib.suppress(EOFError):
                    self._protocol.receive_eof()
            self._events.extend(
                event for event in self._protocol.events_received() if isinstance(event, Frame)
            )
            await self.flush()
        return self._events.popleft()

    async def send(self, message: dict[str, Any]) -> None:
        if self._closing:
            raise SessionGoneError
        try:
            self._protocol.send_text(json.dumps(message).encode())
        except (InvalidState, ProtocolError) as gone:
            raise SessionGoneError from gone
        await self.flush()

    def ping(self) -> None:
        with contextlib.suppress(InvalidState, ProtocolError):
            self._protocol.send_ping(secrets.token_bytes(4))

    async def close(self, message: dict[str, Any], code: CloseCode) -> None:
        """Sends `message`, then a close frame; the serving coroutine reads until the agent's close."""
        if self._closing:
            return
        self._closing = True
        with contextlib.suppress(InvalidState, ProtocolError):
            self._protocol.send_text(json.dumps(message).encode())
            self._protocol.send_close(code)
        await self.flush()

    def abort(self) -> None:
        """Drops the connection without a closing handshake: the agent no longer answers."""
        self._closing = True
        self._writer.close()

    def end(self) -> None:
        if self._deadline is not None:
            self._deadline.cancel()


class ControlSession:
    """An installation's live control session, after a hello the relay took."""

    def __init__(self, socket: _Socket, relay_id: str, message: Callable[..., dict[str, Any]]) -> None:
        self.relay_id = relay_id
        self._socket = socket
        self._message = message
        self._missed = 0

    @property
    def closing(self) -> bool:
        return self._socket.closing

    async def send(self, message: dict[str, Any]) -> None:
        """One message to the agent; `SessionGoneError` when the session is closing or closed."""
        await self._socket.send(message)

    async def open_stream(self, order: RelayOpenStream) -> None:
        """The order to dial back for one phone connection: no host, no port (`x-target`)."""
        await self.send(order.model_dump(mode="json"))

    async def close(self, code: RelayCloseCode, detail: str | None = None) -> None:
        await self._socket.close(self._message(code, detail), CloseCode.NORMAL_CLOSURE)

    async def run(self, ping_interval_s: float) -> None:
        """Reads until the agent leaves: a pong clears the missed count, any message is refused."""
        pinger = asyncio.create_task(self._ping(ping_interval_s))
        try:
            while (frame := await self._socket.frame()) is not None:
                if frame.opcode is Opcode.PONG:
                    self._missed = 0
                elif frame.opcode in (Opcode.TEXT, Opcode.BINARY, Opcode.CONT):
                    await self.close(
                        RelayCloseCode.INVALID_MESSAGE, "the agent sends nothing after its hello"
                    )
        finally:
            pinger.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await pinger

    async def _ping(self, interval_s: float) -> None:
        while True:
            await asyncio.sleep(interval_s)
            if self._missed >= MAX_MISSED_PONGS:
                self._socket.abort()
                return
            self._missed += 1
            self._socket.ping()
            await self._socket.flush()


class ControlChannel:
    """`GET /relay/v1/control`: the challenge, the hello, and the sessions of every installation."""

    def __init__(
        self,
        registry: RelayRegistry,
        *,
        url: Callable[[], str],
        window: RelayProtocolWindow,
        hello_timeout_s: float,
        ping_interval_s: float,
        hellos_per_minute: int,
        now: Callable[[], float],
    ) -> None:
        self._registry = registry
        self._url = url
        self._window = window
        self._hello_timeout_s = hello_timeout_s
        self._ping_interval_s = ping_interval_s
        self._now = now
        self._sessions: dict[str, ControlSession] = {}
        self._per_id = SlidingWindow(hellos_per_minute, 60, now)
        self._per_address = SlidingWindow(hellos_per_minute, 60, now)

    def session(self, relay_id: str) -> ControlSession | None:
        """The id's live session, if it has one that is not closing."""
        found = self._sessions.get(relay_id)
        return found if found is not None and not found.closing else None

    def message(self, code: RelayCloseCode, detail: str | None = None) -> dict[str, Any]:
        """A `close` message: its code, the relay's time, the window when the protocol is refused."""
        document: dict[str, Any] = {"type": "close", "code": code.value, "server_time": iso(self._now())}
        if code is RelayCloseCode.PROTOCOL_UNSUPPORTED:
            document["protocol"] = self._window.model_dump()
            detail = detail or f"this relay serves protocols {self._window.minimum} to {self._window.current}"
        if detail is not None:
            document["detail"] = detail
        return document

    async def end(self, relay_id: str, code: RelayCloseCode) -> None:
        """Closes the id's live session, if any (a suspension)."""
        found = self._sessions.get(relay_id)
        if found is not None:
            await found.close(code)

    async def serve(
        self, head: RequestHead, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, client_ip: str
    ) -> None:
        if not self._per_address.allow(client_ip):
            writer.write(
                problem(
                    RelayProblemCode.RATE_LIMITED,
                    now=self._now(),
                    retry_after=self._per_address.retry_after(client_ip),
                )
            )
            return
        protocol = ServerProtocol(max_size=_MAX_FRAME, logger=_SILENT)
        protocol.receive_data(head.raw)
        requests = [event for event in protocol.events_received() if isinstance(event, Request)]
        if not requests:
            writer.write(response(400))
            return
        answer = protocol.accept(requests[0])
        protocol.send_response(answer)
        socket = _Socket(protocol, reader, writer)
        try:
            await socket.flush()
            if answer.status_code == 101:
                await self._converse(socket, client_ip)
        finally:
            socket.end()

    async def _converse(self, socket: _Socket, client_ip: str) -> None:
        nonce = secrets.token_urlsafe(16)
        await socket.send(
            {
                "type": "challenge",
                "nonce": nonce,
                "server_time": iso(self._now()),
                "protocol": self._window.model_dump(),
            }
        )
        verdict: Installation | RelayCloseCode
        try:
            async with asyncio.timeout(self._hello_timeout_s):
                frame = await socket.frame()
        except TimeoutError:
            verdict = RelayCloseCode.HELLO_TIMEOUT
        else:
            verdict = self._judge(frame, nonce)
        if not isinstance(verdict, Installation):
            await socket.close(
                self.message(verdict),
                CloseCode.NORMAL_CLOSURE if verdict in _NORMAL else CloseCode.POLICY_VIOLATION,
            )
            while await socket.frame() is not None:
                pass
            return
        session = ControlSession(socket, verdict.id, self.message)
        replaced = self._sessions.get(verdict.id)
        self._sessions[verdict.id] = session
        try:
            await session.send(
                {
                    "type": "welcome",
                    "id": verdict.id,
                    "hostname": self._registry.hostname(verdict.id),
                    "server_time": iso(self._now()),
                    "protocol": self._window.model_dump(),
                }
            )
            if replaced is not None:
                await replaced.close(RelayCloseCode.REPLACED)
            await session.run(self._ping_interval_s)
        finally:
            if self._sessions.get(verdict.id) is session:
                del self._sessions[verdict.id]

    def _judge(self, frame: Frame | None, nonce: str) -> Installation | RelayCloseCode:
        """The hello, checked in `x-hello-signature`'s order; the first refusal is the answer."""
        if (
            frame is None
            or frame.opcode is not Opcode.TEXT
            or not frame.fin
            or len(frame.data) > MAX_CONTROL_MESSAGE_BYTES
        ):
            return RelayCloseCode.INVALID_MESSAGE
        try:
            hello = RelayHello.model_validate_json(bytes(frame.data))
        except ValidationError:
            return RelayCloseCode.INVALID_MESSAGE
        if not is_served(hello.protocol, self._window):
            return RelayCloseCode.PROTOCOL_UNSUPPORTED
        installation = self._registry.installation(hello.id)
        if installation is None:
            return RelayCloseCode.UNKNOWN_ID
        try:
            claims = verified(hello.signature, installation.public, header=hello_signature_header(hello.id))
        except SignatureError:
            return RelayCloseCode.INVALID_SIGNATURE
        bound = {"htu": self._url(), "id": hello.id, "protocol": hello.protocol}
        if any(
            claims.get(name) != value or isinstance(claims.get(name), bool) for name, value in bound.items()
        ):
            return RelayCloseCode.INVALID_SIGNATURE
        claimed = claims.get("nonce")
        if not isinstance(claimed, str) or not hmac.compare_digest(claimed.encode(), nonce.encode()):
            return RelayCloseCode.STALE_NONCE
        iat = claims.get("iat")
        if not isinstance(iat, int) or isinstance(iat, bool) or abs(self._now() - iat) > IAT_WINDOW_S:
            return RelayCloseCode.STALE_IAT
        if installation.suspended is not None:
            return RelayCloseCode.SUSPENDED
        if not self._per_id.allow(hello.id):
            return RelayCloseCode.RATE_LIMITED
        return installation


__all__ = ["ControlChannel", "ControlSession", "SessionGoneError"]
