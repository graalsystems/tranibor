# SPDX-License-Identifier: Apache-2.0
"""The owner signs in with the owner key, on the loopback door (ADR 0228 §4).

A personal installation has no identity provider and no password. `choregos local init` generates an
Ed25519 key pair in the installation's data directory; the API holds only its public half, in the file
`TRANIBOR_OWNER_PUBLIC_KEY_FILE` names. `choregos local open` signs a ticket with the private half
(`tranibor_core.owner_ticket`) and opens the console's loopback URL with the ticket in the fragment; the
console's `/auth/owner` page posts it to `POST /auth/owner`, which:

- answers 404 outside the personal posture and on any door but the loopback one, as an unknown route does,
  before the ticket is read (`on_the_loopback_door`);
- checks the ticket against the public key (`EdDSA`, audience `choregos-owner`, valid 60 seconds at most,
  not expired), and refuses one that does not hold with one 401 that never says why: the reason is logged;
- finds the installation's one person, the one account that is not a machine's holding a membership
  (ADR 0228 §2; the bootstrap writes it, S87-10's trigger keeps it alone);
- takes the ticket's `jti` once, in `owner_tickets` under a unique constraint, in the transaction that opens
  the session: two replicas given the same ticket open one session, and the loser is refused.

The session is the person's: recorded (ADR 0130), listed and ended under My account, with an authentication
time of now (fresh for ADR 0170 and ADR 0228 §3), and the loopback door's cookie (`doors.py`).
"""

from __future__ import annotations

import asyncio
import pathlib
from datetime import timedelta
from typing import NoReturn

from fastapi import HTTPException, Request, status
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_core import utcnow
from tranibor_core.owner_ticket import (
    CLOCK_SKEW_S,
    OwnerTicketRefusedError,
    load_public_key,
    verify_owner_ticket,
)
from tranibor_core.production import is_personal

from .config import Settings, get_settings
from .db.models import Membership, OwnerTicket, User
from .doors import LOOPBACK, door_of
from .errors import unauthorized
from .logging import get_logger

logger = get_logger("choregos.owner_sign_in")

#: The setting that names the owner's public key file.
SETTING = "TRANIBOR_OWNER_PUBLIC_KEY_FILE"
#: The one answer to a ticket that opens nothing, whatever the reason: replayed, expired, too long,
#: for another audience or signed by another key.
REFUSED = "this sign-in link is not valid: run `choregos local open` again"
#: How the owner signs in again on the loopback door when a gesture asks for a fresh authentication: a new
#: owner ticket, never the identity provider's re-authentication, which a personal installation does not have.
OWNER_REAUTH = "choregos local open"


def owner_key_configured(settings: Settings) -> bool:
    """Whether an owner ticket can open a session here at all: the personal posture, with a key file."""
    return is_personal(settings.env) and bool(settings.owner_public_key_file.strip())


def check_the_owner_key(settings: Settings) -> None:
    """At start-up, in the personal posture: a key file that is set must hold an Ed25519 public key.

    A file that is missing, unreadable or holds anything else stops the start-up, naming the setting: the
    owner would otherwise meet a 401 at every sign-in with nothing to say why. An unset one is said once.
    """
    if not is_personal(settings.env):
        return
    path = settings.owner_public_key_file.strip()
    if not path:
        logger.warning(
            "owner_sign_in.no_key",
            detail=f"{SETTING} is not set: no owner ticket opens a session on this personal installation "
            "(`choregos local init` writes the key pair and the setting)",
        )
        return
    try:
        load_public_key(pathlib.Path(path).read_bytes())
    except OSError as error:
        raise RuntimeError(
            f"{SETTING}={path}: the file cannot be read ({type(error).__name__}). The API refuses to start: "
            "the owner could not sign in. Point it at the public half `choregos local init` wrote."
        ) from error
    except ValueError as error:
        raise RuntimeError(
            f"{SETTING}={path}: {error}. The API refuses to start: the owner key is an Ed25519 public key in "
            "PEM, the public half `choregos local init` wrote."
        ) from error


def on_the_loopback_door(request: Request) -> None:
    """The owner route exists on the loopback door of a personal installation only: elsewhere, 404 as for an
    unknown route, before the body is read (ADR 0228 §4, ADR 0230 §11)."""
    if door_of(request, get_settings()) != LOOPBACK:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _refuse(reason: str) -> NoReturn:
    logger.warning("owner_sign_in.refused", reason=reason)
    raise unauthorized(REFUSED)


async def _public_key(settings: Settings) -> bytes | None:
    path = settings.owner_public_key_file.strip()
    if not path:
        return None
    try:
        return await asyncio.to_thread(pathlib.Path(path).read_bytes)
    except OSError:
        return None


async def the_owner(session: AsyncSession) -> User | None:
    """The installation's one person: the one account, not a machine's (ADR 0131), holding a membership.

    `None` when there is none yet (no bootstrap) or more than one, which the personal posture refuses
    (ADR 0228 §2): no ticket then says whom it signs in.
    """
    members = select(Membership.user_id)
    rows = (
        (await session.execute(select(User).where(User.is_service.is_(False), User.id.in_(members)).limit(2)))
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


async def take_owner_ticket(session: AsyncSession, ticket: str, settings: Settings) -> User:
    """The person a valid owner ticket signs in, its `jti` taken; otherwise 401 `REFUSED`, for any reason."""
    public = await _public_key(settings)
    if public is None:
        _refuse(f"no owner key: {SETTING} is unset or unreadable")
    now = utcnow()
    try:
        verified = verify_owner_ticket(ticket, public, now)
    except (OwnerTicketRefusedError, ValueError) as error:
        _refuse(str(error))
    owner = await the_owner(session)
    if owner is None:
        _refuse("the installation does not hold exactly one person")
    # A taken ticket is kept until it expires: past that, the expiry refuses it before its `jti` is read.
    await session.execute(
        delete(OwnerTicket).where(OwnerTicket.expires_at < now - timedelta(seconds=CLOCK_SKEW_S))
    )
    session.add(OwnerTicket(jti=verified.jti, expires_at=verified.expires_at))
    try:
        await session.flush()
    except IntegrityError:
        _refuse("the ticket was already taken")
    owner.last_login_at = now
    return owner
