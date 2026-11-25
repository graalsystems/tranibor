# SPDX-License-Identifier: Apache-2.0
"""An organisation's sign-in security settings and the revocation of a member's sessions (ADR 0170); its
Temporal data keys (ADR 0224)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ..approval_freshness import MINIMUM_S


class OrgSecurity(BaseModel):
    #: In seconds; `null` removes the requirement. At least 60: below, nobody could ever approve.
    approval_freshness_s: int | None = Field(default=None, ge=MINIMUM_S)


class SessionRevocation(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


class SessionsRevoked(BaseModel):
    revoked: str
    ended: int


class TemporalKeyInfo(BaseModel):
    """One of the organisation's Temporal data keys (ADR 0224). Never the key, wrapped or not."""

    key_id: str
    #: `active`, `retired` (rotated out, still opens what it sealed) or `shredded`.
    state: str
    #: The key wrapper's key that wraps it.
    kek_id: str
    created_at: datetime
    created_by: str | None = None
    shredded_at: datetime | None = None
    shredded_by: str | None = None


class TemporalKeys(BaseModel):
    org: str
    #: Whether the deployment can hold data keys (a key wrapper is configured); `false`: the organisation's
    #: keys are derived from the master key (ADR 0220) and cannot be shredded.
    available: bool
    keys: list[TemporalKeyInfo]


class TemporalKeyShredding(BaseModel):
    #: The organisation's slug, typed again: shredding cannot be undone.
    confirm: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=3, max_length=500)


class TemporalKeysShredded(BaseModel):
    org: str
    #: The key ids destroyed by this call (empty when they already were).
    shredded: list[str]
    #: What shredding does not erase.
    not_erased: list[str]
