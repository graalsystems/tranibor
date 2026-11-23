# SPDX-License-Identifier: Apache-2.0
"""The roles' former names leave the rows: `developer` is `contributor`, `release_captain` is `approver`

ADR 0181 round C1 named the two roles neutrally on 2026-11-19, and the project lead closed its window
early on 2026-11-23 (ADR 0181, "Decision (2026-11-23)"): no release reads `developer` or
`release_captain` from a membership any more. This migration rewrites the rows that still hold them:

- `memberships.role`, the platform-owned state ADR 0181 named;
- the approver roles of a proposed action (`actions.approval.approvers[].role`), which the broker and the
  action service wrote with the former spelling while the window was open (`Role.stored`);
- the role groups an organisation's SAML provider maps its groups to (`org_saml_idps.group_map` values
  `developers` and `release-captains`, which give no role any more).

A stored workflow version is not rewritten (its digest would move): it is read with the context
`{"stored": True}`, which reads a former approver role as its successor (`tranibor_contracts.recorded`).

Idempotent: a row already rewritten no longer matches. Nothing to undo: the release before this one
(0.28.1) already reads `contributor` and `approver` (C1's readers shipped in 0.27.17).

Revision ID: c1a3e5f7b9d2
Revises: a9c1e3b5d7f0
Create Date: 2026-11-23 09:00:00+00:00
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

from tranibor_api.db.base import Json

revision: str = "c1a3e5f7b9d2"
down_revision: str | None = "a9c1e3b5d7f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: A former role and its successor (ADR 0181 C1).
RENAMED = {"developer": "contributor", "release_captain": "approver"}

#: A former role group and its successor.
RENAMED_GROUPS = {"developers": "contributors", "release-captains": "approvers"}

ACTIONS = sa.table("actions", sa.column("id", sa.String), sa.column("approval", Json()))
SAML = sa.table("org_saml_idps", sa.column("id", sa.String), sa.column("group_map", Json()))


def _approval(value: Any) -> dict[str, Any] | None:
    """The approval with its former roles renamed, or `None` when it holds none."""
    document = json.loads(value) if isinstance(value, str) else value
    if not isinstance(document, dict) or not isinstance(document.get("approvers"), list):
        return None
    changed = False
    approvers = []
    for approver in document["approvers"]:
        if isinstance(approver, dict) and approver.get("role") in RENAMED:
            approver = {**approver, "role": RENAMED[approver["role"]]}
            changed = True
        approvers.append(approver)
    return {**document, "approvers": approvers} if changed else None


def _group_map(value: Any) -> dict[str, Any] | None:
    """The group map with its former role groups renamed, or `None` when it holds none."""
    document = json.loads(value) if isinstance(value, str) else value
    if not isinstance(document, dict) or not set(document.values()) & set(RENAMED_GROUPS):
        return None
    return {key: RENAMED_GROUPS.get(group, group) for key, group in document.items()}


def upgrade_rows(connection: Any) -> int:
    """Rewrites the rows; returns how many changed."""
    changed = 0
    for old, new in RENAMED.items():
        result = connection.execute(
            sa.text("UPDATE memberships SET role = :new WHERE role = :old"), {"old": old, "new": new}
        )
        changed += result.rowcount or 0
    for row in connection.execute(sa.select(ACTIONS.c.id, ACTIONS.c.approval)).mappings().all():
        approval = _approval(row["approval"])
        if approval is not None:
            connection.execute(ACTIONS.update().where(ACTIONS.c.id == row["id"]).values(approval=approval))
            changed += 1
    for row in connection.execute(sa.select(SAML.c.id, SAML.c.group_map)).mappings().all():
        group_map = _group_map(row["group_map"])
        if group_map is not None:
            connection.execute(SAML.update().where(SAML.c.id == row["id"]).values(group_map=group_map))
            changed += 1
    return changed


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        # Forced RLS: the migrating role sees no row without this (S39-27, S40-22e).
        connection.execute(sa.text("SELECT set_config('app.current_orgs', '*', true)"))
    upgrade_rows(connection)


def downgrade() -> None:
    """Nothing to undo: the previous release reads the neutral names."""
