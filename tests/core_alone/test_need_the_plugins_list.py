"""The files of the core's suite that still need a first-party plugin's extension (ADR 0181, round D).

The nightly `core-without-plugins` job runs the core's whole suite without the extensions, less the files
`need_the_plugins.txt` lists. The list is a ceiling, as the ratchets of ADR 0099 are: a file leaves it once
it passes without the plugins (its software-delivery tests moved to `plugins/software-delivery/tests`, or
the core stopped needing the plugin), and none is added. These checks run everywhere.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
LIST = pathlib.Path(__file__).with_name("need_the_plugins.txt")

#: The number of files the list held when it was written (S83-05a, 2026-11-21: 435 files, 2,632 tests). It
#: only falls: lower it with each file that leaves the list.
CEILING = 435


def listed() -> list[str]:
    return [
        line.strip()
        for line in LIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]


def test_every_listed_file_is_a_test_file_of_the_core() -> None:
    stale = [path for path in listed() if not (ROOT / path).is_file()]
    assert not stale, f"listed but absent (moved or deleted: remove the line): {stale}"
    assert all(path.split("/")[-1].startswith("test_") and path.endswith(".py") for path in listed())
    # The plugins' own tests run with their extension; they are never the core's.
    assert not [path for path in listed() if path.startswith("plugins/")]


def test_the_list_is_sorted_and_holds_each_file_once() -> None:
    assert listed() == sorted(set(listed()))


def test_the_list_only_falls() -> None:
    assert len(listed()) <= CEILING, (
        f"{len(listed())} files need a plugin, the ceiling is {CEILING}: a core test that needs "
        "`software-delivery` belongs to `plugins/software-delivery/tests`"
    )
