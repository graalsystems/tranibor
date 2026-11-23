# SPDX-License-Identifier: Apache-2.0
"""Former spellings the platform still READS, from documents it recorded or stored and cannot rewrite.

ADR 0181's windows closed on 2026-11-23, ahead of their dates (the project lead's decision, recorded in
the ADR): no contract writes or accepts a former name any more, and the rows that held one were
rewritten by Alembic data migrations. What no migration can reach stays readable (ADR 0098 §4):

- a stored workflow version is never rewritten, because its digest would move, and a running
  workflow carries its definition in its Temporal history, which is never rewritten either (ADR 0008).
  So a workflow read back from storage (the validation context `{"stored": True}`) reads an action
  approver's former role as its successor; a document sent to the API is refused with it.

Each spelling here is read only, never written; the vocabulary guard does not read this module, for that
reason (`tests/architecture/test_contracts_speak_no_trade.py`).
"""

from __future__ import annotations

__all__ = ["FORMER_APPROVER_ROLES"]

#: An action approver's former role, read as its successor in a stored or recorded workflow (ADR 0181 C1).
FORMER_APPROVER_ROLES: dict[str, str] = {"developer": "contributor", "release_captain": "approver"}
