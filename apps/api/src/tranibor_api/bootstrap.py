# SPDX-License-Identifier: Apache-2.0
"""Bootstrap of a new installation: the first organisation and its administrators.

An empty database has neither organisation nor member. `POST /orgs/{org}/projects` answers 404,
`POST /orgs` demands an administrator who does not exist yet, and the OIDC group mapping has
nothing to attach a role to. Before 2026-09-24 only `dev/scripts/seed.py` and `demo/seed.py` built
an organisation: an installation through the chart was unusable without touching the database.

This module reads `TRANIBOR_BOOTSTRAP_ORG` and `TRANIBOR_BOOTSTRAP_ADMINS` when the API starts. It
creates what is missing and does not modify what exists: an administrator removed by hand does not
come back at the next restart.

A personal installation holds one person (ADR 0228): there the variable names one address at most, and
an address that would be a second person is refused by the database, which this module says in its own
words (`personal_installation.py`). The database refuses it because the `installation` row says
`personal`, so the bootstrap records the posture itself before its first membership, in the same
transaction: `choregos-admin orgs bootstrap` runs it on a database the API may never have started, and
two runs naming two persons would otherwise both find no record and write both.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tranibor_contracts import Role

from .config import Settings
from .db.models import Membership, Organization, User
from .db.org_aliases import organisation_by_slug_or_alias
from .db.session import session_scope
from .logging import get_logger
from .personal_installation import (
    bootstrap_refusal,
    is_sole_member_refusal,
    record_the_first_start,
    refuse_what_the_settings_forbid,
)

logger = get_logger("choregos.bootstrap")

#: The keys of what `bootstrap_installation` returns. `admin_cli.py` and the installation tests read
#: the second one under its former name, so it stays until their own stories switch it.
ORGANISATIONS = "organisations"
ADMINISTRATORS = "administrateurs"  # lang: fr-ok returned key that admin_cli.py and the tests read


async def bootstrap_installation(settings: Settings) -> dict[str, list[str]]:
    """Creates the bootstrap organisation and its administrators if missing. Returns what was created."""
    # One address at most in the personal posture, whether the API's start-up or the CLI calls.
    refuse_what_the_settings_forbid(settings)
    if not settings.bootstrap_org:
        return {ORGANISATIONS: [], ADMINISTRATORS: []}
    try:
        return await _bootstrap(settings)
    except IntegrityError as error:
        if is_sole_member_refusal(error):
            raise bootstrap_refusal() from error
        raise


async def _bootstrap(settings: Settings) -> dict[str, list[str]]:
    created: dict[str, list[str]] = {ORGANISATIONS: [], ADMINISTRATORS: []}
    async with session_scope(orgs="*") as session:
        # The posture first: the record governs the memberships written below (ADR 0228 §2), whether the
        # API's start-up recorded it already or this is the first write to the database.
        await record_the_first_start(session, settings)
        # By slug OR by former slug: once the organisation has been renamed, the deployment's value
        # still names the old one, and a restart must not create a second organisation (ADR 0090).
        org = await organisation_by_slug_or_alias(session, settings.bootstrap_org)
        if org is None:
            org = Organization(
                slug=settings.bootstrap_org, name=settings.bootstrap_org_name or settings.bootstrap_org
            )
            session.add(org)
            await session.flush()
            created[ORGANISATIONS].append(org.slug)
            for email in settings.bootstrap_admin_emails:
                user = (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()
                if user is None:
                    user = User(email=email, display_name=email.split("@", 1)[0])
                    session.add(user)
                    await session.flush()
                session.add(Membership(user_id=user.id, org_id=org.id, role=str(Role.ORG_ADMIN)))
                created[ADMINISTRATORS].append(email)
    if created[ORGANISATIONS]:
        logger.info(
            "installation bootstrapped", organisation=settings.bootstrap_org, admins=created[ADMINISTRATORS]
        )
    return created
