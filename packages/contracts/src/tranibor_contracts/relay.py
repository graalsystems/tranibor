# SPDX-License-Identifier: Apache-2.0
"""The relay: `/relay/v1`, protocol 1 (ADR 0230).

A personal installation publishes one port, on `127.0.0.1` (ADR 0228 §7). To reach it from a phone,
its relay agent dials out to a relay, which routes a phone's TLS connection to it by SNI and never
terminates that TLS. Two programs nobody upgrades together speak this contract: the relay server and
the installation's agent (`packages/sni-relay`), so what both must agree on is here, and nowhere
else:

- the pairing: a device code the agent asks for (`schemas/relay-pairing-request.schema.json`) and
  the relay's answer, pending, then approved with the id the relay drew
  (`schemas/relay-pairing-answer.schema.json`);
- the control channel: one WSS connection the agent opens, a hello signed as the outposts' uplink
  signs a request (`schemas/relay-hello.schema.json`), and, for each phone connection, the order to
  dial back (`schemas/relay-open-stream.schema.json`), which names no host and no port;
- the data: one dial-back connection per phone connection, opened with a single-use ticket;
- the one line the relay keeps per stream (`schemas/relay-metadata-line.schema.json`);
- the bounds and the signatures, which `relay.yaml` describes and the constants below repeat.

The five schemas are closed, and so are their models: the agent refuses an order to dial back that
holds any field the schema does not describe, and the relay refuses a pairing request that names an
id. What the relay sends besides them (the challenge, the welcome, a close, a problem) is read
leniently: an agent of protocol N-1 meets them before the relay knows its protocol. This module
holds no cryptography: whoever signs brings its own library; the helpers here only compute what a
signature binds, and draw an id.
"""

from __future__ import annotations

import secrets
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, IPvAnyAddress, model_validator

from .outpost import (
    CALL_SIGNATURE_PATTERN,
    DAEMON_VERSION_PATTERN,
    PUBLIC_KEY_PATTERN,
    SIGNATURE_ALGORITHM,
    body_sha256,
)
from .workflow import Strict

# ───────────────────────── protocol 1: names and bounds ─────────────────────────

#: The protocol this contract describes; `relay.yaml` is its document (`info.version: v1`).
RELAY_PROTOCOL: Final = 1
#: The prefix every path of the relay's own host is served under.
RELAY_PREFIX: Final = "/relay/v1"
#: How many protocols a relay serves: its own, N, and the one before, N-1. An older hello is refused
#: with a problem naming the minimum (ADR 0230 §2).
PROTOCOLS_SERVED: Final = 2

#: A pairing request and a pairing poll are signed by the agent's key: the header that carries the
#: signature, the `typ` of its protected header, and its claims (the outposts' enrolment's, ADR 0148 §4).
SIGNATURE_HEADER: Final = "Relay-Signature"
REQUEST_SIGNATURE_TYPE: Final = "choregos-relay+jwt"
REQUEST_SIGNATURE_CLAIMS: Final = ("htm", "htu", "body_sha256", "iat")
#: A control hello is signed by the agent's key, with the id as `kid`: it binds the control URL, the
#: id, the protocol, the nonce of this connection's challenge and `iat`.
HELLO_SIGNATURE_TYPE: Final = "choregos-relay-hello+jwt"
HELLO_SIGNATURE_CLAIMS: Final = ("htu", "id", "protocol", "nonce", "iat")
#: A dial-back presents its ticket in this header, and asks for this upgrade.
TICKET_HEADER: Final = "Relay-Ticket"
STREAM_UPGRADE: Final = "choregos-relay-stream"

