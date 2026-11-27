# SPDX-License-Identifier: Apache-2.0
"""Pairing by device code, and the relay's registry of installations (ADR 0230 §6).

The agent makes its Ed25519 key and asks for a device code (`POST /relay/v1/pairing`, signed with
that key); the owner enters the user code in their relay account; the relay then draws an id of 60
random bits and binds it to the account and the key. Nobody chooses an id: a pairing request has no
field that could name one, and one that names it is refused as an extra field.

The registry here is the relay's state in memory: the installations (id, account, key, suspension)
and the pairings under way. What a deployed relay keeps across restarts is its operator's database,
which this registry's methods describe.
"""

from __future__ import annotations

import hmac
import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import ValidationError
from tranibor_contracts.relay import (
    IAT_WINDOW_S,
    MAX_BODY_BYTES,
    PAIRING_INTERVAL_S,
    PAIRING_TTL_S,
    RELAY_PREFIX,
    RelayPairingAnswer,
    RelayPairingPoll,
    RelayPairingRequest,
    RelayPairingStatus,
    RelayProblemCode,
    RelayProtocolWindow,
    is_served,
    new_relay_id,
    request_signature_claims,
    request_signature_header,
)

from .limits import SlidingWindow
from .signing import SignatureError, decode_public, verified
from .wire import iso, json_response, problem

#: RFC 8628 §6.1's consonants: no vowel, so no word, and nothing a reader confuses.
USER_CODE_ALPHABET: Final = "BCDFGHJKLMNPQRSTVWXZ"
PAIRING_PATH: Final = f"{RELAY_PREFIX}/pairing"
POLL_PATH: Final = f"{RELAY_PREFIX}/pairing/poll"


class PairingError(Exception):
    """A user code the relay does not know, or no longer takes."""


@dataclass
class Installation:
    """An id the relay drew, bound to an account and the agent's public key."""

    id: str
    account: str
    signing_key: str
    public: Ed25519PublicKey
    paired_at: float
    #: Why the id is suspended; `None` while it is not.
    suspended: str | None = None


@dataclass
class Pairing:
    """A device code under way."""

    device_code: str
    user_code: str
    signing_key: str
    public: Ed25519PublicKey
    expires_at: float
    last_request_at: float
    status: RelayPairingStatus = RelayPairingStatus.PENDING
    installation: str | None = None


def _user_code() -> str:
    letters = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(8))
    return f"{letters[:4]}-{letters[4:]}"


class RelayRegistry:
    """The relay's installations and pairings."""

    def __init__(self, *, domain: str, now: Callable[[], float]) -> None:
        self.domain = domain
        self._now = now
        self._installations: dict[str, Installation] = {}
        self._pairings: dict[str, Pairing] = {}
        self._by_user_code: dict[str, str] = {}

    def hostname(self, relay_id: str) -> str:
        return f"{relay_id}.{self.domain}"

    def begin(self, signing_key: str) -> Pairing:
        """A pending pairing for `signing_key`: its device code and the user code the owner enters."""
        public = decode_public(signing_key)
        moment = self._now()
        self._forget_expired(moment)
        user_code = _user_code()
        while user_code in self._by_user_code:
            user_code = _user_code()
        pairing = Pairing(
            device_code=secrets.token_urlsafe(32),
            user_code=user_code,
            signing_key=signing_key,
            public=public,
            expires_at=moment + PAIRING_TTL_S,
            last_request_at=moment,
        )
        self._pairings[pairing.device_code] = pairing
        self._by_user_code[user_code] = pairing.device_code
        return pairing

    def pairing(self, device_code: str) -> Pairing | None:
        found = self._pairings.get(device_code)
        if (
            found is not None
            and found.status is RelayPairingStatus.PENDING
            and self._now() >= found.expires_at
        ):
            found.status = RelayPairingStatus.EXPIRED
        return found

    def _pending(self, user_code: str) -> Pairing:
        device_code = self._by_user_code.get(user_code.strip().upper())
        pairing = self.pairing(device_code) if device_code is not None else None
        if pairing is None or pairing.status is not RelayPairingStatus.PENDING:
            raise PairingError("no pending pairing under this user code")
        return pairing

    def approve(self, user_code: str, account: str) -> Installation:
        """The owner entered `user_code` in `account`: the relay draws an id and binds it."""
        pairing = self._pending(user_code)
        relay_id = new_relay_id()
        while relay_id in self._installations:
            relay_id = new_relay_id()
        installation = Installation(
            id=relay_id,
            account=account,
            signing_key=pairing.signing_key,
            public=pairing.public,
            paired_at=self._now(),
        )
        self._installations[relay_id] = installation
        pairing.status, pairing.installation = RelayPairingStatus.APPROVED, relay_id
        return installation

    def deny(self, user_code: str) -> None:
        self._pending(user_code).status = RelayPairingStatus.DENIED

    def installation(self, relay_id: str) -> Installation | None:
        return self._installations.get(relay_id)

    def installations_of(self, account: str) -> list[Installation]:
        return [each for each in self._installations.values() if each.account == account]

    def suspend(self, relay_id: str, reason: str) -> None:
        self._installations[relay_id].suspended = reason

    def resume(self, relay_id: str) -> None:
        self._installations[relay_id].suspended = None

    def remove(self, relay_id: str) -> None:
        self._installations.pop(relay_id, None)

    def _forget_expired(self, moment: float) -> None:
        """A pairing is forgotten one TTL after it expired: its device code then names nothing."""
        for code in [
            code for code, each in self._pairings.items() if moment > each.expires_at + PAIRING_TTL_S
        ]:
            self._by_user_code.pop(self._pairings.pop(code).user_code, None)


class _RefusalError(Exception):
    """A refusal, carrying the problem it is answered with."""

    def __init__(self, answer: bytes) -> None:
        super().__init__()
        self.answer = answer


