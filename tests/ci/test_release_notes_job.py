"""The release page's notes are bounded and reach the action as a file.

v0.29.4's `changelog` job failed twice with "Argument list too long": main had been rewritten after
v0.29.3, `git-cliff --latest` took the whole history and `action-gh-release` received it as its
`body` input. These tests read the job: a range computed by `tools/release_notes.py`, the notes in
a file, a size cap before the release step, and no step handed the notes as an input. The tool
itself is played on small repositories in `tests/tools/test_release_notes.py`.
"""

from __future__ import annotations

import pathlib

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def _changelog_steps() -> list[dict]:  # type: ignore[type-arg]
    jobs = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))["jobs"]
    return jobs["changelog"]["steps"]  # type: ignore[no-any-return]


def _index(steps: list[dict], needle: str) -> int:  # type: ignore[type-arg]
    return next(i for i, step in enumerate(steps) if needle in yaml.safe_dump(step))


def test_the_notes_start_from_a_computed_base_and_travel_as_a_file() -> None:
    steps = _changelog_steps()
    base = steps[_index(steps, "release_notes.py base")]
    assert base["id"] == "notes-base"
    assert '>> "$GITHUB_OUTPUT"' in base["run"]
    cliff = steps[_index(steps, "git-cliff-action")]
    args = cliff["with"]["args"]
    assert "--latest" not in args
    assert "${{ steps.notes-base.outputs.range }}" in args
    assert "--output release-notes.md" in args
    capped = _index(steps, "release_notes.py cap")
    assert "release-notes.md" in steps[capped]["run"]
    publish = _index(steps, "action-gh-release")
    assert _index(steps, "git-cliff-action") < capped < publish
    inputs = steps[publish]["with"]
    assert inputs["body_path"] == "release-notes.md"
    assert "body" not in inputs
    assert inputs["previous_tag"] == "${{ steps.notes-base.outputs.previous_tag }}"
    assert "previous_tag != ''" in inputs["generate_release_notes"]


def test_no_step_receives_the_notes_as_an_input() -> None:
    for step in _changelog_steps():
        assert "outputs.content" not in yaml.safe_dump(step.get("with", {}))
        assert "outputs.content" not in yaml.safe_dump(step.get("env", {}))