#: `iat` is refused when it is further than this from the relay's clock, either way.
IAT_WINDOW_S: Final = 60
#: A control connection whose hello has not arrived this long after its challenge is closed.
HELLO_TIMEOUT_S: Final = 10
#: The relay pings a control session this often; a session that misses `MAX_MISSED_PONGS` is closed.
PING_INTERVAL_S: Final = 25
MAX_MISSED_PONGS: Final = 2
#: A ticket opens one dial-back, for one stream, within this many seconds of its order.
TICKET_TTL_S: Final = 10
#: A device code lives this long; the agent polls no more often than every `PAIRING_INTERVAL_S`.
PAIRING_TTL_S: Final = 600
PAIRING_INTERVAL_S: Final = 5
#: The relay reads a phone's ClientHello within these bounds, or closes the connection (ADR 0230 §4).
CLIENT_HELLO_MAX_BYTES: Final = 16_384
CLIENT_HELLO_TIMEOUT_S: Final = 5
#: The most one control message weighs, either way.
MAX_CONTROL_MESSAGE_BYTES: Final = 4_096
#: A body above its verb's cap is refused before it is parsed.
MAX_BODY_BYTES: Final[dict[str, int]] = {"pairing": 4_096, "pairing_poll": 4_096}

#: An installation's id: 60 random bits, drawn by the relay, written as 12 lowercase base32
#: characters (RFC 4648's alphabet). Nobody chooses one (ADR 0230 §6).
ID_BITS: Final = 60
ID_LENGTH: Final = 12
_BASE32: Final = "abcdefghijklmnopqrstuvwxyz234567"

# ───────────────────────── shapes shared by the schemas ─────────────────────────

RELAY_ID_PATTERN: Final = r"^[a-z2-7]{12}$"
#: `<id>.<relay-domain>`: the one name a phone's ClientHello may carry for an installation.
RELAY_HOSTNAME_PATTERN: Final = r"^[a-z2-7]{12}(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
MAX_HOSTNAME_LENGTH: Final = 253
#: A stream's id: opaque, and safe as one path segment.
STREAM_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{22,64}$"
#: A ticket, a nonce and a device code: random, base64url without padding, 256 bits at least for
#: what opens something, 128 for a nonce.
TICKET_PATTERN: Final = r"^[A-Za-z0-9_-]{43,128}$"
NONCE_PATTERN: Final = r"^[A-Za-z0-9_-]{22,128}$"
DEVICE_CODE_PATTERN: Final = r"^[A-Za-z0-9_-]{43,128}$"
#: What the owner types in their relay account: eight letters of RFC 8628 §6.1's consonants.
USER_CODE_PATTERN: Final = r"^[BCDFGHJKLMNPQRSTVWXZ]{4}-[BCDFGHJKLMNPQRSTVWXZ]{4}$"
VERIFICATION_URI_PATTERN: Final = r"^https://[^\s/@?#]+(/[^\s?#]*)?$"

RelayId = Annotated[str, Field(pattern=RELAY_ID_PATTERN)]
RelayHostname = Annotated[str, Field(pattern=RELAY_HOSTNAME_PATTERN, max_length=MAX_HOSTNAME_LENGTH)]
StreamId = Annotated[str, Field(pattern=STREAM_ID_PATTERN)]
Ticket = Annotated[str, Field(pattern=TICKET_PATTERN)]
Nonce = Annotated[str, Field(pattern=NONCE_PATTERN)]
DeviceCode = Annotated[str, Field(pattern=DEVICE_CODE_PATTERN)]
UserCode = Annotated[str, Field(pattern=USER_CODE_PATTERN)]
AgentVersion = Annotated[str, Field(pattern=DAEMON_VERSION_PATTERN)]
PublicKey = Annotated[str, Field(pattern=PUBLIC_KEY_PATTERN)]
HelloSignature = Annotated[str, Field(pattern=CALL_SIGNATURE_PATTERN, max_length=2048)]
Protocol = Annotated[int, Field(ge=1)]


class RelayPairingStatus(StrEnum):
    """Where a pairing stands, as the relay answers it."""

    #: The owner has not entered the user code yet; the agent polls again.
    PENDING = "pending"
    #: The owner entered it: the relay drew an id and bound it to their account and the agent's key.
    APPROVED = "approved"
    #: The owner refused it.
    DENIED = "denied"
    #: Nobody entered the code before it expired; the agent asks for another.
    EXPIRED = "expired"


