# SPDX-License-Identifier: Apache-2.0
"""The owner ticket of a personal installation (ADR 0228 §4): a compact JWS signed by the owner key.

`choregos local init` generates an Ed25519 key pair in the installation's data directory, the private half
at mode 0600; the API holds only the public half (`TRANIBOR_OWNER_PUBLIC_KEY_FILE`). `choregos local open`
(or the desktop app, ADR 0229) signs a ticket with `sign_owner_ticket` and opens the console's loopback URL
with the ticket in the fragment, which no server receives or logs; the console posts it to
`POST /auth/owner`, which checks it with `verify_owner_ticket` and takes its `jti` once.

A ticket is `base64url(header).base64url(claims).base64url(signature)` (RFC 7515 compact serialisation):
the header is `{"alg": "EdDSA", "typ": "JWT"}` (RFC 8037), the claims are `aud` (`choregos-owner`), `iat`,
`exp` (no more than 60 seconds after `iat`) and `jti` (random, taken once by the API). Nothing else is
read: no `kid`, no `jku`, no other algorithm, and a `crit` header is refused.

Only the standard library and `cryptography`: the CLI imports this module without any server module
(`packages/core/tests/test_owner_ticket.py` holds it to that).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

#: The only audience a ticket is valid for: no other token of the platform names it.
AUDIENCE = "choregos-owner"
#: The only algorithm a ticket is signed with (RFC 8037: Ed25519 under the name `EdDSA`).
ALGORITHM = "EdDSA"
#: The longest a ticket lives, from `iat` to `exp`.
MAX_LIFETIME_S = 60
#: How far in the future `iat` may be: the signer and the API share a machine, not always one clock
#: (a container under a virtual machine's clock drifts by a second or two).
CLOCK_SKEW_S = 5
#: What a `jti` is: random, URL-safe, long enough not to be guessed, short enough to be a key.
JTI = re.compile(r"[A-Za-z0-9_-]{16,128}")
#: The longest ticket read: the contract's bound on `OwnerTicketExchange.ticket`.
MAX_TICKET_CHARS = 4096


class OwnerTicketRefusedError(ValueError):
    """A ticket that does not open a session. The reason is for the logs: the API never says which."""


@dataclass(frozen=True, slots=True)
class OwnerTicket:
    """What a verified ticket says: its single-use id and the window it is valid in."""

    jti: str
    issued_at: datetime
    expires_at: datetime


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode(text: str) -> bytes:
    if not text or not re.fullmatch(r"[A-Za-z0-9_-]+", text):
        raise OwnerTicketRefusedError("a part of the ticket is not base64url")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError) as error:
        raise OwnerTicketRefusedError("a part of the ticket is not base64url") from error


def _text(key_pem: bytes | str) -> bytes:
    return key_pem.encode("ascii") if isinstance(key_pem, str) else key_pem


def _moment(now: datetime | None) -> datetime:
    moment = now or datetime.now(UTC)
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def generate_owner_key_pair() -> tuple[bytes, bytes]:
    """A new owner key: `(private PEM, PKCS#8 unencrypted; public PEM, SubjectPublicKeyInfo)`.

    What `choregos local init` writes: the private half at mode 0600 beside the installation's data, the
    public half where `TRANIBOR_OWNER_PUBLIC_KEY_FILE` names it.
    """
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private_pem, public_pem


def load_private_key(private_key_pem: bytes | str) -> Ed25519PrivateKey:
    """The owner's private key from its PEM; `ValueError` when it is not an Ed25519 one (never quoted)."""
    try:
        key = serialization.load_pem_private_key(_text(private_key_pem), password=None)
    except (ValueError, TypeError) as error:
        raise ValueError("not a PEM private key") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError(f"the owner key is Ed25519, not {type(key).__name__}")
    return key


def load_public_key(public_key_pem: bytes | str) -> Ed25519PublicKey:
    """The owner's public key from its PEM; `ValueError` when it is not an Ed25519 one."""
    try:
        key = serialization.load_pem_public_key(_text(public_key_pem))
    except (ValueError, TypeError) as error:
        raise ValueError("not a PEM public key") from error
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"the owner key is Ed25519, not {type(key).__name__}")
    return key


