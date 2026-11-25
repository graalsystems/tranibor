#!/usr/bin/env python3
"""The former name's ratchet (ADR 0226, Consequences): `choregos` only goes down, file by file.

    uv run python tools/former_name.py            # check the tree against its baseline (`make lint`)
    uv run python tools/former_name.py --write    # write the baseline from the tree, then review its diff
    uv run python tools/former_name.py --report   # the totals, per top-level directory

ADR 0226 gives every name of the former product its `tranibor` twin, reads both for a window, then
removes the former one. What may still say `choregos` is the compatibility code of each open window
and the history. This tool counts, for every text file of the repository, its case-insensitive
occurrences of the former name, and holds each count to a ceiling written in
`tools/former_name_baseline.tsv`:

- a file above its ceiling, or a file the baseline does not list that holds the name, fails: a new
  occurrence was written;
- a file below its ceiling, or a file the baseline lists that is gone or holds the name no more, fails
  too: the ceiling must come down with it (`--write`), so that what was removed cannot come back.

History is not counted at all (`HISTORY`): the ADRs, the plan, changelogs, the database migrations and
`trials/` say what was true when they were written. `--write` writes the counts as they are; a ceiling
that goes up, or a file that enters, shows in the baseline's diff and is justified in the pull request
(ADR 0099, like any ceiling). `tests/docs/test_former_name.py` runs the same check.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable, Mapping

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools" / "former_name_baseline.tsv"
FORMER = re.compile(r"choregos", re.IGNORECASE)
#: Paths that are history, never counted: what they say was true when it was written.
HISTORY = (
    "docs/adr/",
    "docs/plan/",
    "trials/",
    "apps/api/src/tranibor_api/migrations/versions/",
    # Recorded Temporal histories: evidence of a run, replayed as they were recorded (ADR 0008).
    "tests/replay/histories/",
)
#: A changelog, or a migration directory of any package, wherever it lives.
GIT = shutil.which("git") or "git"
HISTORY_NAMES = re.compile(r"(^|/)CHANGELOG[^/]*$|(^|/)migrations/versions/", re.IGNORECASE)
HEADER = (
    "# The former name's ceilings (ADR 0226): occurrences of `choregos` per file, case-insensitive.\n"
    "# A count only goes down. Regenerate with `uv run python tools/former_name.py --write`, then review\n"
    "# the diff: a ceiling that rises, or a file that enters, is justified in the pull request.\n"
)


def is_history(path: str) -> bool:
    return path.startswith(HISTORY) or HISTORY_NAMES.search(path) is not None


def tracked(root: pathlib.Path = ROOT) -> list[str]:
    """The repository's files, tracked or new and not ignored: what a commit would hold."""
    listed = subprocess.run(  # noqa: S603 - a fixed program and fixed arguments
        [GIT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return sorted({name for name in listed.split("\0") if name})


def occurrences(text: str) -> int:
    return len(FORMER.findall(text))


def count(root: pathlib.Path = ROOT, files: Iterable[str] | None = None) -> dict[str, int]:
    """Each counted text file that holds the former name, with its number of occurrences."""
    baseline = BASELINE.relative_to(ROOT).as_posix()
    counts: dict[str, int] = {}
    for name in files if files is not None else tracked(root):
        path = root / name
        if name == baseline or is_history(name) or path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        if b"\0" in raw:
            continue  # binary
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        found = occurrences(text)
        if found:
            counts[name] = found
    return counts


def parse(text: str) -> dict[str, int]:
    ceilings: dict[str, int] = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        ceiling, name = line.split("\t", 1)
        ceilings[name] = int(ceiling)
    return ceilings


def read_baseline(path: pathlib.Path = BASELINE) -> dict[str, int]:
    return parse(path.read_text(encoding="utf-8")) if path.is_file() else {}


def render(counts: Mapping[str, int]) -> str:
    return HEADER + "".join(f"{counts[name]}\t{name}\n" for name in sorted(counts))


def problems(counts: Mapping[str, int], ceilings: Mapping[str, int]) -> list[str]:
    """What breaks the ratchet: a new occurrence, or a ceiling left above the count it held."""
    found: list[str] = []
    for name in sorted(set(counts) | set(ceilings)):
        now, ceiling = counts.get(name, 0), ceilings.get(name)
        if ceiling is None:
            found.append(
                f"{name}: {now} occurrence(s) of the former name in a file the baseline does not list"
            )
        elif now > ceiling:
            found.append(f"{name}: {now} occurrences of the former name, above its ceiling of {ceiling}")
        elif now < ceiling:
            gone = "the file holds it no more" if now == 0 else f"{now} left"
            found.append(f"{name}: stale ceiling of {ceiling} ({gone}): lower it with --write")
    return found


def report(counts: Mapping[str, int]) -> str:
    areas: Counter[str] = Counter()
    for name, found in counts.items():
        areas[name.split("/", 1)[0] if "/" in name else "."] += found
    lines = [f"{found:>7}  {area}" for area, found in areas.most_common()]
    return "\n".join([*lines, f"{sum(counts.values()):>7}  in {len(counts)} files"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true", help="write the baseline from the tree")
    parser.add_argument("--report", action="store_true", help="print the totals per top-level directory")
    args = parser.parse_args(argv)
    counts = count()
    if args.report:
        print(report(counts))
    if args.write:
        before = read_baseline()
        BASELINE.write_text(render(counts), encoding="utf-8")
        raised = sorted(name for name, found in counts.items() if found > before.get(name, 0))
        for name in raised:
            print(
                f"former-name: {name} rises to {counts[name]} (was {before.get(name, 0)}): say why in the PR"
            )
        print(f"former-name: {BASELINE.relative_to(ROOT)} written, {sum(counts.values())} occurrences")
        return 0
    found = problems(counts, read_baseline())
    for problem in found:
        print(f"former-name: {problem}", file=sys.stderr)
    if found:
        print(
            "former-name: write the new name (ADR 0226), or run `tools/former_name.py --write`",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