class RelayProblemCode(StrEnum):
    """The `code` of every problem of the relay's HTTP paths (`relay.yaml`)."""

    #: 422: the body is not what the path's schema describes (a pairing request naming an id is one).
    INVALID_REQUEST = "invalid_request"
    #: 413: the body is above its cap, refused before it is parsed.
    BODY_TOO_LARGE = "body_too_large"
    #: 401: no signature, one that does not verify, or one under a key the relay does not know.
    INVALID_SIGNATURE = "invalid_signature"
    #: 401: the signature binds another method, URL or body.
    REQUEST_MISMATCH = "request_mismatch"
    #: 401: `iat` is more than 60 seconds from the relay's clock.
    STALE_IAT = "stale_iat"
    #: 426: the agent's protocol is below the relay's minimum; the problem names the window.
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    #: 404: no pairing under this device code for this key.
    UNKNOWN_DEVICE_CODE = "unknown_device_code"
    #: 401: the ticket of a dial-back is unknown, used, expired or another stream's; the relay does
    #: not say which.
    TICKET_REFUSED = "ticket_refused"
    #: 429: too many requests; `retry_after` says when to come back.
    RATE_LIMITED = "rate_limited"


#: The HTTP status each problem code travels under.
PROBLEM_STATUS: Final[dict[RelayProblemCode, int]] = {
    RelayProblemCode.INVALID_SIGNATURE: 401,
    RelayProblemCode.REQUEST_MISMATCH: 401,
    RelayProblemCode.STALE_IAT: 401,
    RelayProblemCode.TICKET_REFUSED: 401,
    RelayProblemCode.UNKNOWN_DEVICE_CODE: 404,
    RelayProblemCode.BODY_TOO_LARGE: 413,
    RelayProblemCode.INVALID_REQUEST: 422,
    RelayProblemCode.PROTOCOL_UNSUPPORTED: 426,
    RelayProblemCode.RATE_LIMITED: 429,
}


class RelayCloseCode(StrEnum):
    """Why the relay closes a control session, or refuses its hello. A refused hello leaves the live
    session of its id as it was (ADR 0230 §3)."""

    #: A message that is not one this protocol describes, or above its size.
    INVALID_MESSAGE = "invalid_message"
    #: No hello within `HELLO_TIMEOUT_S` of the challenge.
    HELLO_TIMEOUT = "hello_timeout"
    #: The hello's protocol is below the minimum; the message names the window.
    PROTOCOL_UNSUPPORTED = "protocol_unsupported"
    #: The relay has no installation under this id: the agent pairs again.
    UNKNOWN_ID = "unknown_id"
    #: The signature does not verify under the id's key, or binds another URL, id or protocol.
    INVALID_SIGNATURE = "invalid_signature"
    #: The signature binds a nonce other than this connection's challenge.
    STALE_NONCE = "stale_nonce"
    #: `iat` is more than 60 seconds from the relay's clock.
    STALE_IAT = "stale_iat"
    #: The id is suspended: its ClientHellos are closed and its hello refused (ADR 0230 §8).
    SUSPENDED = "suspended"
    #: A newer session of the same id, validly signed, took this one's place.
    REPLACED = "replaced"
    #: Too many hellos from this id or this address.
    RATE_LIMITED = "rate_limited"


