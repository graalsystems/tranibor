"""The former name leaves, and does not come back (ADR 0226, Consequences; round R8).

Two guards:

- **(a) the repository's ratchet.** Every text file outside the history holds at most as many
  occurrences of `choregos` (case-insensitive) as its ceiling in `tools/former_name_baseline.tsv`; a
  file the baseline does not list holds none. A ceiling left above its count fails too, so that what a
  round removed cannot be written again under the old ceiling. `tools/former_name.py --write` writes
  the baseline; `make lint` runs the same check.
- **(b) the public documentation.** `docs/site/` is vendored by the website at each release, and a
  reader types what it shows. Each occurrence left there must be one of `KEPT`: a name the product
  still holds only under its former form, or stored state, each with its reason. A page can never
  again write `choregos login` or `choregos-api.runDoor.hostnames`.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import pathlib
import re
import sys
from types import ModuleType
from typing import NamedTuple

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
SITE = ROOT / "docs" / "site"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("former_name", ROOT / "tools" / "former_name.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fn = _load()


# ───────────────────────────── (a) the repository's ratchet ─────────────────────────────


def test_every_file_is_at_its_ceiling() -> None:
    found = fn.problems(fn.count(), fn.read_baseline())
    assert not found, (
        "the former name moved (ADR 0226): write the new name, or lower the ceiling with "
        "`uv run python tools/former_name.py --write`:\n  " + "\n  ".join(found[:40])
    )


def test_the_ratchet_refuses_a_new_occurrence_and_a_stale_ceiling() -> None:
    """The guard of the test above, on synthetic counts."""
    ceilings = {"docs/a.md": 2, "docs/b.md": 1, "docs/gone.md": 3}
    assert fn.problems({"docs/a.md": 2, "docs/b.md": 1, "docs/gone.md": 3}, ceilings) == []
    above = fn.problems({"docs/a.md": 3, "docs/b.md": 1, "docs/gone.md": 3}, ceilings)
    assert above == ["docs/a.md: 3 occurrences of the former name, above its ceiling of 2"]
    new = fn.problems({**ceilings, "docs/new.md": 1}, ceilings)
    assert new == ["docs/new.md: 1 occurrence(s) of the former name in a file the baseline does not list"]
    stale = fn.problems({"docs/a.md": 1, "docs/b.md": 1}, ceilings)
    assert [line.split(":")[0] for line in stale] == ["docs/a.md", "docs/gone.md"]
    assert all("stale ceiling" in line for line in stale)


def test_the_count_is_case_insensitive_and_skips_the_history(tmp_path: pathlib.Path) -> None:
    files = {
        "docs/page.md": "Choregos, choregos-api and X-CHOREGOS-Secret\n",
        "docs/adr/0001-a.md": "choregos\n",
        "docs/plan/STATUS.md": "choregos\n",
        "trials/x/RESULTS.md": "choregos\n",
        "apps/api/src/tranibor_api/migrations/versions/0001_a.py": "choregos\n",
        "plugins/p/CHANGELOG.md": "choregos\n",
        "docs/clean.md": "tranibor\n",
        "image.png": "choregos\0",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_bytes(text.encode("utf-8"))
    assert fn.count(tmp_path, files) == {"docs/page.md": 3}


def test_the_baseline_round_trips() -> None:
    counts = {"b/x.md": 2, "a/y.py": 1}
    assert fn.parse(fn.render(counts)) == counts
    assert fn.render(counts).splitlines()[-2:] == ["1\ta/y.py", "2\tb/x.md"]


# ───────────────────────────── (b) the public documentation ─────────────────────────────


class Kept(NamedTuple):
    """A former name a page of `docs/site/` may show, and why."""

    pattern: re.Pattern[str]
    reason: str
    #: The pages it may appear on (a glob under `docs/site/`); every page when empty.
    pages: str = ""


def _kept(pattern: str, reason: str, pages: str = "") -> Kept:
    return Kept(re.compile(pattern), reason, pages)


#: The only former names `docs/site/` may still show, each with its reason (verified in the code).
KEPT = (
    _kept(
        r"\.choregos/(?:inputs|outputs|result\.json|task\.md|fields\.json|job\.json|knowledge)\b",
        "the run's workspace directory inside the step's sandbox, read by agents and job images (R5 notes)",
    ),
    _kept(
        r'"\.choregos/\*\*"',
        "the policy denies agents the former repository directory beside `.tranibor/**` (the templates do)",
    ),
    _kept(
        r'"choregos": \{|metadata\.choregos\.',
        "the A2A task's metadata key, the only one the door writes (apps/api/src/tranibor_api/a2a/door.py)",
    ),
    _kept(
        r"\bchoregos-(?:api-secrets|db|gateway-secrets|oidc|object-store|backup-target)\b",
        "the chart's default Secret names: stored state (ADR 0226 §6, R6 notes)",
    ),
    _kept(
        r"`choregos/api`", "the ExternalSecret's remote key: a path in the operator's secret store (R6 notes)"
    ),
    _kept(
        r"\bchoregos-pg-rw\.choregos-data\.svc\b|\.choregos-(?:temporal|gateway)\.svc\b",
        "the default namespaces of external dependencies in the chart's values (R6 notes)",
    ),
    _kept(r"^\s+(?:name|user): choregos$", "the database name and role: stored state (ADR 0226 §6)"),
    _kept(
        r"\bclientId: choregos$|\bchoregos-mcp\b|\bchoregos-claude-(?:code|ai)\b",
        "the OIDC client, the MCP audience and the Keycloak clients the chart and the API default to",
    ),
    _kept(
        r"\bchoregos@choregos\b",
        "the Claude Code plugin and its marketplace (.claude-plugin/marketplace.json)",
    ),
    _kept(
        r'"choregos\.assistant"', "the embedded assistant's postMessage source (apps/web/src/lib/embed.ts)"
    ),
    _kept(
        r'"choregos:inbox"', "a tracker state label of the shipped templates (R6 notes: state labels stay)"
    ),
    _kept(r"`choregos\.(?:plugins|migrations)`", "the Python entry-point groups extensions register under"),
    _kept(r"`choregos\.release\.\*`", "event types, which history and subscribers key on (ADR 0226 §6)"),
    _kept(r"`choregos-quant`", "the investment-research plugin's job command, under that name only"),
    _kept(r"`admin@choregos\.local`", "the demo administrator compose.yaml signs in"),
    _kept(
        r"`/choregos approve`", "the tracker comment command, parsed under that name only (github_events.py)"
    ),
    _kept(
        r"named `choregos` before|`choregos` still runs it"
        r"|The former file, `~/\.config/choregos/config\.json`",
        "the generated CLI reference names the former command and profile once, for the window (R5)",
        "cli/reference.md",
    ),
    _kept(
        r"`choregos-outpost enrol`",
        "the CLI's own help, which prints the enrolment command under the former name (cli/outposts.py)",
        "cli/reference.md",
    ),
)


def _pages() -> list[pathlib.Path]:
    return sorted(p for p in SITE.rglob("*") if p.is_file() and p.suffix in {".md", ".mdx", ".json"})


def unexplained(page: str, text: str) -> list[str]:
    """Each occurrence of the former name on the page that no entry of `KEPT` covers."""
    kept = [k.pattern for k in KEPT if not k.pages or fnmatch.fnmatch(page, k.pages)]
    found: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        covered = {i for pattern in kept for m in pattern.finditer(line) for i in range(m.start(), m.end())}
        found += [
            f"{page}:{number}: {line.strip()[:120]}"
            for m in fn.FORMER.finditer(line)
            if m.start() not in covered
        ]
    return found


def test_the_site_shows_the_former_name_only_where_the_product_keeps_it() -> None:
    offenders = [
        line for p in _pages() for line in unexplained(p.relative_to(SITE).as_posix(), p.read_text("utf-8"))
    ]
    assert not offenders, (
        "docs/site writes a former name the product has a twin for (ADR 0226 R8): write the new one, "
        "or add it to KEPT with its reason:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("kept", KEPT, ids=lambda k: k.pattern.pattern[:40])
def test_every_kept_name_is_still_shown(kept: Kept) -> None:
    """An entry nothing matches any more is a door left open: remove it."""
    pages = [
        p for p in _pages() if not kept.pages or fnmatch.fnmatch(p.relative_to(SITE).as_posix(), kept.pages)
    ]
    assert any(kept.pattern.search(line) for p in pages for line in p.read_text("utf-8").splitlines()), (
        f"no page of docs/site shows {kept.pattern.pattern!r} any more: remove it from KEPT"
    )


@pytest.mark.parametrize(
    "line",
    [
        "choregos login --api-url https://tranibor.example.com --org acme",
        "choregos-api:",
        "  `choregos-api.runDoor.hostnames`",
        "apiVersion: choregos/v1",
        "`X-Choregos-Signature`",
        "| `choregos:<org>:viewers` | `viewer` |",
        "curl -fsSLO https://tranibor.example.com/downloads/cli/choregos-linux-x86_64",
        "kubectl -n tranibor exec deploy/tranibor-api -- choregos-admin tokens create",
        "`.choregos/workflow.yaml`",
        "`choregos-outpost enrol`",  # kept on the generated reference only
    ],
)
def test_the_site_check_refuses_a_name_that_has_its_twin(line: str) -> None:
    assert unexplained("use/a-page.md", line), f"the check let `{line}` through"


def test_the_site_check_lets_a_kept_name_through() -> None:
    assert unexplained("host/secrets.md", "| `choregos-api-secrets` (`global.apiSecretRef`) |") == []
    assert unexplained("cli/reference.md", "the `choregos-outpost enrol` command to run there.") == []
