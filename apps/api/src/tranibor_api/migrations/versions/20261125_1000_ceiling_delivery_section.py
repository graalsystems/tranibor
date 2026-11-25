# SPDX-License-Identifier: Apache-2.0
"""An organisation's policy ceiling caps the diff bounds in its `delivery` section (ADR 0181, S83-13)

The policy's diff bounds moved to its `delivery` section, the `software-delivery` plugin's, in round C2
(S83-04e3, S83-04r-delivery). The organisation's policy ceiling kept its own caps of them,
`scope.max_diff_files` and `scope.max_diff_lines`, which bounded `delivery.scope.*`. They move to the
ceiling's `delivery` section the same way, and a ceiling that writes them at their former place is refused.
This migration rewrites what is stored:

- `organizations.settings`, key `policy_ceiling`, its `ceiling`: `scope.max_diff_files`,
  `scope.max_diff_lines` → `delivery.scope.*`. A `scope` left empty is dropped, as the API stores a
  ceiling (`exclude_defaults`). A cap written at both places keeps the former one, which is the one the
  previous release applied.

The audit log's `policy.ceiling` entries keep the ceiling as it was written: an audit entry is a record,
never rewritten, and nothing reads it back as a ceiling.

The transform is inlined: a migration does not import application code, which changes later.

Idempotent: a ceiling without a former cap is not touched. The downgrade moves the caps back, since the
previous release refuses a ceiling with a `delivery` section (`extra="forbid"`).

Revision ID: 6eb670a63a1b
Revises: e8c0a2d4f6b1
Create Date: 2026-11-25 10:00:00+00:00
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "6eb670a63a1b"
down_revision: str | None = "e8c0a2d4f6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CEILING_KEY = "policy_ceiling"
DIFF_FIELDS = ("max_diff_files", "max_diff_lines")


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def ceiling_forward(ceiling: Any) -> Any:
    """A ceiling with its diff caps in its `delivery` section; unchanged without a former cap."""
    if not isinstance(ceiling, dict):
        return ceiling
    scope = ceiling.get("scope")
    if not isinstance(scope, dict) or not any(name in scope for name in DIFF_FIELDS):
        return ceiling
    ceiling = dict(ceiling)
    scope = dict(scope)
    delivery = dict(ceiling.get("delivery") or {})
    bounds = dict(delivery.get("scope") or {})
    for name in DIFF_FIELDS:
        if name in scope:
            value = scope.pop(name)
            if value is not None:
                bounds[name] = value
    if scope:
        ceiling["scope"] = scope
    else:
        ceiling.pop("scope", None)
    if bounds:
        delivery["scope"] = bounds
    if delivery:
        ceiling["delivery"] = delivery
    return ceiling


def ceiling_back(ceiling: Any) -> Any:
    """The downgrade's: the caps back under `scope`, the section dropped."""
    if not isinstance(ceiling, dict) or not isinstance(ceiling.get("delivery"), dict):
        return ceiling
    ceiling = dict(ceiling)
    delivery = ceiling.pop("delivery")
    bounds = delivery.get("scope") if isinstance(delivery.get("scope"), dict) else {}
    moved = {name: bounds[name] for name in DIFF_FIELDS if bounds.get(name) is not None}
    if moved:
        ceiling["scope"] = {**dict(ceiling.get("scope") or {}), **moved}
    return ceiling


def settings_with(settings: Any, transform: Any) -> Any:
    """An organisation's settings with its stored ceiling transformed; unchanged otherwise."""
    if not isinstance(settings, dict):
        return settings
    stored = settings.get(CEILING_KEY)
    if not isinstance(stored, dict) or not isinstance(stored.get("ceiling"), dict):
        return settings
    rewritten = transform(stored["ceiling"])
    if rewritten == stored["ceiling"]:
        return settings
    return {**settings, CEILING_KEY: {**stored, "ceiling": rewritten}}


def _rewrite(transform: Any) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # Forced RLS: the migrating role sees no row without this (S39-27, S40-22e).
        bind.execute(sa.text("SELECT set_config('app.current_orgs', '*', true)"))
    target = sa.table("organizations", sa.column("id"), sa.column("settings", sa.JSON))
    rows = bind.execute(sa.text("SELECT id, settings FROM organizations WHERE settings IS NOT NULL")).fetchall()
    for row_id, stored in rows:
        settings = _json(stored)
        rewritten = settings_with(settings, transform)
        if rewritten is not settings and rewritten != settings:
            bind.execute(target.update().where(target.c.id == row_id).values(settings=rewritten))


def upgrade() -> None:
    _rewrite(ceiling_forward)


def downgrade() -> None:
    _rewrite(ceiling_back)