class RelayCloseReason(StrEnum):
    """Why a phone's connection ended, as its metadata line says. Every connection the relay accepts
    ends with one line, routed or not (ADR 0230 §7)."""

    #: The phone closed it.
    CLIENT_CLOSED = "client_closed"
    #: The installation's side closed it.
    INSTALLATION_CLOSED = "installation_closed"
    #: Nothing crossed it for the relay's idle time.
    IDLE_TIMEOUT = "idle_timeout"
    #: The ClientHello named no server.
    NO_SNI = "no_sni"
    #: The ClientHello could not be read.
    MALFORMED_CLIENT_HELLO = "malformed_client_hello"
    #: The ClientHello was longer than `CLIENT_HELLO_MAX_BYTES`.
    CLIENT_HELLO_TOO_LARGE = "client_hello_too_large"
    #: The ClientHello did not arrive within `CLIENT_HELLO_TIMEOUT_S`.
    CLIENT_HELLO_TIMEOUT = "client_hello_timeout"
    #: The server name is not `<id>.<relay-domain>`.
    OUTSIDE_DOMAIN = "outside_domain"
    #: The id is not one the relay knows.
    UNKNOWN_ID = "unknown_id"
    #: The id is suspended.
    SUSPENDED = "suspended"
    #: The installation has no control session: closed, nothing stored (ADR 0230 §9).
    NO_CONTROL_SESSION = "no_control_session"
    #: The agent did not dial back within `TICKET_TTL_S`.
    DIAL_BACK_TIMEOUT = "dial_back_timeout"
    #: Past a limit of the installation (ADR 0230 §8).
    LIMIT_CONCURRENT_STREAMS = "limit_concurrent_streams"
    LIMIT_STREAMS_PER_MINUTE = "limit_streams_per_minute"
    LIMIT_BYTES_PER_MONTH = "limit_bytes_per_month"
    #: Past the limit of the client's address, checked before anything is read.
    LIMIT_CLIENT_CONNECTIONS_PER_MINUTE = "limit_client_connections_per_minute"
    #: The relay stopped.
    RELAY_SHUTDOWN = "relay_shutdown"
    #: Anything else: a socket error on either side.
    ERROR = "error"


#: The reasons a connection ends before an id is known: their line names no installation.
UNROUTED: Final = frozenset(
    {
        RelayCloseReason.NO_SNI,
        RelayCloseReason.MALFORMED_CLIENT_HELLO,
        RelayCloseReason.CLIENT_HELLO_TOO_LARGE,
        RelayCloseReason.CLIENT_HELLO_TIMEOUT,
        RelayCloseReason.OUTSIDE_DOMAIN,
        RelayCloseReason.UNKNOWN_ID,
        RelayCloseReason.LIMIT_CLIENT_CONNECTIONS_PER_MINUTE,
    }
)


class Lenient(BaseModel):
    """What the relay sends besides the five closed schemas: a field this version does not know is
    ignored, so that an agent of protocol N-1 reads a later relay's challenge, welcome and close."""

    model_config = ConfigDict(extra="ignore")


class RelayProtocolWindow(Strict):
    """The protocols a relay serves: its own (`current`) and the one before (`minimum`)."""

    current: Protocol
    minimum: Protocol


# ───────────────────────── the pairing ─────────────────────────


class RelayPairingRequest(Strict):
    """`POST /relay/v1/pairing`: the agent's new Ed25519 key, and nothing that names an id. The
    request is signed with the key it pairs (ADR 0230 §6)."""

    protocol: Protocol
    agent_version: AgentVersion
    #: Ed25519, raw 32 bytes, base64url without padding: verifies every hello of this installation.
    signing_key: PublicKey


class RelayPairingPoll(Strict):
    """`POST /relay/v1/pairing/poll`: where the pairing of this device code stands. Signed by the key
    the device code was issued to."""

    protocol: Protocol
    device_code: DeviceCode


#: What each status of a pairing answer carries, and nothing else.
_PENDING: Final = ("device_code", "user_code", "verification_uri", "expires_at", "interval_s")
_APPROVED: Final = ("id", "hostname")


