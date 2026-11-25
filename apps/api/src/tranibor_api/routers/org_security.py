# SPDX-License-Identifier: Apache-2.0
"""An organisation's sign-in security (ADR 0170): how recent an approval's authentication must be, and
an administrator ending a member's sessions; and its Temporal data keys (ADR 0224): listed, rotated and
shredded. All belong to the organisation's administrator (on a single-organisation instance, the
platform's administrator is that person)."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import func, select
from tranibor_contracts import Role

from ..approval_freshness import SETTING, required_seconds
from ..audit import record
from ..browser_sessions import revoke_user_sessions
from ..db.models import Membership, User
from ..db.session import in_platform_scope
from ..deps import Db, Me
from ..errors import conflict, forbidden, not_found, unprocessable
from ..schemas.org_security import (
    OrgSecurity,
    SessionRevocation,
    SessionsRevoked,
    TemporalKeyInfo,
    TemporalKeys,
    TemporalKeyShredding,
    TemporalKeysShredded,
)
from ..services import temporal_keys
from ..services.organisations import organisation_by_slug

router = APIRouter(tags=["security"])


def _require_admin(principal: Me, org: str, gesture: str) -> None:
    if principal.org_roles.get(org) != Role.ORG_ADMIN:
        raise forbidden(f"{gesture} the security of {org} takes being its org_admin")


@router.get("/orgs/{org}/security", response_model=OrgSecurity, operation_id="getOrgSecurity")
async def get_security(org: str, session: Db, principal: Me) -> OrgSecurity:
    _require_admin(principal, org, "reading")
    organisation = await organisation_by_slug(session, org)
    return OrgSecurity(approval_freshness_s=required_seconds(organisation))


@router.put("/orgs/{org}/security", response_model=OrgSecurity, operation_id="setOrgSecurity")
async def set_security(org: str, body: OrgSecurity, session: Db, principal: Me) -> OrgSecurity:
    _require_admin(principal, org, "setting")
    organisation = await organisation_by_slug(session, org)
    settings = dict(organisation.settings or {})
    if body.approval_freshness_s is None:
        settings.pop(SETTING, None)
    else:
        settings[SETTING] = body.approval_freshness_s
    organisation.settings = settings  # reassigned: a mutated JSON column is not tracked
    await record(
        session,
        principal,
        "org.security",
        org_id=organisation.id,
        target_type="org",
        target_id=organisation.id,
        **body.model_dump(),
    )
    return body


@router.post(
    "/orgs/{org}/members/{email}/sessions/revocation",
    response_model=SessionsRevoked,
    operation_id="revokeMemberSessions",
)
async def revoke_member_sessions(
    org: str, email: str, body: SessionRevocation, session: Db, principal: Me
) -> SessionsRevoked:
    """End every session of a member of this organisation, from their next request."""
    _require_admin(principal, org, "revoking sessions in")
    organisation = await organisation_by_slug(session, org)
    user = (
        (
            await session.execute(
                select(User)
                .join(Membership, Membership.user_id == User.id)
                .where(Membership.org_id == organisation.id, func.lower(User.email) == email.lower())
            )
        )
        .scalars()
        .first()
    )
    if user is None:
        raise not_found("Member", email)
    # Membership was checked above; the write itself is on ANOTHER person's rows, which the identity
    # RLS lets only the owner or the platform scope write (ADR 0130, 6).
    async with in_platform_scope(session):
        ended = await revoke_user_sessions(session, user)
    await record(
        session,
        principal,
        "user.sessions.revoke",
        org_id=organisation.id,
        target_type="user",
        target_id=user.id,
        reason=body.reason,
        ended=ended,
    )
    return SessionsRevoked(revoked=user.email, ended=ended)


# ───────────────────────────── Temporal data keys (ADR 0224) ─────────────────────────────


def _require_key_admin(principal: Me, org: str, gesture: str) -> None:
    if principal.org_roles.get(org) != Role.ORG_ADMIN:
        raise forbidden(f"{gesture} the Temporal keys of {org} takes being its org_admin")


def _key_info(row: object) -> TemporalKeyInfo:
    return TemporalKeyInfo.model_validate(row, from_attributes=True)


@router.get("/orgs/{org}/temporal-keys", response_model=TemporalKeys, operation_id="listTemporalKeys")
async def list_temporal_keys(org: str, session: Db, principal: Me) -> TemporalKeys:
    """The organisation's Temporal data keys: ids, states, wrapping key, dates. Never a key."""
    _require_key_admin(principal, org, "reading")
    organisation = await organisation_by_slug(session, org)
    rows = await temporal_keys.list_data_keys(session, organisation.id)
    return TemporalKeys(
        org=org,
        available=temporal_keys.available(),
        keys=[_key_info(row) for row in rows],
    )


@router.post(
    "/orgs/{org}/temporal-keys/rotation", response_model=TemporalKeyInfo, operation_id="rotateTemporalKey"
)
async def rotate_temporal_key(org: str, session: Db, principal: Me) -> TemporalKeyInfo:
    """A new data key for the organisation's new Temporal payloads; the retired ones still open theirs."""
    _require_key_admin(principal, org, "rotating")
    organisation = await organisation_by_slug(session, org)
    try:
        row = await temporal_keys.rotate_data_key(session, organisation.id, actor=principal.email)
    except temporal_keys.TemporalKeysUnavailable as refusal:
        raise conflict(str(refusal)) from refusal
    await record(
        session,
        principal,
        "org.temporal_keys.rotate",
        org_id=organisation.id,
        target_type="org",
        target_id=organisation.id,
        key_id=row.key_id,
        kek_id=row.kek_id,
    )
    return _key_info(row)


@router.post(
    "/orgs/{org}/temporal-keys/shredding",
    response_model=TemporalKeysShredded,
    operation_id="shredTemporalKeys",
)
async def shred_temporal_keys(
    org: str, body: TemporalKeyShredding, session: Db, principal: Me
) -> TemporalKeysShredded:
    """Destroy every wrapped Temporal data key of the organisation (crypto-shredding, ADR 0224): what they
    sealed can never be read again. Irreversible; `confirm` repeats the organisation's slug. Audited."""
    _require_key_admin(principal, org, "shredding")
    if body.confirm != org:
        raise unprocessable(
            f"`confirm` must repeat the organisation's slug, `{org}`: shredding is irreversible"
        )
    organisation = await organisation_by_slug(session, org)
    try:
        shredded = await temporal_keys.shred_data_keys(session, organisation.id, actor=principal.email)
    except temporal_keys.TemporalKeysUnavailable as refusal:
        raise conflict(str(refusal)) from refusal
    await record(
        session,
        principal,
        "org.temporal_keys.shred",
        org_id=organisation.id,
        target_type="org",
        target_id=organisation.id,
        reason=body.reason,
        keys=shredded,
    )
    return TemporalKeysShredded(org=org, shredded=shredded, not_erased=list(temporal_keys.NOT_ERASED))
