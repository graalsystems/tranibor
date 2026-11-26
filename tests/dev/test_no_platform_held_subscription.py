# SPDX-License-Identifier: Apache-2.0
"""S87-56: no deployment file holds a person's model subscription (ADR 0227).

The demonstration bench used to copy a person's Claude Code token from their local credentials file into
a cluster-wide secret, `agent-creds`, that every run's pod mounted: one person's subscription serving
every run of the platform, which the vendors' terms forbid, and the token ended in Temporal's payloads.
A subscription is used only by its owner's personal runner, on their own machine. This test reads every
file a deployment is made from (the charts, the demonstration, the compose files and their example
environment, the examples, the personal bundle) and refuses the variable that carries one, the name of
that secret, and the key of the credentials file.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
#: What a deployment is made from.
PLACES = [
    ROOT / "charts",
    ROOT / "demo",
    ROOT / "examples",
    ROOT / "deploy",
    ROOT / "compose.yaml",
    ROOT / ".env.example",
]
#: The variable of a Claude subscription, the bench's former secret, the credentials file's key.
FORBIDDEN = ("CLAUDE_CODE_OAUTH_TOKEN", "agent-creds", "claudeAiOauth")
#: Files that are not configuration.
SKIPPED_SUFFIXES = (".png", ".jpg", ".svg", ".lock", ".gz", ".zip", ".tgz", ".pyc")


def _files() -> list[Path]:
    found: list[Path] = []
    for place in PLACES:
        if place.is_file():
            found.append(place)
        elif place.is_dir():
            found += [
                p
                for p in sorted(place.rglob("*"))
                if p.is_file() and not p.name.endswith(SKIPPED_SUFFIXES) and "node_modules" not in p.parts
            ]
    return found


def test_no_deployment_file_names_a_subscription_secret() -> None:
    files = _files()
    assert len(files) > 50, "the deployment files were not found: the test checks nothing"
    offences = [
        f"{path.relative_to(ROOT)}: {word}"
        for path in files
        for word in FORBIDDEN
        if word in path.read_text("utf-8", errors="ignore")
    ]
    assert not offences, (
        "a deployment file holds a person's model subscription; a subscription stays on its owner's machine, "
        f"in their personal runner (ADR 0227): {offences}"
    )
