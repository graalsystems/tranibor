"""What the retention tests share (ADR 0221): SQLite has no trigger, so the chain's link is done by an ORM
listener that does what `choregos_audit_link` does (the next place, the previous hash, the hash, the head
moved), and a clock that makes `kits.audit_export.write_chain`'s entries old."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta

from sqlalchemy import event, select, update
from tranibor_api.audit_chain import GENESIS, Link, link_hash
from tranibor_api.db.base import uuid7
from tranibor_api.db.models import AuditChainHead, AuditLog

from .audit_export import T0

#: `write_chain` gives entry `n` the time `T0 + n seconds`: with this clock and a rule of 30 days, entries 1
#: to 5 are older than the rule and entries 6 and after are not.
CLOCK = T0 + timedelta(days=30, seconds=5.5)


@contextmanager
def trigger_on_sqlite() -> Iterator[None]:
    """Link every entry inserted from now on, as PostgreSQL's trigger does."""

    def link(_mapper: object, connection: object, target: AuditLog) -> None:
        if target.hash is not None:  # written already linked, by `write_chain`
            return
        target.id = target.id or uuid7()
        chain = target.org_id or ""
        head = connection.execute(select(AuditChainHead).where(AuditChainHead.chain == chain)).first()  # type: ignore[attr-defined]
        seq, prev = (head.seq, head.hash) if head is not None else (0, GENESIS)
        target.seq, target.prev_hash = seq + 1, prev
        entry = Link(
            id=target.id,
            org_id=target.org_id,
            seq=target.seq,
            ts=target.ts,
            actor_kind=target.actor_kind,
            actor_id=target.actor_id,
            action=target.action,
            target_type=target.target_type,
            target_id=target.target_id,
            payload=json.dumps(target.payload),
            prev_hash=prev,
            hash=None,
        )
        target.hash = link_hash(prev, entry)
        if head is None:
            connection.execute(  # type: ignore[attr-defined]
                AuditChainHead.__table__.insert().values(
                    chain=chain, org_id=target.org_id, seq=target.seq, hash=target.hash
                )
            )
        else:
            connection.execute(  # type: ignore[attr-defined]
                update(AuditChainHead)
                .where(AuditChainHead.chain == chain)
                .values(seq=target.seq, hash=target.hash)
            )

    event.listen(AuditLog, "before_insert", link)
    try:
        yield
    finally:
        event.remove(AuditLog, "before_insert", link)
