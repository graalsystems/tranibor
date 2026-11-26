"""The owner ticket (ADR 0228 §4): what `choregos local open` signs and `POST /auth/owner` verifies."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from tranibor_core.owner_ticket import (
    AUDIENCE,
    MAX_LIFETIME_S,
    OwnerTicketRefusedError,
    generate_owner_key_pair,
    sign_claims,
    sign_owner_ticket,
    verify_owner_ticket,
)

NOW = datetime(2026, 11, 27, 9, 0, tzinfo=UTC)


def _parts(ticket: str) -> tuple[dict[str, object], dict[str, object]]:
    head, body, _ = ticket.split(".")
    decode = lambda part: json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))  # noqa: E731
    return decode(head), decode(body)


def test_a_ticket_is_an_eddsa_compact_jws_for_the_owner_audience_valid_sixty_seconds() -> None:
    private, public = generate_owner_key_pair()
    ticket = sign_owner_ticket(private, NOW)
    header, claims = _parts(ticket)
    assert header == {"alg": "EdDSA", "typ": "JWT"}
    assert claims["aud"] == AUDIENCE == "choregos-owner"
    assert claims["iat"] == int(NOW.timestamp())
    assert claims["exp"] == int(NOW.timestamp()) + MAX_LIFETIME_S
    verified = verify_owner_ticket(ticket, public, NOW + timedelta(seconds=59))
    assert verified.jti == claims["jti"]
    assert verified.expires_at - verified.issued_at == timedelta(seconds=60)
    # Every ticket has its own `jti`: the API takes each once.
    assert verify_owner_ticket(sign_owner_ticket(private, NOW), public, NOW).jti != verified.jti


def test_a_lifetime_beyond_sixty_seconds_is_not_signed() -> None:
    private, _ = generate_owner_key_pair()
    with pytest.raises(ValueError, match="1 to 60 seconds"):
        sign_owner_ticket(private, NOW, lifetime_s=61)


def _claims(**overrides: object) -> dict[str, object]:
    issued = int(NOW.timestamp())
    return {"aud": AUDIENCE, "iat": issued, "exp": issued + 30, "jti": "a" * 32} | overrides


@pytest.mark.parametrize(
    ("case", "claims", "header", "reason"),
    [
        ("expired", _claims(iat=int(NOW.timestamp()) - 60, exp=int(NOW.timestamp())), None, "expired"),
        ("too long", _claims(exp=int(NOW.timestamp()) + 61), None, "valid for 61 seconds"),
        ("another audience", _claims(aud="choregos-mcp"), None, "another audience"),
        ("an audience list", _claims(aud=[AUDIENCE]), None, "another audience"),
        (
            "issued in the future",
            _claims(iat=int(NOW.timestamp()) + 30, exp=int(NOW.timestamp()) + 60),
            None,
            "future",
        ),
        ("no jti", {k: v for k, v in _claims().items() if k != "jti"}, None, "jti"),
        ("a short jti", _claims(jti="abc"), None, "jti"),
        ("a float iat", _claims(iat=float(int(NOW.timestamp()))), None, "whole number"),
        ("another algorithm", _claims(), {"alg": "HS256"}, "not EdDSA"),
        ("a critical extension", _claims(), {"alg": "EdDSA", "crit": ["b64"]}, "critical"),
    ],
)
def test_a_ticket_that_says_the_wrong_thing_is_refused(
    case: str, claims: dict[str, object], header: dict[str, object] | None, reason: str
) -> None:
    private, public = generate_owner_key_pair()
    with pytest.raises(OwnerTicketRefusedError, match=reason):
        verify_owner_ticket(sign_claims(private, claims, header), public, NOW)


def test_a_ticket_signed_by_another_key_or_altered_is_refused() -> None:
    private, public = generate_owner_key_pair()
    other, _ = generate_owner_key_pair()
    with pytest.raises(OwnerTicketRefusedError, match="signature"):
        verify_owner_ticket(sign_owner_ticket(other, NOW), public, NOW)
    head, body, signature = sign_owner_ticket(private, NOW).split(".")
    stretched = (
        base64.urlsafe_b64encode(json.dumps(_claims(exp=int(NOW.timestamp()) + 3600)).encode())
        .decode()
        .rstrip("=")
    )
    with pytest.raises(OwnerTicketRefusedError, match="signature"):
        verify_owner_ticket(f"{head}.{stretched}.{signature}", public, NOW)
    unsigned_head = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    for forged in (
        f"{unsigned_head}.{body}.",
        f"{head}.{body}",
        "not a ticket",
        f"{head}.{body}.{signature}!",
    ):
        with pytest.raises(OwnerTicketRefusedError):
            verify_owner_ticket(forged, public, NOW)


def test_only_an_ed25519_key_is_an_owner_key() -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    elliptic = ec.generate_private_key(ec.SECP256R1())
    pem = elliptic.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(ValueError, match="Ed25519"):
        sign_owner_ticket(pem, NOW)
    public = elliptic.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    private, _ = generate_owner_key_pair()
    with pytest.raises(ValueError, match="Ed25519"):
        verify_owner_ticket(sign_owner_ticket(private, NOW), public, NOW)
    with pytest.raises(ValueError, match="not a PEM"):
        verify_owner_ticket(sign_owner_ticket(private, NOW), b"garbage", NOW)


def test_the_signer_imports_no_server_module() -> None:
    """The CLI and the desktop app sign tickets: the module loads nothing of the API, the database or HTTP."""
    probe = (
        "import sys, tranibor_core.owner_ticket\n"
        "print(sorted({m.split('.')[0] for m in sys.modules} & "
        "{'tranibor_api', 'fastapi', 'starlette', 'sqlalchemy', 'uvicorn', 'httpx', 'temporalio'}))"
    )
    loaded = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True)
    assert loaded.stdout.strip() == "[]"
