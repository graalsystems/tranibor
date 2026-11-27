# SPDX-License-Identifier: Apache-2.0
"""The bounded ClientHello reader: the server name of a phone's TLS connection, and nothing more.

The relay routes a phone's connection by the `server_name` of its ClientHello (RFC 6066 §3) and
never terminates its TLS (ADR 0230 §4). This module reads that one field within two bounds,
`CLIENT_HELLO_MAX_BYTES` and `CLIENT_HELLO_TIMEOUT_S`, and:

- reads whole TLS records, exactly as many bytes as the ClientHello's records hold and never one
  more: what follows belongs to the stream and is spliced, not read;
- never reads past its bound: a record or a handshake whose declared length cannot fit is refused
  before its bytes are asked for;
- never raises on what a phone sends: every byte string ends in a `Scan` that names a server, asks
  for more bytes, or says why the connection is closed.

It keeps the bytes it read, so that the relay forwards them first, unchanged. It does not look at
the ALPN, the cipher suites or any other extension: the relay neither needs nor logs them.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Final, Protocol

from tranibor_contracts.relay import CLIENT_HELLO_MAX_BYTES, CLIENT_HELLO_TIMEOUT_S, RelayCloseReason

#: A TLS record's header: content type, legacy version (two bytes), length (two bytes).
RECORD_HEADER: Final = 5
#: The most a TLS plaintext record may carry (RFC 8446 §5.1).
MAX_RECORD: Final = 1 << 14
_HANDSHAKE: Final = 22
_CLIENT_HELLO: Final = 1
_HANDSHAKE_HEADER: Final = 4
_SERVER_NAME: Final = 0
_HOST_NAME: Final = 0
_RANDOM: Final = 32
_MAX_SESSION_ID: Final = 32
_MAX_NAME: Final = 253
_NAME_BYTES: Final = frozenset(b"abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-.")


@dataclass(frozen=True)
class Scan:
    """What the bytes read so far say: exactly one of `need`, `server_name` and `refusal` is set."""

    #: Read exactly this many more bytes, then scan again.
    need: int = 0
    #: The server name, in lower case.
    server_name: str | None = None
    #: Why the connection is closed.
    refusal: RelayCloseReason | None = None


@dataclass(frozen=True)
class ClientHello:
    """A phone's ClientHello as the relay read it: every byte read, to forward first, and what they say."""

    data: bytes
    server_name: str | None
    refusal: RelayCloseReason | None


class ByteSource(Protocol):
    """What the reader reads from: an `asyncio.StreamReader`, or a test's stand-in."""

    async def readexactly(self, n: int) -> bytes: ...


_MALFORMED: Final = Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)
_TOO_LARGE: Final = Scan(refusal=RelayCloseReason.CLIENT_HELLO_TOO_LARGE)
_NO_SNI: Final = Scan(refusal=RelayCloseReason.NO_SNI)


class _MalformedError(Exception):
    """A length that overruns what holds it, or a value the grammar refuses."""