class PairingDoor:
    """`POST /relay/v1/pairing` and `POST /relay/v1/pairing/poll`, checked in the contract's order:
    the body's size, the protocol, the schema, the signature, what it binds, `iat`, the rate, then what
    the path refuses."""

    def __init__(
        self,
        registry: RelayRegistry,
        *,
        origin: Callable[[], str],
        window: RelayProtocolWindow,
        verification_uri: str,
        pairings_per_minute: int,
        now: Callable[[], float],
    ) -> None:
        self._registry = registry
        self._origin = origin
        self._window = window
        self._verification_uri = verification_uri
        self._now = now
        self._requests = SlidingWindow(pairings_per_minute, 60, now)

    def answer(self, path: str, body: bytes, signature: str | None, client_ip: str) -> bytes:
        """The response to one pairing request or poll, whole."""
        try:
            if path == PAIRING_PATH:
                return self._request(body, signature, client_ip)
            return self._poll(body, signature)
        except _RefusalError as refusal:
            return refusal.answer

    def _refuse(
        self, code: RelayProblemCode, detail: str | None = None, retry_after: int | None = None
    ) -> _RefusalError:
        window = self._window if code is RelayProblemCode.PROTOCOL_UNSUPPORTED else None
        return _RefusalError(
            problem(code, now=self._now(), window=window, detail=detail, retry_after=retry_after)
        )

    def _judged(self, body: bytes, cap: int) -> Any:
        """The body, once its size and protocol are served: the first two checks."""
        if len(body) > cap:
            raise self._refuse(RelayProblemCode.BODY_TOO_LARGE)
        try:
            document = json.loads(body)
        except ValueError as broken:
            raise self._refuse(RelayProblemCode.INVALID_REQUEST, "the body is not JSON") from broken
        protocol = document.get("protocol") if isinstance(document, dict) else None
        if (
            isinstance(protocol, int)
            and not isinstance(protocol, bool)
            and not is_served(protocol, self._window)
        ):
            raise self._refuse(
                RelayProblemCode.PROTOCOL_UNSUPPORTED,
                f"this relay serves protocols {self._window.minimum} to {self._window.current}",
            )
        return document

    def _signed(self, path: str, body: bytes, signature: str | None, key: Ed25519PublicKey) -> None:
        """The signature, what it binds and `iat`: checks four to six."""
        try:
            claims = verified(signature or "", key, header=request_signature_header())
        except SignatureError as refused:
            raise self._refuse(RelayProblemCode.INVALID_SIGNATURE) from refused
        bound = request_signature_claims(method="POST", url=self._origin() + path, body=body, iat=0)
        if any(claims.get(name) != bound[name] for name in ("htm", "htu", "body_sha256")):
            raise self._refuse(RelayProblemCode.REQUEST_MISMATCH)
        iat = claims.get("iat")
        if not isinstance(iat, int) or isinstance(iat, bool) or abs(self._now() - iat) > IAT_WINDOW_S:
            raise self._refuse(RelayProblemCode.STALE_IAT)

    def _request(self, body: bytes, signature: str | None, client_ip: str) -> bytes:
        document = self._judged(body, MAX_BODY_BYTES["pairing"])
        try:
            request = RelayPairingRequest.model_validate(document)
            key = decode_public(request.signing_key)
        except (ValidationError, ValueError) as invalid:
            raise self._refuse(
                RelayProblemCode.INVALID_REQUEST, "a pairing request carries no id"
            ) from invalid
        self._signed(PAIRING_PATH, body, signature, key)
        if not self._requests.allow(client_ip):
            raise self._refuse(
                RelayProblemCode.RATE_LIMITED, retry_after=self._requests.retry_after(client_ip)
            )
        pairing = self._registry.begin(request.signing_key)
        return json_response(201, self._document(pairing))

    def _poll(self, body: bytes, signature: str | None) -> bytes:
        document = self._judged(body, MAX_BODY_BYTES["pairing_poll"])
        try:
            poll = RelayPairingPoll.model_validate(document)
        except ValidationError as invalid:
            raise self._refuse(RelayProblemCode.INVALID_REQUEST) from invalid
        pairing = self._registry.pairing(poll.device_code)
        if pairing is None or not hmac.compare_digest(pairing.device_code, poll.device_code):
            # A device code the relay does not know names no key to verify a signature under.
            raise self._refuse(RelayProblemCode.UNKNOWN_DEVICE_CODE)
        self._signed(POLL_PATH, body, signature, pairing.public)
        moment = self._now()
        if moment - pairing.last_request_at < PAIRING_INTERVAL_S:
            wait = int(pairing.last_request_at + PAIRING_INTERVAL_S - moment) + 1
            raise self._refuse(RelayProblemCode.RATE_LIMITED, retry_after=wait)
        pairing.last_request_at = moment
        return json_response(200, self._document(pairing))

    def _document(self, pairing: Pairing) -> dict[str, Any]:
        """The pairing answer, through its model: a status carries exactly what it says."""
        values: dict[str, Any] = {
            "status": pairing.status,
            "server_time": iso(self._now()),
            "protocol": self._window,
        }
        if pairing.status is RelayPairingStatus.PENDING:
            values |= {
                "device_code": pairing.device_code,
                "user_code": pairing.user_code,
                "verification_uri": self._verification_uri,
                "expires_at": iso(pairing.expires_at),
                "interval_s": PAIRING_INTERVAL_S,
            }
        elif pairing.status is RelayPairingStatus.APPROVED and pairing.installation is not None:
            values |= {"id": pairing.installation, "hostname": self._registry.hostname(pairing.installation)}
        return RelayPairingAnswer.model_validate(values).model_dump(mode="json", exclude_none=True)