def sign_claims(
    private_key_pem: bytes | str, claims: dict[str, Any], header: dict[str, Any] | None = None
) -> str:
    """A compact JWS of `claims`, signed with Ed25519, whatever the claims say.

    `sign_owner_ticket` is the one a surface calls; this is what it is made of, open so that a test can
    write a ticket the API must refuse (another audience, a lifetime too long).
    """
    key = load_private_key(private_key_pem)
    head = _encode(json.dumps(header or {"alg": ALGORITHM, "typ": "JWT"}, separators=(",", ":")).encode())
    body = _encode(json.dumps(claims, separators=(",", ":")).encode())
    signature = key.sign(f"{head}.{body}".encode("ascii"))
    return f"{head}.{body}.{_encode(signature)}"


def sign_owner_ticket(
    private_key_pem: bytes | str, now: datetime | None = None, *, lifetime_s: int = MAX_LIFETIME_S
) -> str:
    """A ticket for `POST /auth/owner`, valid `lifetime_s` seconds (60 at most) from `now`, its own `jti`."""
    if not 0 < lifetime_s <= MAX_LIFETIME_S:
        raise ValueError(f"an owner ticket lives 1 to {MAX_LIFETIME_S} seconds, not {lifetime_s}")
    issued = int(_moment(now).timestamp())
    claims = {"aud": AUDIENCE, "iat": issued, "exp": issued + lifetime_s, "jti": secrets.token_urlsafe(24)}
    return sign_claims(private_key_pem, claims)


def _json_object(raw: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise OwnerTicketRefusedError(f"the ticket's {what} is not JSON") from error
    if not isinstance(value, dict):
        raise OwnerTicketRefusedError(f"the ticket's {what} is not a JSON object")
    return value


def _seconds(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise OwnerTicketRefusedError(f"the ticket's `{name}` is not a whole number of seconds")
    return value


def verify_owner_ticket(ticket: str, public_key_pem: bytes | str, now: datetime | None = None) -> OwnerTicket:
    """Check a ticket against the owner's public key at `now`; `OwnerTicketRefusedError` otherwise.

    The signature first, over the exact bytes received, then the header (`EdDSA`, no `crit`) and the claims:
    `aud` is `choregos-owner`, `exp` is after `now`, `iat` is not more than `CLOCK_SKEW_S` ahead of it,
    `exp - iat` is 1 to 60 seconds, and `jti` is a random id. Taking the `jti` once is the caller's.
    """
    key = load_public_key(public_key_pem)
    if len(ticket) > MAX_TICKET_CHARS or ticket.count(".") != 2:
        raise OwnerTicketRefusedError("not a compact JWS")
    head, body, signature = ticket.split(".")
    try:
        key.verify(_decode(signature), f"{head}.{body}".encode("ascii"))
    except (InvalidSignature, UnicodeEncodeError) as error:
        raise OwnerTicketRefusedError("the signature is not the owner key's") from error
    header = _json_object(_decode(head), "header")
    if header.get("alg") != ALGORITHM:
        raise OwnerTicketRefusedError(f"the ticket is signed with {header.get('alg')!r}, not {ALGORITHM}")
    if "crit" in header:
        raise OwnerTicketRefusedError("the ticket's header names critical extensions")
    claims = _json_object(_decode(body), "claims")
    if claims.get("aud") != AUDIENCE:
        raise OwnerTicketRefusedError("the ticket is for another audience")
    issued, expires = _seconds(claims, "iat"), _seconds(claims, "exp")
    if not 0 < expires - issued <= MAX_LIFETIME_S:
        raise OwnerTicketRefusedError(f"the ticket is valid for {expires - issued} seconds")
    moment = _moment(now).timestamp()
    if expires <= moment:
        raise OwnerTicketRefusedError("the ticket has expired")
    if issued > moment + CLOCK_SKEW_S:
        raise OwnerTicketRefusedError("the ticket is issued in the future")
    jti = claims.get("jti")
    if not isinstance(jti, str) or not JTI.fullmatch(jti):
        raise OwnerTicketRefusedError("the ticket's `jti` is not a random id")
    return OwnerTicket(
        jti=jti,
        issued_at=datetime.fromtimestamp(issued, tz=UTC),
        expires_at=datetime.fromtimestamp(expires, tz=UTC),
    )
