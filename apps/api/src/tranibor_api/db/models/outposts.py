# SPDX-License-Identifier: Apache-2.0
"""Outposts, their instances, their enrolment tokens and the calls routed to them (ADRs 0147-0149).

Every row is an organisation's, under forced row-level security. The three tables that belong to one
outpost (`outpost_instances`, `outpost_enrolments`, `outpost_calls`) also require the session to name
that outpost (`app.current_outpost`, ADR 0148 §6): a session that names none sees none of their rows.
Nothing of an outpost's side is stored: no address, no credential, no private key (ADR 0147 §7).

An outpost may be one person's (`owner_user_id`, ADR 0226 §1): their personal runner, one per person and
organisation. Its row stays the organisation's; its own rows are under a third, RESTRICTIVE policy that
shows them to their owner, to the door and under the platform scope, and to no other person
(`20261127_1200_s87_15_personal_outposts`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..base import Base, Json, PkMixin, TimestampMixin, utcnow


class Outpost(Base, PkMixin, TimestampMixin):
    """A place a person enrolled, of one organisation, that only dials out (ADR 0147 §1).

    `status` is an `OutpostStatus`. `epoch` is moved by every revocation, of the outpost or of one of
    its instances: an answer is taken only under the epoch its call was claimed in (ADR 0147 §5).

    With an `owner_user_id` it is that person's runner (ADR 0226 §1): enrolled and approved by its owner
    only, through `/me/runners`, and refused by every administrator route that reaches its instances, its
    enrolment tokens or its calls (ADR 0226 §2)."""

    __tablename__ = "outposts"
    __table_args__ = (
        UniqueConstraint("org_id", "slug", name="uq_outposts_org_slug"),
        # One personal runner per person and organisation, as one personal space (ADR 0226 §1).
        Index(
            "uq_outposts_personal_owner",
            "org_id",
            "owner_user_id",
            unique=True,
            postgresql_where=text("owner_user_id IS NOT NULL"),
            sqlite_where=text("owner_user_id IS NOT NULL"),
        ),
    )

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    #: The name connectors route by and the journal shows (`outpost:<slug>/<alias>`).
    slug: Mapped[str] = mapped_column(String(63))
    display_name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    #: What the organisation enabled it for (`OutpostCapability`); `tools` is the one of this version.
    capabilities: Mapped[list[str]] = mapped_column(Json, default=list)
    epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: The person whose runner it is (ADR 0226 §1); none for an organisation's outpost.
    owner_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    #: Who paused it, which is the side that lifts the pause (ADR 0226 §2): `administrator` or `owner`.
    #: Null while it is not paused.
    paused_by: Mapped[str | None] = mapped_column(String(16), nullable=True)


class OutpostInstance(Base, TimestampMixin):
    """One copy of the daemon `choregos-outpost`, with keys made on the spot (ADR 0147 §2). Public
    halves only: the platform never holds a private key of an instance.

    The id starts with its outpost's id (`services.outposts.outpost_id_of_instance`): a request signed
    by an instance names it in `kid`, and the door has to name the outpost before it can read the row.
    `signing_key` is unique across the installation: a key enrolled anywhere, in any state, is refused
    elsewhere by the index itself, which row-level security does not hide."""

    __tablename__ = "outpost_instances"
    __table_args__ = (UniqueConstraint("signing_key", name="uq_outpost_instances_signing_key"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    outpost_id: Mapped[str] = mapped_column(ForeignKey("outposts.id", ondelete="CASCADE"), index=True)
    #: An `OutpostInstanceStatus`: `pending` until a person approves it against its fingerprint.
    status: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    #: Ed25519, raw 32 bytes as unpadded base64url: verifies every request of this instance.
    signing_key: Mapped[str] = mapped_column(String(43))
    #: X25519, same spelling: the calls this instance claims are sealed to it.
    sealing_key: Mapped[str] = mapped_column(String(43))
    #: `SHA256:` and the digest of the signing key: what the daemon printed and a person compares.
    fingerprint: Mapped[str] = mapped_column(String(50))
    #: What it said of itself at enrolment: stored as data, never interpreted (ADR 0148 §9).
    facts: Mapped[dict[str, str]] = mapped_column(Json, default=dict)
    daemon_version: Mapped[str] = mapped_column(String(64))
    #: The door protocol it spoke last.
    protocol: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    #: What it announces (`OutpostCapability`).
    capabilities: Mapped[list[str]] = mapped_column(Json, default=list)
    #: The aliases its policy file serves, as its last poll announced them: names only (ADR 0147 §7).
    endpoints: Mapped[list[str]] = mapped_column(Json, default=list)
    #: The last sequence number the door took from it: a request not above it is a replay (ADR 0148 §4).
    #: 64 bits: the contract allows 1 to 2^53 - 1, and a daemon raises it to its clock in milliseconds.
    sequence: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The one poll this instance has waiting (ADR 0148 §7): a token only that poll knows, and when the
    #: slot lapses by itself. It is on the row because the replicas share nothing else: a second poll
    #: meets it at whichever replica it arrives. Emptied when the poll answers or its client goes away;
    #: a replica that dies leaves it to lapse.
    poll_holder: Mapped[str | None] = mapped_column(String(32), nullable=True)
    poll_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When a second poll under this key last raised an alert: one a minute at most, however many
    #: conflicts a stolen key or a daemon in a loop produces.
    poll_conflict_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enrolled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OutpostEnrolment(Base, PkMixin, TimestampMixin):
    """An enrolment token: bound to one outpost, single use, valid 15 minutes at most, kept as a
    digest (ADR 0147 §4). `used_at` is written with the instance it enrolled, in one transaction: a
    refused enrolment does not use the token up."""

    __tablename__ = "outpost_enrolments"
    __table_args__ = (UniqueConstraint("token_digest", name="uq_outpost_enrolments_token_digest"),)

    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    outpost_id: Mapped[str] = mapped_column(ForeignKey("outposts.id", ondelete="CASCADE"), index=True)
    #: SHA-256 of the token, lowercase hex. The token itself is shown once and never stored.
    token_digest: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The instance it enrolled, once used.
    instance_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(200), nullable=True)


class OutpostCall(Base, TimestampMixin):
    """A call through an outpost: a row of the rendezvous, with a short life (ADR 0149 §4).

    It holds references and digests. Its arguments wait for their claim sealed under the key wrapper
    (ADR 0103; the five `arguments_*` columns below, the envelope of `org_secrets`) and are emptied
    when the call completes; the digests stay (ADR 0148 §11). The primary key is the call's id: a
    write's is derived from its effect's key, so a retried effect meets the first row (ADR 0149 §5).
    One row is also the one record of what the door took for the call (`recorded` and the three
    columns beside it): recorded once, read back by every later copy of the same answer.

    An answer crosses the door sealed to a key made for this one call (`answer_key`): the door keeps
    the envelope as it came (`answer_sealed`) for whoever placed the call, and the private half of
    that key waits beside the arguments, under the same key wrapper (`answer_key_*`). Both are
    emptied once the caller has read the answer."""

    __tablename__ = "outpost_calls"
    __table_args__ = (
        Index("ix_outpost_calls_outpost_state", "outpost_id", "state"),
        # What the cap of calls per minute reads (ADR 0148 §10): an outpost's calls of the last minute,
        # without walking every call it ever took.
        Index("ix_outpost_calls_outpost_created", "outpost_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    outpost_id: Mapped[str] = mapped_column(ForeignKey("outposts.id", ondelete="CASCADE"))
    #: The connector that routed it; null once that connector is gone.
    connector_id: Mapped[str | None] = mapped_column(
        ForeignKey("org_connectors.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: An `OutpostCallKind`.
    kind: Mapped[str] = mapped_column(String(32))
    #: An `OutpostCallState`.
    state: Mapped[str] = mapped_column(String(16), default="pending", server_default="pending")
    #: The alias it is sent to.
    endpoint: Mapped[str] = mapped_column(String(63))
    #: Null for a tool listing.
    tool: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: The platform took it for a write: it is delivered at most once (ADR 0149 §5).
    write: Mapped[bool] = mapped_column(Boolean, default=False)
    #: The instance that claimed it, the outpost's epoch at that moment, and when its lease ends.
    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("outpost_instances.id", ondelete="SET NULL"), nullable=True, index=True
    )
    epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When the platform stops waiting for it: unclaimed by then it is `expired`, and a lease never
    #: outlives it (`deadline` is the sooner of it and 300 seconds after the claim).
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: What the door recorded for this call, once: `answer` or `refusal` (the `recorded` of the door's
    #: `CallAck`), from which instance, under which epoch of the outpost, and when. It is written by
    #: one compare-and-set while `recorded` is null and never changed afterwards. A daemon sends its
    #: answer or its refusal again until the call's deadline, and again after a restart: it reads back
    #: what these four columns hold, whatever `state` has become since (a caller that opens an
    #: `unanswered` answer moves the state, never the record).
    recorded: Mapped[str | None] = mapped_column(String(8), nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recorded_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: An `OutpostAnswerOutcome`, for an answered call.
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_error: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    #: An `OutpostRefusalCode`, for a refused call.
    refusal: Mapped[str | None] = mapped_column(String(32), nullable=True)
    arguments_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: The governed action a write belongs to.
    action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    arguments_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    arguments_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    arguments_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    arguments_dek_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    arguments_kek_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: Who the platform says the call is made for (`OutpostAssertedNames`, ADR 0149 §3): names the
    #: outpost may log and decides nothing on. Emptied with the arguments.
    asserted: Mapped[dict[str, Any] | None] = mapped_column(Json, nullable=True)
    #: The X25519 key the answer is sealed to, public half: it travels inside the sealed call.
    answer_key: Mapped[str | None] = mapped_column(String(43), nullable=True)
    #: Its private half, in the envelope of `org_secrets` like the arguments. The door never opens it.
    answer_key_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    answer_key_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    answer_key_wrapped_dek: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    answer_key_dek_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    answer_key_kek_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: The answer as it crossed the door, still sealed: the door stores it and does not open it.
    answer_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)


class OutpostRun(Base, TimestampMixin):
    """An offer of the door's runs lane: a run, or a conversation's turn, placed on a person's runner
    (ADR 0226 §4). The primary key is the run's id, or the turn's: one offer per run, and a retried
    placement meets the first row.

    It is claimed once, by one instance, with `FOR UPDATE SKIP LOCKED` and a compare-and-set on a row no
    instance ever claimed (`claimed_at` is null), and never handed over again. The run token is minted at
    that claim and travels in the sealed envelope only: the row holds what the token names (`token_subject`,
    `project_slug`, `work_item_key`), because the door cannot read the run's row in a personal space, and
    never the token. `env` is the runner's environment beyond its id, address and token, never a secret.

    `state` is one of `offered`, `claimed`, `ended`, `lost`, `cancelled`, `expired`
    (`services.outpost_runs.OutpostRunState`). The rows of a personal outpost are under its restrictive
    policy (`20261127_1400_s87_16_outpost_runs`)."""

    __tablename__ = "outpost_runs"
    __table_args__ = (Index("ix_outpost_runs_outpost_state", "outpost_id", "state"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    org_id: Mapped[str] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    outpost_id: Mapped[str] = mapped_column(ForeignKey("outposts.id", ondelete="CASCADE"))
    #: The project the run belongs to: the owner's personal space in phase A (ADR 0226 §8).
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True, index=True
    )
    #: An `OfferKind`: `stage_run` or `conversation_turn`.
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), default="offered", server_default="offered")
    #: A `RunCredential`, and for a subscription the `SubscriptionProvider` the claiming instance holds.
    credential: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)
    #: The isolation placement chose (`container` for the docker mode, `process` for the native one).
    isolation: Mapped[str] = mapped_column(String(16))
    env: Mapped[dict[str, str]] = mapped_column(Json, default=dict)
    #: Where the runner calls the run's API: a run door (ADR 0226 §12).
    api_url: Mapped[str] = mapped_column(String(2000))
    #: What the run token minted at the claim names: its subject (the run's id, or `turn-…`), its project
    #: and its work item.
    token_subject: Mapped[str] = mapped_column(String(200))
    project_slug: Mapped[str] = mapped_column(String(200))
    work_item_key: Mapped[str] = mapped_column(String(512))
    #: A turn's conversation.
    conversation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    max_minutes: Mapped[int] = mapped_column(Integer)
    #: The lease a poll that lists the run renews, in seconds.
    lease_s: Mapped[int] = mapped_column(Integer)
    #: The instance that claimed it, the outpost's epoch then, when, and until when its lease runs.
    instance_id: Mapped[str | None] = mapped_column(
        ForeignKey("outpost_instances.id", ondelete="SET NULL"), nullable=True, index=True
    )
    epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: When it left `offered` or `claimed`, and the exit code its instance reported.
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: When a poll of the claiming instance carried it in `cancel_runs` after it was lost or withdrawn.
    cancel_told_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: Unclaimed by then, it is `expired`: the run's queue bound.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
