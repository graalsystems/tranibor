"""The range and the size of a release page's notes (`release.yml`, job `changelog`).

v0.29.4's page failed twice with "Argument list too long": main had been rewritten after v0.29.3,
so no release tag was an ancestor of v0.29.4 any more, `git-cliff --latest` took the whole history
(about 300 KB, 2,600 commits) and `action-gh-release` received it as its `body` input, that is as an
environment variable of the process the runner starts, past the kernel's limit. The page was
written by hand.

`base` names the commit the notes start from, and the job hands git-cliff that range:

* the nearest earlier release tag (by version) that is an ancestor of the tag;
* when none is (a rewritten history), the commit of the tag's history that carries the same
  author date and subject as the previous release tag's commit: a rewrite keeps both, and only
  that commit is its copy;
* when neither exists, the tag's commit alone: the notes cover the whole history, and `cap`
  bounds them.

`cap` keeps the notes file under `--max-bytes` (GitHub refuses a release body over 125,000
characters), cutting at a line and saying where the full list is. The notes reach the action as a
file (`body_path`), never as an argument or an input.

    python3 tools/release_notes.py base v0.29.4 >> "$GITHUB_OUTPUT"
    python3 tools/release_notes.py cap notes.md --max-bytes 60000 --full-list <compare URL>
"""

from __future__ import annotations

import argparse
import dataclasses
import pathlib
import re
import shutil
import subprocess
import sys

RELEASE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
#: Under GitHub's 125,000-character limit, with room for the notes GitHub generates beside them.
DEFAULT_MAX_BYTES = 60_000
GIT = shutil.which("git") or "git"


@dataclasses.dataclass(frozen=True)
class Base:
    """Where a release's notes start."""

    #: The git-cliff range: `<base>..<tag>`, or the tag's commit alone (the whole history).
    range: str
    #: The earlier release tag the range starts from, when it is an ancestor ('' otherwise).
    previous_tag: str
    #: `ancestor`, `rewritten` (the copy of the previous tag's commit) or `none`.
    kind: str


def _git(cwd: pathlib.Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 - a fixed program; the arguments are refs
        [GIT, *args], cwd=cwd, capture_output=True, text=True, check=True, encoding="utf-8"
    )
    return done.stdout


def _version(tag: str) -> tuple[int, int, int] | None:
    match = RELEASE_TAG.match(tag)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def earlier_release_tags(cwd: pathlib.Path, tag: str) -> list[str]:
    """The release tags of a lower version than `tag`, the nearest first."""
    own = _version(tag)
    if own is None:
        raise SystemExit(f"{tag!r} is not a release tag (vX.Y.Z)")
    tags = [t for t in _git(cwd, "tag", "--list", "v*").split() if (v := _version(t)) and v < own]
    return sorted(tags, key=lambda t: _version(t) or (0, 0, 0), reverse=True)


def _is_ancestor(cwd: pathlib.Path, ancestor: str, tag: str) -> bool:
    done = subprocess.run(  # noqa: S603 - a fixed program; the arguments are refs
        [GIT, "merge-base", "--is-ancestor", f"{ancestor}^{{commit}}", f"{tag}^{{commit}}"],
        cwd=cwd,
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


def _rewritten_copy(cwd: pathlib.Path, previous: str, tag: str) -> str:
    """The commit of `tag`'s history with the author date and subject of `previous`'s commit."""
    fingerprint = _git(cwd, "log", "-1", "--format=%aI%x00%s", f"{previous}^{{commit}}").strip()
    for line in _git(cwd, "log", "--format=%H%x00%aI%x00%s", f"{tag}^{{commit}}").splitlines():
        commit, _, rest = line.partition("\x00")
        if rest == fingerprint:
            return commit
    return ""


def base(cwd: pathlib.Path, tag: str) -> Base:
    earlier = earlier_release_tags(cwd, tag)
    for candidate in earlier:
        if _is_ancestor(cwd, candidate, tag):
            commit = _git(cwd, "rev-parse", f"{candidate}^{{commit}}").strip()
            return Base(range=f"{commit}..{tag}", previous_tag=candidate, kind="ancestor")
    if earlier:
        copy = _rewritten_copy(cwd, earlier[0], tag)
        if copy:
            return Base(range=f"{copy}..{tag}", previous_tag="", kind="rewritten")
    commit = _git(cwd, "rev-parse", f"{tag}^{{commit}}").strip()
    return Base(range=commit, previous_tag="", kind="none")


def cap(path: pathlib.Path, max_bytes: int, full_list: str) -> bool:
    """Cut `path` at a line under `max_bytes`, saying where the full list is; True when cut."""
    data = path.read_bytes()
    if len(data) <= max_bytes:
        return False
    footer = f"\n\n_The list is cut at {max_bytes} bytes: the full list is {full_list}_\n".encode()
    kept = data[: max(0, max_bytes - len(footer))]
    if b"\n" in kept:
        kept = kept[: kept.rindex(b"\n")]
    path.write_bytes(kept + footer)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    base_command = commands.add_parser("base", help="print the notes' range as GITHUB_OUTPUT lines")
    base_command.add_argument("tag")
    cap_command = commands.add_parser("cap", help="keep a notes file under a size")
    cap_command.add_argument("file", type=pathlib.Path)
    cap_command.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    cap_command.add_argument("--full-list", required=True, help="where the full list is (a URL)")
    args = parser.parse_args(argv)
    if args.command == "base":
        found = base(pathlib.Path.cwd(), args.tag)
        if found.kind == "none":
            print(f"::warning::no earlier release of {args.tag} found in its history", file=sys.stderr)
        print(f"range={found.range}")
        print(f"previous_tag={found.previous_tag}")
        print(f"kind={found.kind}")
        return 0
    if cap(args.file, args.max_bytes, args.full_list):
        print(f"::warning::the release notes were cut at {args.max_bytes} bytes", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