class RelayPairingAnswer(Strict):
    """The relay's answer to a pairing request (`pending`) and to each poll of it."""

    status: RelayPairingStatus
    server_time: datetime
    protocol: RelayProtocolWindow
    #: `pending`: what the agent polls with; a secret of the agent, never shown.
    device_code: DeviceCode | None = None
    #: `pending`: what the agent shows the owner, who enters it in their relay account.
    user_code: UserCode | None = None
    #: `pending`: where the owner enters it.
    verification_uri: Annotated[str, Field(pattern=VERIFICATION_URI_PATTERN, max_length=2000)] | None = None
    #: `pending`: after it, the code is `expired`.
    expires_at: datetime | None = None
    #: `pending`: the agent polls no more often.
    interval_s: Annotated[int, Field(ge=PAIRING_INTERVAL_S, le=60)] | None = None
    #: `approved`: the id the relay drew.
    id: RelayId | None = None
    #: `approved`: `<id>.<relay-domain>`, the installation's public name.
    hostname: RelayHostname | None = None

    @model_validator(mode="after")
    def _the_status_says_what_it_carries(self) -> Self:
        carried = {name for name in (*_PENDING, *_APPROVED) if getattr(self, name) is not None}
        wanted: set[str] = set()
        if self.status is RelayPairingStatus.PENDING:
            wanted = set(_PENDING)
        elif self.status is RelayPairingStatus.APPROVED:
            wanted = set(_APPROVED)
        if carried != wanted:
            raise ValueError(
                f"a `{self.status.value}` pairing carries {', '.join(sorted(wanted)) or 'nothing more'}"
            )
        if self.id is not None and self.hostname is not None and not self.hostname.startswith(self.id + "."):
            raise ValueError("the hostname is the id followed by the relay's domain")
        return self


# ───────────────────────── the control channel ─────────────────────────


class RelayChallenge(Lenient):
    """The relay's first message on a control connection: the nonce the hello must bind."""

    type: Literal["challenge"]
    nonce: Nonce
    server_time: datetime
    protocol: RelayProtocolWindow


class RelayHello(Strict):
    """The agent's answer to the challenge: its id and its signature over this connection's nonce."""

    type: Literal["hello"]
    protocol: Protocol
    agent_version: AgentVersion
    id: RelayId
    #: A compact JWS by the installation's Ed25519 key (`x-hello-signature` of `relay.yaml`).
    signature: HelloSignature


class RelayWelcome(Lenient):
    """The relay took the hello: this session is the id's from now on, replacing any other."""

    type: Literal["welcome"]
    id: RelayId
    hostname: RelayHostname
    server_time: datetime
    protocol: RelayProtocolWindow


class RelayOpenStream(Strict):
    """The order to dial back for one phone connection. It names no host and no port: the agent
    connects only to the relay's own host, and splices to the one target its own configuration
    names (ADR 0230 §3). An order holding any other field is refused."""

    type: Literal["open_stream"]
    stream: StreamId
    #: Single use, bound to this stream, valid `TICKET_TTL_S`.
    ticket: Ticket
    #: The phone's address, for the PROXY protocol v2 header the agent writes before the bytes.
    client_ip: IPvAnyAddress


class RelayClose(Lenient):
    """The relay refuses a hello, or ends a session, and closes the connection."""

    type: Literal["close"]
    code: str
    server_time: datetime
    #: On `protocol_unsupported`: the window the relay serves.
    protocol: RelayProtocolWindow | None = None
    detail: Annotated[str, Field(max_length=2000)] | None = None

    @property
    def known_code(self) -> RelayCloseCode | None:
        return RelayCloseCode(self.code) if self.code in set(RelayCloseCode) else None


class RelayProblem(Lenient):
    """A problem of the relay's HTTP paths (RFC 9457) with its `code` and the relay's time."""

    type: str = "about:blank"
    title: str
    status: int
    code: str
    server_time: datetime
    detail: str | None = None
    retry_after: int | None = None
    #: On `protocol_unsupported`: the window the relay serves.
    protocol: RelayProtocolWindow | None = None

    @property
    def known_code(self) -> RelayProblemCode | None:
        return RelayProblemCode(self.code) if self.code in set(RelayProblemCode) else None


