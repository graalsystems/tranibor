# SPDX-License-Identifier: Apache-2.0
"""The one line the relay keeps per phone connection, and nothing else (ADR 0230 §7).

Every line is validated twice before it is written: by the contract's model and by the JSON Schema
`relay-metadata-line.schema.json`. It names the installation (or none, when the connection ended
before one was known), when, from which address, how many bytes each way, how long and why it ended:
no server name, no ALPN, no cipher list, no byte of a stream. The relay logs nothing else.

The retention is the operator's, and the relay refuses to start without one; `prune` deletes the
lines older than it.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any

import tranibor_contracts as contracts
from jsonschema import Draft202012Validator
from tranibor_contracts.relay import RelayMetadataLine


class RelayConfigError(ValueError):
    """A relay configuration the relay refuses to start with."""


@cache
def _validator() -> Draft202012Validator:
    return Draft202012Validator(
        contracts.load_schema("relay-metadata-line.schema.json"),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def validated(line: RelayMetadataLine) -> dict[str, Any]:
    """The line as it is written, once the schema accepts it."""
    document = line.model_dump(mode="json")
    errors = sorted(error.message for error in _validator().iter_errors(document))
    if errors:
        raise ValueError(f"a metadata line the schema refuses: {'; '.join(errors)}")
    return document


class MetadataLog:
    """The relay's metadata lines: one JSON object per line, appended, pruned past the retention."""

    def __init__(self, path: Path, retention: timedelta | None, now: Callable[[], float]) -> None:
        if retention is None or retention <= timedelta(0):
            raise RelayConfigError(
                "a relay keeps its metadata lines for a fixed retention, which its operator sets; none is set"
            )
        self.path = path
        self.retention = retention
        self._now = now
        self._lock = asyncio.Lock()

    async def write(self, line: RelayMetadataLine) -> None:
        text = json.dumps(validated(line), separators=(",", ":")) + "\n"
        async with self._lock:
            await asyncio.to_thread(self._append, text)

    def _append(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as log:
            log.write(text)

    async def prune(self) -> int:
        """Deletes the lines that ended before the retention; how many it deleted. A line that does not
        read as one is deleted too: it cannot be dated, so it cannot be kept."""
        cutoff = datetime.fromtimestamp(self._now(), UTC) - self.retention
        async with self._lock:
            return await asyncio.to_thread(self._prune, cutoff)

    def _prune(self, cutoff: datetime) -> int:
        if not self.path.exists():
            return 0
        kept: list[str] = []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        for text in lines:
            try:
                fresh_enough = RelayMetadataLine.model_validate_json(text).ended_at >= cutoff
            except (ValueError, TypeError):  # not a line, or a time without its zone
                continue
            if fresh_enough:
                kept.append(text + "\n")
        if len(kept) != len(lines):
            fresh = self.path.with_name(self.path.name + ".pruning")
            fresh.write_text("".join(kept), encoding="utf-8")
            os.replace(fresh, self.path)
        return len(lines) - len(kept)
