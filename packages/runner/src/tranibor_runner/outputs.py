# SPDX-License-Identifier: Apache-2.0
"""Binary deliverables: what an agent writes in `.tranibor/outputs/` (S31-05, ADR 0049).

An agent that produces a slide deck, a spreadsheet, a document, an image or a video writes it in
`.tranibor/outputs/`. An agent built on an older SDK, or a job image, may still write the former
`.choregos/outputs/` until 0.32.0 (ADR 0226, R9a): both folders are collected, and a file name found in
both is taken from the new one. The runner hands each file to the internal API, which hashes it, stores it in
the object store and records it as a typed artifact; the runner then lists what was stored in the
result's `artifacts.files` — itself, never the agent.

The platform does not generate these files: an office file comes from a skill run in the sandbox,
an image or a video from a model behind the gateway (whose cost the gateway meters) or from an MCP
connector. The folder is excluded from git: deliverables are artifacts, not commits.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tranibor_contracts import (
    ARTIFACT_NAME_PATTERN,
    ARTIFACT_TYPES,
    RESERVED_ARTIFACT_NAMES,
    ArtifactFile,
    StageInput,
    artifact_digest,
    type_of_filename,
)
from tranibor_contracts.wire import run_path, run_paths

from . import safe_files

OUTPUTS_DIR = run_path("outputs")
#: Both folders, the new one first: a file name in both is the new folder's.
OUTPUTS_DIRS = run_paths("outputs")
_NAME = re.compile(ARTIFACT_NAME_PATTERN)


def expected_files(stage_input: StageInput) -> dict[str, str]:
    """{declared file output: the file name the agent must write}, e.g. `{"deck": "deck.pptx"}`."""
    files: dict[str, str] = {}
    for name in stage_input.transition.file_outputs():
        extension = ARTIFACT_TYPES[stage_input.transition.output_types[name]].extension
        files[name] = f"{name}.{extension}" if extension and not name.endswith(f".{extension}") else name
    return files


def missing_files(outputs_dir: Path, expected: dict[str, str]) -> list[str]:
    """The expected file names found in neither folder; `outputs_dir` is `<workspace>/.tranibor/outputs`."""
    workspace = outputs_dir.parent.parent
    return [
        filename
        for filename in expected.values()
        if not any(safe_files.is_regular(workspace, Path(folder) / filename) for folder in OUTPUTS_DIRS)
    ]


def collect(workspace: Path, stage_input: StageInput) -> tuple[list[tuple[str, str, Path]], list[str]]:
    """The files to store, as (artifact name, type, path), and the ones refused with why.

    A declared output keeps its declared name and type; any other file is stored under its own file
    name, typed by its extension. Only regular files directly in the folder: a symbolic link could
    point anywhere in the pod, a sub-folder has no artifact name. Both folders are read, the new one
    first; a name already taken from it is not read again from the former."""
    declared = {filename: name for name, filename in expected_files(stage_input).items()}
    kept: list[tuple[str, str, Path]] = []
    refused: list[str] = []
    seen: set[str] = set()
    for outputs in OUTPUTS_DIRS:
        folder = workspace / outputs
        if not folder.is_dir():
            continue
        try:
            regular, other = safe_files.regular_files(workspace, outputs)
        except OSError:
            refused.append(f"{outputs}: is a link, or sits behind one")
            continue
        refused.extend(f"{name}: not a regular file" for name in other if name not in seen)
        _classify(
            [folder / name for name in regular if name not in seen], declared, stage_input, kept, refused
        )
        seen.update(regular, other)
    return kept, refused


def _classify(
    paths: list[Path],
    declared: dict[str, str],
    stage_input: StageInput,
    kept: list[tuple[str, str, Path]],
    refused: list[str],
) -> None:
    for path in paths:
        if path.name in declared:
            name = declared[path.name]
            kept.append((name, stage_input.transition.output_types[name], path))
        elif path.name in RESERVED_ARTIFACT_NAMES:
            refused.append(f"{path.name}: this name is measured by the platform")
        elif _NAME.fullmatch(path.name):
            kept.append((path.name, type_of_filename(path.name), path))
        else:
            refused.append(f"{path.name}: not a valid artifact name")


async def store_outputs(
    client: Any, workspace: Path, stage_input: StageInput
) -> tuple[dict[str, ArtifactFile], list[str]]:
    """Hands every deliverable to the internal API; returns what it stored and what failed."""
    kept, problems = collect(workspace, stage_input)
    stored: dict[str, ArtifactFile] = {}
    for name, type_, path in kept:
        try:
            data = safe_files.read_bytes(workspace, path.relative_to(workspace))
        except OSError as exc:
            problems.append(f"{path.name}: {str(exc)[:200]}")
            continue
        digest = artifact_digest(data)
        try:
            await client.put_output(name, type_, data, digest)
        except Exception as exc:
            problems.append(f"{path.name}: {str(exc)[:200]}")
            continue
        stored[name] = ArtifactFile(
            filename=path.name,
            type=type_,
            media_type=ARTIFACT_TYPES[type_].media_type,
            digest=digest,
            size=len(data),
        )
    return stored, problems


def outputs_markdown(stage_input: StageInput) -> str:
    """The section of `.tranibor/task.md` that says which files to write, and where."""
    expected = expected_files(stage_input)
    if not expected:
        return ""
    types = stage_input.transition.output_types
    return (
        "\n".join(
            [
                "",
                "## Files to deliver",
                "Write each of these files in `.tranibor/outputs/` before you finish; a missing one "
                "fails the step. Produce office files with your skills, images and video with the "
                "models or tools you are given — then write the file there:",
                *[f"- `{OUTPUTS_DIR}/{filename}` ({types[name]})" for name, filename in expected.items()],
            ]
        )
        + "\n"
    )
