# SPDX-License-Identifier: Apache-2.0
"""Open lists: values the core starts with and an extension extends (ADR 0181, round C0).

Five lists of the contracts were closed enumerations although a trade names their values: the inbound
event types (`scm.pr.opened`), the finding types (`flaky-test`), the memory kinds (`hotspot`), the
connector kinds (`scm`) and the stage roles (`fix_ci`); a sixth, the platform's gestures on a system
transition (`does:`), is `workflow.STEP_EFFECTS`, and a seventh, the internal event types, was opened by
round D. The schemas hold a pattern for each, and this module holds the list: the core's values, and the
ones an extension declares from its `choregos.plugins` entry point. A value neither in the core's list
nor declared is refused.

Software delivery's values of the first four lists (`scm.*`, `ci.*`, `cd.*`, `bug`, `perf`, `tech-debt`,
`flaky-test`, `flaky_test`, `hotspot`, the kinds `scm`, `ci`, `cd`) and the release train's
`tranibor.release.*` (formerly `choregos.release.*`) left the core's lists with their enumeration members
(ADR 0181, windows C2, D-train and D, closed on 2026-11-23): only the `software-delivery` plugin's extension
declares them, under the same strings. A document the platform stored reads them whether or not it is
installed (`STORED`).

A core value is returned as its enumeration's member (`InboundEventType.ITEM_CREATED`), so code that
compares by identity keeps working; a declared value is returned as a string.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from enum import StrEnum
from typing import Any

from pydantic import ValidationInfo

from . import wire
from .enums import ConnectorKind, EventType, FindingType, InboundEventType, MemoryKind, StageRole

#: The validation context of a document read back from the platform's own storage (`{STORED: True}`).
STORED = "stored"


class OpenList:
    """The core's values of one list, and the ones extensions declared, each with its plugin."""

    def __init__(
        self,
        what: str,
        pattern: str,
        core: Iterable[str],
        enum: type[StrEnum] | None = None,
        spelling: Callable[[str], str] | None = None,
    ) -> None:
        self.what = what
        #: How a value written under a former name reads (ADR 0226): `choregos.run.finished` is the event
        #: type `tranibor.run.finished`. Applied to every value checked, declared or looked up.
        self._spelling = spelling or (lambda value: value)
        self.pattern = pattern
        self.core: frozenset[str] = frozenset(core)
        self._enum = enum
        self._compiled = re.compile(pattern)
        self._declared: dict[str, str] = {}
        for value in self.core:
            if not self._compiled.fullmatch(value):
                raise ValueError(f"the core's {what} `{value}` does not match {pattern}")

    def declare(self, value: str, *, plugin: str) -> None:
        """Declare a value for `plugin`. A value of the core may be declared too (a plugin that takes
        it over), and declaring again for the same plugin changes nothing; a value another plugin
        declared is refused, naming it: two plugins cannot mean two things by one name."""
        value = self._spelling(value)
        if not self._compiled.fullmatch(value):
            raise ValueError(f"the {self.what} `{value}` of the plugin `{plugin}` must match {self.pattern}")
        owner = self._declared.get(value)
        if owner is not None and owner != plugin:
            raise ValueError(f"the {self.what} `{value}` is already declared by the plugin `{owner}`")
        self._declared[value] = plugin

    def known(self) -> frozenset[str]:
        return self.core | frozenset(self._declared)

    def declared_by(self, plugin: str) -> frozenset[str]:
        """The values `plugin`'s extension declared."""
        return frozenset(value for value, owner in self._declared.items() if owner == plugin)

    def plugin_of(self, value: str) -> str | None:
        """The plugin that declared `value`; `None` for a value the core owns, or nobody's."""
        return self._declared.get(self._spelling(value))

    def forget(self, plugin: str) -> None:
        """Drop what `plugin` declared: what a test does after installing an extension."""
        self._declared = {value: owner for value, owner in self._declared.items() if owner != plugin}

    def validate(self, value: Any, info: ValidationInfo) -> Any:
        """The validator of a contract's field (`AfterValidator(LIST.validate)`): `check`, with the
        validation context, which says whether the document is one the platform stored."""
        return self.check(value, info)

    def check(self, value: Any, info: ValidationInfo | None = None) -> Any:
        """The validator of a contract's field: the enumeration's member for a value of the core, the
        string for a declared one; anything else is refused, naming the list.

        A document the platform already stored is read with the context `{"stored": True}`: a value
        that matches the pattern is then read even when no installed extension declares it (ADR 0098
        §4, readers accept). A stored workflow that names a plugin's step effect keeps parsing after
        the plugin is gone, and its Temporal history keeps replaying; the effect itself is then refused
        when it is reached, naming it."""
        text = self._spelling(str(value))
        if text in self.core:
            return self._enum(text) if self._enum is not None else text
        if text in self._declared:
            return text
        if info is not None and (info.context or {}).get(STORED) and self._compiled.fullmatch(text):
            return text
        raise ValueError(
            f"unknown {self.what}: {text!r} (the core's: {', '.join(sorted(self.core))}; an extension "
            f"declares more{', declared: ' + ', '.join(sorted(self._declared)) if self._declared else ''})"
        )


#: `<source>.<object>.<event>`: a tracker's, a source control's, a pipeline's, an alert's.
INBOUND_EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
FINDING_TYPE_PATTERN = r"^[a-z][a-z0-9-]{0,31}$"
MEMORY_KIND_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
CONNECTOR_KIND_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"
#: `tranibor.<object>.<event>`: an internal event's type (CloudEvents `type`). Open since round D, when the
#: release train's types became the `software-delivery` plugin's. The former `choregos.` prefix is accepted
#: and read as the new one until 0.32.0 (ADR 0226, R9c).
EVENT_TYPE_PATTERN = r"^(?:tranibor|choregos)(\.[a-z][a-z0-9_]*)+$"
#: The pattern of an agent's `role` in the workflow schema.
STAGE_ROLE_PATTERN = r"^[a-z][a-z0-9_]{1,31}$"

INBOUND_EVENT_TYPES = OpenList(
    "inbound event type",
    INBOUND_EVENT_TYPE_PATTERN,
    (v.value for v in InboundEventType),
    InboundEventType,
)
FINDING_TYPES = OpenList(
    "finding type",
    FINDING_TYPE_PATTERN,
    (v.value for v in FindingType),
    FindingType,
)
MEMORY_KINDS = OpenList(
    "memory kind",
    MEMORY_KIND_PATTERN,
    (v.value for v in MemoryKind),
    MemoryKind,
)
CONNECTOR_KINDS = OpenList(
    "connector kind",
    CONNECTOR_KIND_PATTERN,
    (v.value for v in ConnectorKind),
    ConnectorKind,
)
EVENT_TYPES = OpenList(
    "event type",
    EVENT_TYPE_PATTERN,
    (v.value for v in EventType),
    EventType,
    wire.event_type,
)
STAGE_ROLES = OpenList("stage role", STAGE_ROLE_PATTERN, (v.value for v in StageRole), StageRole)
