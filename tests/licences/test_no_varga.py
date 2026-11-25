"""The product does not name the Varga Foundation (ADR 0186).

The publisher is GRAAL.SYSTEMS SAS. A tracked file that spells "Varga" outside the allowlist below fails:
a copyright line, a footer, a contact address, a default source or an example that names the foundation
would bring it back. Each exception carries its reason.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
VARGA = re.compile("varga", re.IGNORECASE)

#: Prefixes: history the product does not rewrite.
HISTORY = (
    "docs/adr/",  # the decisions, past tense, and the amendments that retire them
    "docs/plan/",  # the plan's rows say what was done when
    "apps/api/src/tranibor_api/migrations/versions/",  # a migration means what it meant when it ran
    "trials/",  # dated reports
)

#: Files that still spell the name: the old source name, and the guards of the deleted frozen namespace.
ALLOWED = {
    "tests/licences/test_no_varga.py": "this guard",
    "tools/former_name_baseline.tsv": "the former name's ceilings (ADR 0224) list this guard's path",
    "packages/core/src/tranibor_core/plugins/sources.py": "resolves the old default source name",
    "packages/core/tests/test_plugin_sources.py": "tests that the old default source name resolves",
    "apps/api/tests/test_default_plugin_source_migration.py": "tests the rename of the old source name",
    "tests/charts/test_no_frozen_namespace_in_policies.py": "guards: no policy names the deleted namespace",
    "apps/api/tests/test_catalogue_provenance_migration.py": "an old migration records the old source name",
}


#: The registry namespace deleted on 2026-11-21: no manifest, workflow, Dockerfile or chart may pull from it.
FROZEN_NAMESPACE = re.compile(r"ghcr\.io/vargafoundation", re.IGNORECASE)

#: Files that name the namespace on purpose: the guards that keep it out.
NAMES_THE_NAMESPACE = {
    "tests/licences/test_no_varga.py",
    "tests/charts/test_no_frozen_namespace_in_policies.py",
}


def _tracked() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True).stdout
    return [name for name in out.decode().split("\0") if name]


def _holds_varga(name: str) -> bool:
    try:
        return bool(VARGA.search((ROOT / name).read_text(encoding="utf-8")))
    except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
        return False


def test_varga_is_named_only_where_the_allowlist_says() -> None:
    offenders = [
        name
        for name in _tracked()
        if not name.startswith(HISTORY) and name not in ALLOWED and _holds_varga(name)
    ]
    assert offenders == [], (
        "the product does not name the Varga Foundation (ADR 0186): "
        + ", ".join(offenders)
        + "; name GRAAL.SYSTEMS SAS, or add the file to ALLOWED with its reason"
    )


def test_every_exception_is_still_needed() -> None:
    tracked = set(_tracked())
    stale = [
        name
        for name in ALLOWED
        if name != "tests/licences/test_no_varga.py" and (name not in tracked or not _holds_varga(name))
    ]
    assert stale == [], "no longer names Varga, remove from ALLOWED: " + ", ".join(stale)


def test_the_guard_sees_a_file_that_names_the_foundation(tmp_path: pathlib.Path) -> None:
    """Red without the check: the pattern matches the spellings the product used."""
    for text in ("Varga Foundation", "VargaFoundation/choregos", "docs.varga.foundation", "@varga.dev"):
        assert VARGA.search(text), text


def test_nothing_pulls_from_the_deleted_registry_namespace() -> None:
    """Red when a manifest, workflow, Dockerfile, chart or policy points at `ghcr.io/vargafoundation` again:
    every package there was deleted on 2026-11-21 (only the ADRs and the plan, which are history, say so)."""
    offenders = []
    for name in _tracked():
        if name.startswith(HISTORY) or name in NAMES_THE_NAMESPACE:
            continue
        try:
            text = (ROOT / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        if FROZEN_NAMESPACE.search(text):
            offenders.append(name)
    assert offenders == [], "points at the deleted ghcr.io/vargafoundation namespace: " + ", ".join(offenders)


def test_the_guard_of_the_namespace_matches_its_spellings() -> None:
    for text in ("ghcr.io/vargafoundation/choregos-runner:0.17.15", "GHCR.IO/VargaFoundation/x"):
        assert FROZEN_NAMESPACE.search(text), text
    assert not FROZEN_NAMESPACE.search("registry.example.org/exampleorg/runner:1")


def test_the_gvisor_installer_image_is_supplied_by_the_operator() -> None:
    """No image of the installer is built here: the manifest names a placeholder the operator overrides
    (kustomize `images:`), and says so, rather than pointing at a registry nobody publishes to."""
    manifest = (ROOT / "infra/bootstrap/gvisor.yaml").read_text(encoding="utf-8")
    assert "image: gvisor-installer:operator-supplied" in manifest
    assert "kustomize `images:` override" in manifest
    assert "ghcr.io/" not in manifest.replace("registry.k8s.io", "")
