# SPDX-License-Identifier: Apache-2.0
"""S87-04: the support matrix is a record, and the documentation reads it.

`docs/support/clusters.yaml` holds each kind of cluster and its last green proof; `tools/support_matrix.py`
computes the word (plan 2026-11-25, D4) and writes the section of `docs/deployment.md`. Refused here: a
section that differs from the record, a word written by hand, a page that keeps `supported` for a proof
older than 30 days, and a page that calls a provider supported without an entry that computes to it.
"""

from __future__ import annotations

import datetime
import importlib.util
import pathlib
import sys
from types import ModuleType

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TODAY = datetime.date.today()


def _tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("support_matrix", ROOT / "tools" / "support_matrix.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["support_matrix"] = module
    spec.loader.exec_module(module)
    return module


sm = _tool()

ENTRY = """
  - id: {id}
    provider: {provider}
    names: [{names}]
    distribution: null
    runtime: null
    network_policy: null
    executors: []
    proof: {proof}
"""


def _record(*entries: str, as_of: datetime.date = TODAY) -> str:
    return f"as_of: {as_of.isoformat()}\nclusters:" + "".join(entries)


def _proved(day: datetime.date | None) -> str:
    date = "null" if day is None else day.isoformat()
    return "{date: " + date + ", version: '1.0', what: a run, evidence: [docs/deployment.md]}"


def test_the_generated_section_is_the_records() -> None:
    page = sm.PAGE.read_text(encoding="utf-8")
    assert sm.section_of(page) == sm.render(sm.load()), "run `make docs-support`"


def test_the_seed_names_every_kind_the_plan_lists_and_claims_no_cloud() -> None:
    words = {entry.id: entry.word(TODAY) for entry in sm.load()}
    for cloud in ("eks", "gke", "aks", "openshift", "scaleway-kapsule", "ovhcloud-mks"):
        assert words[cloud] == "unknown"
    assert {"operator-dev", "operator-prod", "kind", "k3s-single-node", "aca"} <= set(words)
    assert words["aca"] == "tested"  # green once, its date not recorded


def test_every_evidence_path_exists() -> None:
    for entry in sm.load():
        for item in entry.evidence:
            assert item.startswith("https://") or (ROOT / item).exists(), f"{entry.id}: {item}"


@pytest.mark.parametrize(
    ("age", "word"), [(0, "supported"), (30, "supported"), (31, "tested"), (None, "tested")]
)
def test_the_word_is_computed_from_the_date(age: int | None, word: str) -> None:
    day = None if age is None else TODAY - datetime.timedelta(days=age)
    entries = sm.load(_record(ENTRY.format(id="x", provider="X", names="", proof=_proved(day))))
    assert entries[0].word(TODAY) == word
    never = sm.load(_record(ENTRY.format(id="y", provider="Y", names="", proof="null")))
    assert never[0].word(TODAY) == "unknown"


def test_a_page_that_keeps_supported_for_a_proof_older_than_30_days_is_refused() -> None:
    """The page was generated while the proof was fresh; 31 days on, the record says `tested`."""
    fresh = TODAY - datetime.timedelta(days=31)
    entries = sm.load(
        _record(ENTRY.format(id="gke", provider="Google GKE", names="GKE", proof=_proved(fresh)))
    )
    written_back_then = sm.render(entries, today=fresh)
    assert "**supported**" in written_back_then
    assert written_back_then != sm.render(entries), "an aged proof must change the section"
    assert "**tested**" in sm.render(entries)
    # And prose that still says so is refused.
    assert sm.unsupported_claims("GKE is supported.", entries)


def test_a_word_written_by_hand_is_refused() -> None:
    hand = ENTRY.format(id="x", provider="X", names="", proof=_proved(TODAY)) + "    word: supported\n"
    with pytest.raises(sm.RecordError, match="computed"):
        sm.load(_record(hand))


def test_a_proof_dated_in_the_future_is_refused() -> None:
    tomorrow = TODAY + datetime.timedelta(days=1)
    with pytest.raises(sm.RecordError, match="after the record's `as_of`"):
        sm.load(_record(ENTRY.format(id="x", provider="X", names="", proof=_proved(tomorrow))))


def test_the_word_counts_from_the_records_as_of_not_from_the_day_the_test_runs() -> None:
    """A record brought up to date 40 days ago still says what it said then: the docs test never turns
    red on a day nobody committed. Moving `as_of` forward is what ages a proof."""
    then = TODAY - datetime.timedelta(days=40)
    proof = _proved(then - datetime.timedelta(days=5))
    entry = ENTRY.format(id="gke", provider="Google GKE", names="GKE", proof=proof)
    assert "**supported**" in sm.render(
        sm.load(_record(entry, as_of=then)), today=sm.as_of(_record(entry, as_of=then))
    )
    assert "**tested**" in sm.render(sm.load(_record(entry)), today=TODAY)


def test_a_record_without_as_of_or_with_one_in_the_future_is_refused() -> None:
    with pytest.raises(sm.RecordError, match="as_of"):
        sm.as_of("clusters: []")
    with pytest.raises(sm.RecordError, match="after today"):
        sm.as_of(_record(as_of=TODAY + datetime.timedelta(days=1)))


CLOUDS = _record(
    ENTRY.format(id="eks", provider="Amazon EKS", names="EKS, Amazon EKS", proof="null"),
    ENTRY.format(id="aks", provider="Azure AKS", names="AKS", proof="null"),
    ENTRY.format(id="k3s", provider="k3s", names="k3s", proof=_proved(TODAY)),
)


@pytest.mark.parametrize(
    "text",
    [
        "EKS is supported.",
        "The platform is supported on Amazon EKS and on k3s.",
        "Tranibor supports AKS out of the box.",
        "Supported clusters: k3s, EKS, kind.",
    ],
)
def test_a_provider_called_supported_without_its_entry_is_refused(text: str) -> None:
    assert sm.unsupported_claims(text, sm.load(CLOUDS))


@pytest.mark.parametrize(
    "text",
    [
        "EKS is not supported yet.",
        "AKS isn't supported.",
        "An unsupported setting on EKS is refused at render.",
        "k3s is supported.",  # its entry computes to supported
        "A provider is supported when its proof ran green in the last 30 days.",
        "EKS: the VPC CNI's network policy agent. The gate is supported everywhere.",
        "The AKSX operator is supported.",  # not the provider's name, whole words only
    ],
)
def test_what_is_no_claim_is_not_refused(text: str) -> None:
    assert sm.unsupported_claims(text, sm.load(CLOUDS)) == []


def test_no_page_of_the_documentation_claims_a_provider_the_record_does_not() -> None:
    entries = sm.load()
    # The plan and the ADRs are dated decisions about the matrix, not claims to an operator.
    pages = [
        page
        for page in (ROOT / "docs").rglob("*.md")
        if not {"plan", "adr"} & set(page.relative_to(ROOT / "docs").parts[:1])
    ]
    assert len(pages) > 20
    claims = [
        f"{page.relative_to(ROOT)}: {claim}"
        for page in pages
        for claim in sm.unsupported_claims(page.read_text(encoding="utf-8"), entries)
    ]
    assert claims == []
