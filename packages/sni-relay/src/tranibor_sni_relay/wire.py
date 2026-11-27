# SPDX-License-Identifier: Apache-2.0
"""What crosses the relay's own host and the splice: bounded HTTP/1.1 heads, problems, and the pump.

The relay's own host serves four paths (`relay.yaml`): two small POSTs, one WebSocket upgrade and one
upgrade to `choregos-relay-stream`. That last one is not something an ASGI server can hand over, so
the relay reads its requests' heads itself, within `MAX_HEAD_BYTES` and `HEAD_TIMEOUT_S`, one request
per connection, and answers with `Connection: close`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from http import HTTPStatus
from typing import Any, Final

from tranibor_contracts.relay import PROBLEM_STATUS, RelayProblemCode, RelayProtocolWindow

#: The most a request's head (request line and headers) weighs, and how long it may take to arrive.
MAX_HEAD_BYTES: Final = 8_192
HEAD_TIMEOUT_S: Final = 10.0
#: What the splice reads at once.
CHUNK: Final = 65_536
_PROBLEM_TITLES: Final[dict[RelayProblemCode, str]] = {
    RelayProblemCode.INVALID_REQUEST: "The body is not what this path's schema describes",
    RelayProblemCode.BODY_TOO_LARGE: "The body is above this path's cap",
    RelayProblemCode.INVALID_SIGNATURE: "The signature is refused",
    RelayProblemCode.REQUEST_MISMATCH: "The signature binds another request",
    RelayProblemCode.STALE_IAT: "The signature's time is more than 60 seconds from the relay's",
    RelayProblemCode.PROTOCOL_UNSUPPORTED: "This relay does not serve that protocol",
    RelayProblemCode.UNKNOWN_DEVICE_CODE: "No pairing under this device code",
    RelayProblemCode.TICKET_REFUSED: "The ticket is refused",
    RelayProblemCode.RATE_LIMITED: "Too many requests",
}


class HeadError(Exception):
    """A request head that is too large, too slow, or not HTTP/1.1."""


@dataclass(frozen=True)
class RequestHead:
    """A request's head as it arrived, and what it says."""

    method: str
    path: str
    headers: dict[str, str]
    raw: bytes

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


async def read_head(
    reader: asyncio.StreamReader, *, max_bytes: int = MAX_HEAD_BYTES, timeout_s: float = HEAD_TIMEOUT_S
) -> RequestHead:
    """The head of one request, within bounds; what follows it stays unread. The reader's own limit
    (`limit=` of its server) bounds what it buffers while it looks for the head's end. A header given
    twice is refused, so that no two readings of one request disagree."""
    try:
        async with asyncio.timeout(timeout_s):
            raw = await reader.readuntil(b"\r\n\r\n")
    except TimeoutError as slow:
        raise HeadError("the head did not arrive in time") from slow
    except (asyncio.LimitOverrunError, asyncio.IncompleteReadError, OSError) as broken:
        raise HeadError("the head is too large, or the connection ended before it") from broken
    if len(raw) > max_bytes:
        raise HeadError("the head is too large")
    head = raw[:-4]
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or parts[2] != "HTTP/1.1" or not parts[1].startswith("/"):
        raise HeadError("not an HTTP/1.1 request")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, colon, value = line.partition(":")
        name = name.strip().lower()
        if not colon or not name or name != name.strip() or name in headers:
            raise HeadError("a header is malformed or given twice")
        headers[name] = value.strip()
    return RequestHead(parts[0], parts[1].partition("?")[0], headers, raw)