class _Cursor:
    """A reader over bytes that refuses to overrun them."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._at = 0

    @property
    def done(self) -> bool:
        return self._at == len(self._data)

    @property
    def length(self) -> int:
        return len(self._data)

    def take(self, count: int) -> bytes:
        if self._at + count > len(self._data):
            raise _MalformedError
        chunk = self._data[self._at : self._at + count]
        self._at += count
        return chunk

    def number(self, width: int) -> int:
        return int.from_bytes(self.take(width), "big")

    def vector(self, width: int) -> _Cursor:
        return _Cursor(self.take(self.number(width)))


def scan_client_hello(data: bytes, *, max_bytes: int = CLIENT_HELLO_MAX_BYTES) -> Scan:
    """What `data`, the first bytes of a connection, say. Never raises; asks for more only when the
    records read so far declare it, and never for a byte past `max_bytes`."""
    handshake = bytearray()
    position = 0
    while True:
        if len(data) - position < RECORD_HEADER:
            if position + RECORD_HEADER > max_bytes:
                return _TOO_LARGE
            return Scan(need=position + RECORD_HEADER - len(data))
        content_type, major, minor = data[position], data[position + 1], data[position + 2]
        length = int.from_bytes(data[position + 3 : position + RECORD_HEADER], "big")
        if content_type != _HANDSHAKE or major != 3 or minor > 4 or not 0 < length <= MAX_RECORD:
            return _MALFORMED
        end = position + RECORD_HEADER + length
        if end > max_bytes:
            return _TOO_LARGE
        if len(data) < end:
            return Scan(need=end - len(data))
        handshake += data[position + RECORD_HEADER : end]
        position = end
        if len(handshake) < _HANDSHAKE_HEADER:
            continue
        if handshake[0] != _CLIENT_HELLO:
            return _MALFORMED
        body_length = int.from_bytes(handshake[1:_HANDSHAKE_HEADER], "big")
        if _HANDSHAKE_HEADER + body_length + RECORD_HEADER > max_bytes:
            return _TOO_LARGE
        if len(handshake) >= _HANDSHAKE_HEADER + body_length:
            return _server_name(bytes(handshake[_HANDSHAKE_HEADER : _HANDSHAKE_HEADER + body_length]))


def _server_name(body: bytes) -> Scan:
    """The `host_name` of a ClientHello's body (RFC 8446 §4.1.2, RFC 6066 §3)."""
    try:
        hello = _Cursor(body)
        if hello.number(2) >> 8 != 3:  # legacy_version
            raise _MalformedError
        hello.take(_RANDOM)
        if hello.vector(1).length > _MAX_SESSION_ID:
            raise _MalformedError
        suites = hello.vector(2)
        if suites.length < 2 or suites.length % 2:
            raise _MalformedError
        if hello.vector(1).length < 1:  # compression methods
            raise _MalformedError
        if hello.done:
            return _NO_SNI  # no extensions at all
        extensions = hello.vector(2)
        if not hello.done:
            raise _MalformedError
        seen: set[int] = set()
        name: str | None = None
        while not extensions.done:
            kind = extensions.number(2)
            content = extensions.vector(2)
            if kind in seen:  # RFC 8446 §4.2: one extension of each type
                raise _MalformedError
            seen.add(kind)
            if kind == _SERVER_NAME:
                name = _host_name(content)
    except _MalformedError:
        return _MALFORMED
    return Scan(server_name=name) if name is not None else _NO_SNI


def _host_name(extension: _Cursor) -> str | None:
    """The one `host_name` of a `server_name` extension; another name type is skipped."""
    names = extension.vector(2)
    if not extension.done or names.done:
        raise _MalformedError
    found: str | None = None
    while not names.done:
        name_type = names.number(1)
        value = names.vector(2)
        raw = value.take(value.length)
        if name_type != _HOST_NAME:
            continue
        if found is not None:  # RFC 6066 §3: no two names of one type
            raise _MalformedError
        found = _checked_name(raw)
    return found


def _checked_name(raw: bytes) -> str:
    """A DNS host name in ASCII, without a trailing dot (RFC 6066 §3), in lower case."""
    if not 0 < len(raw) <= _MAX_NAME or any(byte not in _NAME_BYTES for byte in raw):
        raise _MalformedError
    name = raw.decode("ascii").lower()
    if any(not label for label in name.split(".")):
        raise _MalformedError
    return name


async def read_client_hello(
    source: ByteSource,
    *,
    max_bytes: int = CLIENT_HELLO_MAX_BYTES,
    timeout_s: float = CLIENT_HELLO_TIMEOUT_S,
) -> ClientHello:
    """A phone's ClientHello, within `max_bytes` and `timeout_s`. Reads only what the records
    declare, so the bytes that follow stay unread for the splice. Never raises for what it reads."""
    data = b""
    try:
        async with asyncio.timeout(timeout_s):
            while True:
                scan = scan_client_hello(data, max_bytes=max_bytes)
                if not scan.need:
                    return ClientHello(data, scan.server_name, scan.refusal)
                data += await source.readexactly(scan.need)
    except TimeoutError:
        return ClientHello(data, None, RelayCloseReason.CLIENT_HELLO_TIMEOUT)
    except asyncio.IncompleteReadError as ended:
        return ClientHello(data + ended.partial, None, RelayCloseReason.MALFORMED_CLIENT_HELLO)
    except OSError:
        return ClientHello(data, None, RelayCloseReason.MALFORMED_CLIENT_HELLO)
