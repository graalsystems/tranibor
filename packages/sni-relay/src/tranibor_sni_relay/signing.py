# SPDX-License-Identifier: Apache-2.0
"""The agent's Ed25519 signatures, as `relay.yaml` describes them: compact JWS, `alg` `EdDSA`.

A pairing request and a pairing poll carry one in `Relay-Signature` (`x-request-signature`), a control
hello in its `signature` (`x-hello-signature`). The relay holds only public keys: the agent's, bound to
an id at the pairing. It verifies a signature under that key and an exact protected header, and leaves
`iat` to the caller, which compares it with its own clock.
"""

from __future__ import annotations

import base64
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from tranibor_contracts.outpost import SIGNATURE_ALGORITHM


class SignatureError(Exception):
    """No signature, one that does not verify, or one under another protected header."""


def encode_public(key: Ed25519PublicKey) -> str:
    """A public key as the contract writes it: raw 32 bytes, base64url without padding."""
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def decode_public(encoded: str) -> Ed25519PublicKey:
    """The key `encoded` writes; `ValueError` when it is not one."""
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except (ValueError, TypeError) as broken:
        raise ValueError("not base64url") from broken
    return Ed25519PublicKey.from_public_bytes(raw)


def sign(key: Ed25519PrivateKey, claims: dict[str, Any], header: dict[str, str]) -> str:
    """A compact JWS over `claims` by `key`, under exactly `header`."""
    return jwt.encode(claims, key, algorithm=SIGNATURE_ALGORITHM, headers=header)


def verified(token: str, key: Ed25519PublicKey, *, header: dict[str, str]) -> dict[str, Any]:
    """The claims of `token`, once its protected header is exactly `header` and its signature verifies
    under `key`. `iat` is not judged here."""
    try:
        if jwt.get_unverified_header(token) != header:
            raise SignatureError("the protected header is not the one this signature must carry")
        claims: dict[str, Any] = jwt.decode(
            token,
            key,
            algorithms=[SIGNATURE_ALGORITHM],
            options={"verify_iat": False, "verify_exp": False, "verify_nbf": False, "verify_aud": False},
        )
    except jwt.InvalidTokenError as refused:
        raise SignatureError("the signature does not verify") from refused
    return claims