def response(
    status: int, *, body: bytes = b"", content_type: str | None = None, headers: dict[str, str] | None = None
) -> bytes:
    """One HTTP/1.1 response, closing the connection unless it switches protocols."""
    lines = [f"HTTP/1.1 {status} {HTTPStatus(status).phrase}"]
    every = dict(headers or {})
    if status != HTTPStatus.SWITCHING_PROTOCOLS:
        every.setdefault("Connection", "close")
        every["Content-Length"] = str(len(body))
        if content_type is not None:
            every["Content-Type"] = content_type
    lines += [f"{name}: {value}" for name, value in every.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


def json_response(status: int, document: dict[str, Any]) -> bytes:
    return response(status, body=json.dumps(document).encode(), content_type="application/json")


def iso(moment: float) -> str:
    """A time of the relay's clock as the contract writes it."""
    return datetime.fromtimestamp(moment, UTC).isoformat().replace("+00:00", "Z")


def problem(
    code: RelayProblemCode,
    *,
    now: float,
    window: RelayProtocolWindow | None = None,
    detail: str | None = None,
    retry_after: int | None = None,
) -> bytes:
    """An RFC 9457 problem of the relay's HTTP paths, with its code and the relay's time."""
    status = PROBLEM_STATUS[code]
    document: dict[str, Any] = {
        "type": "about:blank",
        "title": _PROBLEM_TITLES[code],
        "status": status,
        "code": code.value,
        "server_time": iso(now),
    }
    if detail is not None:
        document["detail"] = detail
    if retry_after is not None:
        document["retry_after"] = retry_after
    if window is not None:
        document["protocol"] = window.model_dump()
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return response(
        status, body=json.dumps(document).encode(), content_type="application/problem+json", headers=headers
    )


async def close_quietly(writer: asyncio.StreamWriter) -> None:
    """Closes a connection whatever state it is in."""
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()
    with contextlib.suppress(OSError, RuntimeError, TimeoutError, asyncio.CancelledError):
        async with asyncio.timeout(2):
            await writer.wait_closed()


class Side(StrEnum):
    """Which end of a splice stopped it first."""

    #: The connection the splice was opened for: the phone, at the relay; the relay, at the agent.
    NEAR = "near"
    #: The other one.
    FAR = "far"


class ByteLimitError(Exception):
    """A counter said no more bytes may cross."""


async def pump(
    source: asyncio.StreamReader, sink: asyncio.StreamWriter, counted: Callable[[int], bool] | None = None
) -> None:
    """Copies `source` to `sink` until `source` ends. `counted` sees each chunk's size once it crossed,
    and stops the copy by answering `False`."""
    while chunk := await source.read(CHUNK):
        sink.write(chunk)
        await sink.drain()
        if counted is not None and not counted(len(chunk)):
            raise ByteLimitError


async def splice(
    near: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    far: tuple[asyncio.StreamReader, asyncio.StreamWriter],
    *,
    near_counted: Callable[[int], bool] | None = None,
    far_counted: Callable[[int], bool] | None = None,
    idle_s: float | None = None,
) -> Side | ByteLimitError | TimeoutError | OSError:
    """Bytes both ways, unparsed, until either side ends; then both are closed. Says what stopped it:
    the side that ended first, a limit, the idle time, or a socket error."""
    activity = [asyncio.get_running_loop().time()]

    def touched(counted: Callable[[int], bool] | None) -> Callable[[int], bool]:
        def seen(size: int) -> bool:
            activity[0] = asyncio.get_running_loop().time()
            return counted(size) if counted is not None else True

        return seen

    async def idle() -> None:
        assert idle_s is not None
        while True:
            left = activity[0] + idle_s - asyncio.get_running_loop().time()
            if left <= 0:
                return
            await asyncio.sleep(left)

    inward = asyncio.create_task(pump(near[0], far[1], touched(near_counted)))
    outward = asyncio.create_task(pump(far[0], near[1], touched(far_counted)))
    watch = asyncio.create_task(idle()) if idle_s is not None else None
    tasks = {inward, outward} | ({watch} if watch is not None else set())
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(close_quietly(near[1]), close_quietly(far[1]))
    first = inward if inward in done else outward if outward in done else watch
    assert first is not None
    if first is watch:
        return TimeoutError()
    failure = first.exception()
    if isinstance(failure, ByteLimitError | OSError):
        return failure
    if failure is not None:
        raise failure
    return Side.NEAR if first is inward else Side.FAR
