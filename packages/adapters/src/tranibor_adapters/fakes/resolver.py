# SPDX-License-Identifier: Apache-2.0
"""A resolver that answers from a table (ADR 0224 §6a): no DNS in a test, and a name can be made to
resolve to a private address, to several, or to none."""

from __future__ import annotations

from collections.abc import Mapping

from ..private_egress import is_literal


class FakeResolver:
    def __init__(self, table: Mapping[str, list[str]] | None = None) -> None:
        self.table = {k.lower(): list(v) for k, v in (table or {}).items()}
        self.asked: list[str] = []

    async def __call__(self, host: str) -> list[str]:
        self.asked.append(host)
        if is_literal(host):
            return [host.strip("[]")]
        return list(self.table.get(host.lower(), []))
