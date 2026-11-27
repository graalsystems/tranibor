# SPDX-License-Identifier: Apache-2.0
"""The ClientHello parser never raises on arbitrary bytes and never reads past its bound (ADR 0230 §4).

`hypothesis` is not a dependency of the workspace, so the fuzz is a parametrised test over byte
strings drawn from a seeded generator: random bytes, random TLS-shaped records, and mutations (bit
flips, truncations, rewritten length fields) of real ClientHellos made by Python's own TLS stack.
"""

from __future__ import annotations

import asyncio
import random
import ssl
import struct

import pytest
from tranibor_contracts.relay import CLIENT_HELLO_MAX_BYTES, RelayCloseReason
from tranibor_sni_relay.sni import ClientHello, Scan, read_client_hello, scan_client_hello

from ._kit import client_hello

NAME = "kq7mz3xw2pa5.relay.test"
HELLO = client_hello(NAME)
#: The bounds the fuzz runs under: the contract's, and two tight ones that real ClientHellos overrun.
BOUNDS = (CLIENT_HELLO_MAX_BYTES, 512, 64)


class Source:
    """A phone that sends `data` and then closes; it counts what the reader asked for."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.read = 0

    async def readexactly(self, n: int) -> bytes:
        assert n > 0, "the reader asks for at least one byte"
        chunk = self._data[self.read : self.read + n]
        self.read += len(chunk)
        if len(chunk) < n:
            raise asyncio.IncompleteReadError(chunk, n)
        return chunk


class Endless:
    """A phone that never stops sending handshake records of the largest size."""

    def __init__(self) -> None:
        self.read = 0
        self._record = b"\x16\x03\x01\x40\x00" + b"\x01\x00\x40\x00" + b"\x00" * (16384 - 4)

    async def readexactly(self, n: int) -> bytes:
        stream = self._record * (2 + (self.read + n) // len(self._record))
        chunk = stream[self.read : self.read + n]
        self.read += n
        return chunk


def _records(body: bytes, size: int) -> bytes:
    """`body` as one handshake message split into TLS records of `size` bytes."""
    message = b"\x01" + len(body).to_bytes(3, "big") + body
    return b"".join(
        b"\x16\x03\x01" + len(message[i : i + size]).to_bytes(2, "big") + message[i : i + size]
        for i in range(0, len(message), size)
    )


def _hello_body(extensions: bytes) -> bytes:
    return (
        b"\x03\x03"
        + b"\x00" * 32
        + b"\x00"
        + b"\x00\x02\x13\x01"
        + b"\x01\x00"
        + struct.pack("!H", len(extensions))
        + extensions
    )


def _sni(*names: tuple[int, bytes]) -> bytes:
    entries = b"".join(bytes([kind]) + struct.pack("!H", len(name)) + name for kind, name in names)
    listed = struct.pack("!H", len(entries)) + entries
    return b"\x00\x00" + struct.pack("!H", len(listed)) + listed


# ───────────────────────── what a real ClientHello says ─────────────────────────


def test_the_server_name_of_a_real_client_hello() -> None:
    assert scan_client_hello(HELLO) == Scan(server_name=NAME)
    assert scan_client_hello(client_hello(None)).refusal is RelayCloseReason.NO_SNI


@pytest.mark.parametrize("size", [1, 7, 100, 16384])
def test_a_client_hello_split_across_records_is_read_whole(size: int) -> None:
    body = HELLO[9:]  # past the record header and the handshake header
    assert scan_client_hello(_records(body, size)) == Scan(server_name=NAME)


async def test_the_reader_reads_the_client_hello_and_not_one_byte_more() -> None:
    source = Source(HELLO + b"\x17\x03\x03 the stream that follows")
    read = await read_client_hello(source)
    assert read == ClientHello(HELLO, NAME, None)
    assert source.read == len(HELLO)


@pytest.mark.parametrize(
    ("extensions", "expected"),
    [
        (_sni((0, b"A.Example.Test")), Scan(server_name="a.example.test")),
        (_sni((1, b"opaque"), (0, b"x.test")), Scan(server_name="x.test")),
        (_sni((1, b"opaque")), Scan(refusal=RelayCloseReason.NO_SNI)),
        (b"", Scan(refusal=RelayCloseReason.NO_SNI)),
        (_sni((0, b"a.test"), (0, b"b.test")), Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (_sni((0, b"a.test")) * 2, Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (_sni((0, b"a..test")), Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (_sni((0, b"a.test.")), Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (_sni((0, b"a_b.test")), Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (_sni((0, b"")), Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (_sni((0, b"a" * 254)), Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (b"\x00\x00\x00\x00", Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
    ],
    ids=[
        "lower-cased",
        "other-type-skipped",
        "only-other-type",
        "no-extension",
        "two-host-names",
        "duplicate-extension",
        "empty-label",
        "trailing-dot",
        "underscore",
        "empty-name",
        "too-long",
        "empty-list",
    ],
)
def test_the_server_name_extension_is_read_by_the_rfc(extensions: bytes, expected: Scan) -> None:
    assert scan_client_hello(_records(_hello_body(extensions), 16384)) == expected


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"", Scan(need=5)),
        (b"\x16\x03", Scan(need=3)),
        (b"\x16\x03\x01\x00\x10", Scan(need=16)),
        (b"\x17\x03\x01\x00\x10", Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (b"\x16\x02\x00\x00\x10", Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (b"\x16\x03\x01\x00\x00", Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (b"\x16\x03\x01\x40\x01", Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (b"\x16\x03\x01\x00\x04\x02\x00\x00\x00", Scan(refusal=RelayCloseReason.MALFORMED_CLIENT_HELLO)),
        (b"\x16\x03\x01\x00\x04\x01\x00\x40\x00", Scan(refusal=RelayCloseReason.CLIENT_HELLO_TOO_LARGE)),
        (b"\x16\x03\x01\x00\x04\x01\xff\xff\xff", Scan(refusal=RelayCloseReason.CLIENT_HELLO_TOO_LARGE)),
    ],
    ids=[
        "nothing",
        "half-header",
        "record-body",
        "not-handshake",
        "not-tls",
        "empty-record",
        "record-too-big",
        "not-client-hello",
        "hello-cannot-fit",
        "hello-huge",
    ],
)
def test_the_records_say_what_to_read_next(data: bytes, expected: Scan) -> None:
    assert scan_client_hello(data) == expected


# ───────────────────────── the bounds ─────────────────────────


async def test_an_endless_handshake_is_refused_without_reading_past_the_bound() -> None:
    source = Endless()
    read = await read_client_hello(source)
    assert read.refusal is RelayCloseReason.CLIENT_HELLO_TOO_LARGE
    assert source.read <= CLIENT_HELLO_MAX_BYTES


async def test_a_client_hello_that_does_not_arrive_in_time_is_refused() -> None:
    reader = asyncio.StreamReader()
    reader.feed_data(HELLO[:40])
    read = await read_client_hello(reader, timeout_s=0.05)
    assert read.refusal is RelayCloseReason.CLIENT_HELLO_TIMEOUT
    assert read.data == HELLO[:5]


# ───────────────────────── the fuzz ─────────────────────────


def _mutations(seed: int) -> list[bytes]:
    rng = random.Random(seed)
    cases: list[bytes] = []
    real = [HELLO, client_hello(None), client_hello("a" * 63 + ".relay.test"), _records(HELLO[9:], 50)]
    for _ in range(40):
        data = bytearray(rng.choice(real))
        action = rng.randrange(5)
        if action == 0:  # flip bits
            for _ in range(rng.randrange(1, 8)):
                data[rng.randrange(len(data))] ^= 1 << rng.randrange(8)
        elif action == 1:  # truncate
            del data[rng.randrange(len(data)) :]
        elif action == 2:  # rewrite a two-byte length somewhere
            at = rng.randrange(len(data) - 1)
            data[at : at + 2] = rng.choice(
                [b"\x00\x00", b"\xff\xff", b"\x40\x00", b"\x40\x01", rng.randbytes(2)]
            )
        elif action == 3:  # insert garbage
            at = rng.randrange(len(data))
            data[at:at] = rng.randbytes(rng.randrange(1, 64))
        else:  # rewrite a three-byte length (the handshake's)
            at = rng.randrange(len(data) - 2)
            data[at : at + 3] = rng.choice([b"\xff\xff\xff", b"\x00\x00\x00", rng.randbytes(3)])
        cases.append(bytes(data))
    for _ in range(20):  # random bytes, TLS-shaped or not
        prefix = rng.choice(
            [b"", b"\x16\x03\x01", b"\x16\x03\x03" + rng.randbytes(2), b"\x16\x03\x01\x00\x30\x01"]
        )
        cases.append(prefix + rng.randbytes(rng.randrange(0, 20_000)))
    return cases


def _check(scan: Scan, data: bytes, bound: int) -> None:
    assert [scan.need > 0, scan.server_name is not None, scan.refusal is not None].count(True) == 1
    if scan.need:
        assert len(data) + scan.need <= bound, "the parser never asks for a byte past its bound"


@pytest.mark.parametrize("seed", range(25))
@pytest.mark.parametrize("bound", BOUNDS)
async def test_the_parser_never_raises_and_never_reads_past_its_bound(seed: int, bound: int) -> None:
    for data in _mutations(seed):
        for prefix in (data, data[:bound], data[: len(data) // 2]):
            _check(scan_client_hello(prefix, max_bytes=bound), prefix, bound)
        source = Source(data)
        read = await read_client_hello(source, max_bytes=bound)
        assert source.read <= bound, "the reader never reads past its bound"
        assert read.data == data[: source.read], "what was read is kept, to be forwarded first"
        assert (read.server_name is None) != (read.refusal is None)


def test_the_python_client_hello_is_a_tls_one() -> None:
    """The fuzz starts from what a real client sends, not from a stand-in."""
    assert HELLO[0] == 0x16 and HELLO[5] == 0x01
    assert ssl.HAS_SNI
