#!/usr/bin/env python3
"""Writes `SHA256SUMS` and `manifest.json` beside the native CLI binaries (S62-07).

    python tools/cli_binary/release.py dist/cli --version 0.25.3
    python tools/cli_binary/release.py dist/cli --version 0.25.3 --partial   # fewer than five
    python tools/cli_binary/release.py dist/cli --former-names                # the GitHub release's copies

The file names and the manifest are a shared contract (S62-08 serves them from the instance, the
install scripts read the manifest):

    {"version": "x.y.z", "files": [{"name": "...", "os": "linux|macos|windows",
      "arch": "x86_64|arm64", "sha256": "...", "size": <bytes>}]}

`SHA256SUMS` is what `sha256sum -c` (`shasum -a 256 -c` on macOS) reads. A release refuses to write
them unless the five targets are all there: a release that silently lacked one would publish a
manifest whose missing platform nobody notices before a user does.

The files are named `<prefix>-<platform>` after the publishing identity (`tranibor-linux-x86_64`,
ADR 0224 R5). For the deprecation window the former names (`choregos-linux-x86_64`) stay readable:
`SHA256SUMS` gives each file's digest under both names, the instance serves a former name as the
current file (`/downloads/cli/`), and `--former-names` copies each file and its cosign bundle under
the former name, for the GitHub release only (the API image carries one copy). The manifest lists the
current names alone: an installed CLI picks its file by platform, never by name.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import shutil
import sys
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
#: The publishing identity (ADR 0156), read as a file: this script runs before the workspace is installed.
PUBLISHING_JSON = ROOT / "packages" / "core" / "src" / "tranibor_core" / "publishing.json"
#: (os, arch) -> the end of the file's name; `build.py` and `tranibor_core.publishing` hold the same.
PLATFORMS: dict[tuple[str, str], str] = {
    ("linux", "x86_64"): "linux-x86_64",
    ("linux", "arm64"): "linux-arm64",
    ("macos", "arm64"): "macos-arm64",
    ("macos", "x86_64"): "macos-x86_64",
    ("windows", "x86_64"): "windows-x86_64.exe",
}
_IDENTITY = json.loads(PUBLISHING_JSON.read_text(encoding="utf-8"))
PREFIX, FORMER_PREFIX = str(_IDENTITY["image_prefix"]), str(_IDENTITY["image_alias_prefix"])
#: name -> (os, arch): the five targets, the same names as `build.py`'s.
TARGETS: dict[str, tuple[str, str]] = {f"{PREFIX}-{end}": target for target, end in PLATFORMS.items()}
#: current name -> former name, for the deprecation window of ADR 0224 R5 (the same bytes).
FORMER_NAMES: dict[str, str] = {f"{PREFIX}-{end}": f"{FORMER_PREFIX}-{end}" for end in PLATFORMS.values()}
#: The cosign bundle beside each file (`cosign sign-blob --bundle <file>.sigstore.json`).
BUNDLE_SUFFIX = ".sigstore.json"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest(directory: pathlib.Path, version: str, *, partial: bool = False) -> dict[str, Any]:
    if not SEMVER.match(version):
        raise ValueError(f"not a release version: {version!r}")
    present = [name for name in TARGETS if (directory / name).is_file()]
    missing = sorted(set(TARGETS) - set(present))
    if missing and not partial:
        raise ValueError(f"binaries missing from {directory}: {missing}")
    if not present:
        raise ValueError(f"no binary in {directory}")
    files = []
    for name in present:
        path = directory / name
        os_name, arch = TARGETS[name]
        files.append(
            {"name": name, "os": os_name, "arch": arch, "sha256": sha256(path), "size": path.stat().st_size}
        )
    return {"version": version, "files": files}


def sha256sums(doc: dict[str, Any]) -> str:
    """Each file's digest under its name, then under its former name (ADR 0224 R5): a script that checks
    a `choregos-*` download with `sha256sum --ignore-missing -c SHA256SUMS` keeps working."""
    lines = [f"{entry['sha256']}  {entry['name']}\n" for entry in doc["files"]]
    lines += [
        f"{entry['sha256']}  {FORMER_NAMES[entry['name']]}\n"
        for entry in doc["files"]
        if entry["name"] in FORMER_NAMES
    ]
    return "".join(lines)


def former_copies(directory: pathlib.Path) -> list[str]:
    """Copies each file of the manifest, and its cosign bundle when there is one, under its former name.

    For the GitHub release: a former name keeps a download URL working through the window, and the
    bundle verifies the copy, since a cosign signature is over the bytes, not the name.
    """
    doc = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    written: list[str] = []
    for entry in doc["files"]:
        name = str(entry["name"])
        former = FORMER_NAMES.get(name)
        if former is None:
            continue
        for source, target in ((name, former), (name + BUNDLE_SUFFIX, former + BUNDLE_SUFFIX)):
            if (directory / source).is_file():
                shutil.copy2(directory / source, directory / target)
                written.append(target)
    return written


def write(directory: pathlib.Path, version: str, *, partial: bool = False) -> dict[str, Any]:
    doc = manifest(directory, version, partial=partial)
    (directory / "manifest.json").write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    (directory / "SHA256SUMS").write_text(sha256sums(doc), encoding="utf-8")
    return doc


def main() -> int:
    parser = argparse.ArgumentParser(description="SHA256SUMS and manifest.json for the CLI binaries.")
    parser.add_argument("directory", type=pathlib.Path)
    parser.add_argument("--version")
    parser.add_argument("--partial", action="store_true", help="accept fewer than the five targets")
    parser.add_argument(
        "--former-names",
        action="store_true",
        help="copy each file of the manifest, and its bundle, under its former name (ADR 0224 R5)",
    )
    args = parser.parse_args()
    if args.former_names:
        for name in former_copies(args.directory):
            print(f"copied as {name}")
        return 0
    if not args.version:
        parser.error("--version is required")
    try:
        doc = write(args.directory, args.version, partial=args.partial)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for entry in doc["files"]:
        print(f"{entry['sha256']}  {entry['name']}  ({entry['size']} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
