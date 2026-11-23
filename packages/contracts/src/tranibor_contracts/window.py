# SPDX-License-Identifier: Apache-2.0
"""The window list: every former name a round keeps beside its successor, and the release that removes it.

ADR 0181 renames the core's own concepts in rounds, before the 1.0 freeze. A round adds the neutral
name beside the former one: a response carries both, a request accepts either, a reader of a stored
value takes both spellings (ADR 0098 §4). The former name then stays for one window and leaves in its
own pull request, at least two weeks after the round's release (ADR 0181, Decision (2026-11-19) point
6). There is no alias policy any more (ADR 0098): this list is the only place a former name may be
kept, and `tests/architecture/test_the_window_list.py` holds it both ways:

- every entry names a place where its former name is today, so an entry whose name already left is
  refused, and the list shrinks with the names;
- in the release that removes an entry's window, the name must be gone from that place: the release
  that should remove it cannot ship with it;
- the list is empty on 1.0, so the frozen surfaces carry no former name (ADR 0168).

The vocabulary guard (`tests/architecture/test_contracts_speak_no_trade.py`) does not read this file:
it names each former name once, on purpose, and its own test removes them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Literal

__all__ = [
    "C1",
    "WINDOW",
    "FormerName",
    "Window",
    "opening_release",
    "release",
    "removal_due",
    "writers_due",
]


#: The least a consumer (the enterprise edition, a plugin, a deployment's values) is given to switch: the
#: removal ships at least two weeks after the release that opened the window (ADR 0181, 2026-11-19).
GRACE = timedelta(days=14)


@dataclass(frozen=True, slots=True)
class Window:
    """One round's window, keyed on the releases actually cut, not on version numbers guessed ahead.

    Both names are served from the first release cut after the day the round merged (`opened_on`). The writers
    switch in the first release cut on or after `writers_switch_on`; the former names leave in the first
    release cut on or after `removed_on`, provided the release that opened the window is two weeks old
    by then. Whether a release is a patch or a minor does not matter. The releases and their dates are
    the release ledger (`tools/releases.json`), which `tools/bump_version.py` writes when it sets a
    version: the window test reads it, so it changes its verdict only in a release's own pull request,
    never on a calendar day."""

    round: str
    #: The day the round merged: the first release cut after that day serves both names.
    opened_on: date
    #: From the first release cut on or after this day, the writers of a stored value write the
    #: neutral spelling (ADR 0098 §4).
    writers_switch_on: date
    #: The first release cut on or after this day no longer has the former names. Its pull request
    #: removes them, rewrites the stored rows that still hold them, and takes their entries off this list.
    removed_on: date


def release(name: str) -> tuple[int, ...]:
    """`0.30.0` -> (0, 30, 0); a pre-release suffix (`0.30.0rc1`) counts as its release."""
    import re

    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", name)
    if match is None:
        raise ValueError(f"`{name}` is not a release number")
    return tuple(int(part) for part in match.groups())


def _cut(releases: Mapping[str, date], version: str) -> list[tuple[str, date]]:
    """The releases of the ledger up to `version`, in order."""
    return sorted(
        ((name, day) for name, day in releases.items() if release(name) <= release(version)),
        key=lambda item: release(item[0]),
    )


def opening_release(window: Window, releases: Mapping[str, date], version: str) -> tuple[str, date] | None:
    """The first release, up to `version`, cut after the day the round merged; `None` before it. A release
    of that same day may have been cut before the merge: the window then opens one release later, which
    only makes it longer."""
    return next(((name, day) for name, day in _cut(releases, version) if day > window.opened_on), None)


def _due(window: Window, releases: Mapping[str, date], version: str, on: date, grace: timedelta) -> bool:
    opened = opening_release(window, releases, version)
    if opened is None:
        return False
    return any(
        release(name) > release(opened[0]) and day >= on and day >= opened[1] + grace
        for name, day in _cut(releases, version)
    )


def writers_due(window: Window, releases: Mapping[str, date], version: str) -> bool:
    """Whether a release up to `version` (one after the opening) is the writers' switch, or after it."""
    return _due(window, releases, version, window.writers_switch_on, timedelta(0))


def removal_due(window: Window, releases: Mapping[str, date], version: str) -> bool:
    """Whether a release up to `version` is the one that removes the window's former names, or after it."""
    return _due(window, releases, version, window.removed_on, GRACE)


#: Round C1 of ADR 0181: neutral names for concepts the core owns. Merged on 2026-11-19, so served from
#: 0.27.17 (2026-11-20).
C1 = Window(
    round="C1",
    opened_on=date(2026, 11, 19),
    writers_switch_on=date(2026, 11, 30),
    removed_on=date(2026, 12, 7),
)

Surface = Literal["python", "rest", "dsl", "stage-result", "sign-in"]


@dataclass(frozen=True, slots=True)
class FormerName:
    """A former name, where it is today, and what replaces it.

    `where` is a locator the window test resolves:

    - `py:<module>:<Attr>.<attr>`, an attribute; `[<key>]` at the end asks whether a mapping has that key;
    - `literal:<module>:<Model>.<field>`, the values of a `Literal` field (with `value`);
    - `openapi:<json pointer>` in `openapi.yaml`, `schema:<file>#<json pointer>` in `schemas/`; with
      `value`, the name is that value of the list the pointer reaches (an `enum`).
    """

    name: str
    successor: str
    surface: Surface
    where: str
    window: Window
    value: str | None = None


#: Every former name kept for a window, with its successor and its window.
WINDOW: tuple[FormerName, ...] = ()