# ───────────────────────── the metadata line ─────────────────────────


class RelayMetadataLine(Strict):
    """The one line the relay keeps of a phone's connection, and nothing else: no server name, no
    ALPN, no cipher, no byte of the stream (ADR 0230 §7). Validated before it is written."""

    #: The installation's id; null when the connection ended before one was known (`UNROUTED`).
    install_id: RelayId | None
    started_at: datetime
    ended_at: datetime
    client_ip: IPvAnyAddress
    #: Bytes from the phone towards the installation, the ClientHello included.
    bytes_in: Annotated[int, Field(ge=0)]
    #: Bytes from the installation towards the phone.
    bytes_out: Annotated[int, Field(ge=0)]
    duration_ms: Annotated[int, Field(ge=0)]
    close_reason: RelayCloseReason

    @model_validator(mode="after")
    def _a_line_is_consistent(self) -> Self:
        if self.ended_at < self.started_at:
            raise ValueError("a connection ends after it starts")
        if (self.install_id is None) != (self.close_reason in UNROUTED):
            raise ValueError("a line names its installation exactly when the connection was routed to one")
        return self


# ───────────────────────── ids, windows and what a signature binds ─────────────────────────


def encode_relay_id(value: int) -> str:
    """The 12 characters of an id of `ID_BITS` bits, most significant first."""
    if not 0 <= value < 1 << ID_BITS:
        raise ValueError(f"an id is a number of {ID_BITS} bits")
    return "".join(_BASE32[(value >> shift) & 0x1F] for shift in range(ID_BITS - 5, -1, -5))


def decode_relay_id(relay_id: str) -> int:
    """The number an id writes; refused when it is not 12 lowercase base32 characters."""
    if len(relay_id) != ID_LENGTH or any(char not in _BASE32 for char in relay_id):
        raise ValueError("an id is 12 lowercase base32 characters")
    value = 0
    for char in relay_id:
        value = value << 5 | _BASE32.index(char)
    return value


def new_relay_id() -> str:
    """An id of 60 bits from the system's random source: what the relay draws at a pairing."""
    return encode_relay_id(secrets.randbits(ID_BITS))


def protocol_window(current: int = RELAY_PROTOCOL) -> RelayProtocolWindow:
    """The window a relay of protocol `current` serves: N and N-1, never below 1."""
    return RelayProtocolWindow(current=current, minimum=max(1, current - PROTOCOLS_SERVED + 1))


def is_served(protocol: int, window: RelayProtocolWindow) -> bool:
    """Whether an agent of `protocol` is served; an older one is refused `protocol_unsupported`."""
    return window.minimum <= protocol <= window.current


def request_signature_claims(*, method: str, url: str, body: bytes, iat: int) -> dict[str, Any]:
    """The claims of a pairing request's or a pairing poll's signature (`Relay-Signature`)."""
    return {"htm": method.upper(), "htu": url, "body_sha256": body_sha256(body), "iat": iat}


def request_signature_header() -> dict[str, str]:
    """The protected header of a pairing request's or poll's signature: no `kid`, the relay knows the
    key from the body (a request) or from the device code (a poll)."""
    return {"alg": SIGNATURE_ALGORITHM, "typ": REQUEST_SIGNATURE_TYPE}


def hello_signature_claims(*, url: str, relay_id: str, protocol: int, nonce: str, iat: int) -> dict[str, Any]:
    """The claims of a hello's signature: this relay's control URL, this id, this protocol, this
    connection's nonce, and the time by the agent's clock."""
    return {"htu": url, "id": relay_id, "protocol": protocol, "nonce": nonce, "iat": iat}


def hello_signature_header(relay_id: str) -> dict[str, str]:
    """The protected header of a hello's signature: `kid` is the id."""
    return {"alg": SIGNATURE_ALGORITHM, "typ": HELLO_SIGNATURE_TYPE, "kid": relay_id}
