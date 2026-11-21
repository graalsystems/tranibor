# SPDX-License-Identifier: Apache-2.0
"""The source control's fake behaves like the real adapters (ADR 0005): the `software-delivery` plugin's
`FakeScm`, the `scm` slot of `AdapterSet.fakes()` once the extension is loaded (ADR 0181, round D). These were
`packages/adapters/tests/test_fakes.py`'s source control tests."""

from __future__ import annotations

import pytest
from tranibor_adapters import AdapterSet
from tranibor_adapters.errors import HeadMovedError
from tranibor_software_delivery.connectors.base import ScmAdapter


async def test_the_fake_set_s_source_control_honours_the_protocol() -> None:
    assert isinstance(AdapterSet.fakes().scm, ScmAdapter)


async def test_scm_pr_flow() -> None:
    scm = AdapterSet.fakes().scm
    await scm.ensure_branch("acme/billing", "choregos/123", "main")
    ref = await scm.open_pr("acme/billing", "choregos/123", "main", "fix: credit notes", "body", True)
    await scm.update_pr(ref, body="body v2", draft=False)
    scm.set_checks(ref, "success")  # type: ignore[attr-defined]
    scm.submit_review(ref, "marie", "approved")  # type: ignore[attr-defined]
    pr = await scm.get_pr(ref)
    assert pr.checks_conclusion() == "success"
    assert pr.review_conclusion() == "approved"
    assert not pr.draft
    await scm.enqueue_merge(ref, str(pr.head_sha))
    assert scm.merge_queue == [ref]  # type: ignore[attr-defined]
    token = await scm.mint_token("acme/billing", 3600, ["contents:write"])
    assert token.startswith("fake-token-")


async def test_scm_a_branch_has_a_head_that_every_write_moves() -> None:
    """ADR 0171: the fake has commits, as a forge does. A commit keeps the diff it was pushed with."""
    scm = AdapterSet.fakes().scm
    assert await scm.head_of("acme/billing", "choregos/123") is None, "no branch, no commit"
    scm.set_diff("acme/billing", "main", "choregos/123", [("src/a.py", 10, 2)])  # type: ignore[attr-defined]
    first = await scm.head_of("acme/billing", "choregos/123")
    assert first and await scm.head_of("acme/billing", "choregos/123") == first, "reading moves nothing"
    second = scm.push("acme/billing", "choregos/123", [("src/a.py", 10, 2), (".github/x.yml", 3, 0)])  # type: ignore[attr-defined]
    assert second != first and await scm.head_of("acme/billing", "choregos/123") == second
    assert (await scm.compare("acme/billing", "main", first)).paths() == ["src/a.py"], "the first commit"
    assert (await scm.compare("acme/billing", "main", second)).paths() == ["src/a.py", ".github/x.yml"]
    assert (await scm.compare("acme/billing", "main", "choregos/123")).paths() == [
        "src/a.py",
        ".github/x.yml",
    ], "by its name, a branch is whatever it holds now"
    ref = await scm.open_pr("acme/billing", "choregos/123", "main", "fix", "body", False)
    assert (await scm.get_pr(ref)).head_sha == second
    third = scm.push("acme/billing", "choregos/123")  # type: ignore[attr-defined]
    assert (await scm.get_pr(ref)).head_sha == third, "a pull request's head follows its branch"


async def test_scm_a_merge_names_its_commit_and_a_stale_one_is_refused() -> None:
    """ADR 0171: a merge that names a commit the head has left merges nothing; merged, it stays."""
    from tranibor_software_delivery.connectors.base import ScmAdapter, merge_at_commit_refusal

    scm = AdapterSet.fakes().scm
    assert isinstance(scm, ScmAdapter) and merge_at_commit_refusal(scm) is None
    scm.set_diff("acme/billing", "main", "choregos/123", [("src/a.py", 10, 2)])  # type: ignore[attr-defined]
    ref = await scm.open_pr("acme/billing", "choregos/123", "main", "fix", "body", False)
    judged = await scm.head_of("acme/billing", "choregos/123")
    assert judged
    moved = scm.push("acme/billing", "choregos/123")  # type: ignore[attr-defined]
    with pytest.raises(HeadMovedError) as refused:
        await scm.enqueue_merge(ref, judged)
    assert (refused.value.expected, refused.value.actual) == (judged, moved)
    assert scm.merge_queue == [], "nothing was merged"  # type: ignore[attr-defined]

    await scm.enqueue_merge(ref, moved)
    await scm.enqueue_merge(ref, moved)
    assert scm.merge_queue == [ref], "merging twice is not a second merge"  # type: ignore[attr-defined]
    scm.push("acme/billing", "choregos/123")  # type: ignore[attr-defined]
    assert (await scm.get_pr(ref)).head_sha == moved, (
        "a merged pull request keeps the commit it was merged at"
    )
    with pytest.raises(HeadMovedError):
        await scm.enqueue_merge(ref, judged)


async def test_scm_diff_and_scope() -> None:
    scm = AdapterSet.fakes().scm
    scm.set_diff("acme/billing", "main", "choregos/123", [("src/a.py", 10, 2), ("docs/x.md", 1, 0)])  # type: ignore[attr-defined]
    diff = await scm.compare("acme/billing", "main", "choregos/123")
    assert diff.paths() == ["src/a.py", "docs/x.md"]
    assert diff.additions == 11 and diff.deletions == 2


async def test_scm_lists_what_a_branch_holds() -> None:
    scm = AdapterSet.fakes().scm
    await scm.commit_files("acme/billing", "main", {"docs/adr/0001-q.md": "x", "src/a.py": "y"}, "m")
    assert await scm.list_files("acme/billing", "main", "docs/adr") == ["docs/adr/0001-q.md"]  # type: ignore[attr-defined]
    assert await scm.list_files("acme/billing", "other", "docs/adr") == []  # type: ignore[attr-defined]


async def test_scm_commit_files_and_pr_are_idempotent() -> None:
    """Like GitHub: a replay writes no commit, and a PR open on the branch is reused."""
    scm = AdapterSet.fakes().scm
    await scm.ensure_branch("acme/infra", "choregos/p-1", "main")
    await scm.commit_files("acme/infra", "choregos/p-1", {"platform/a.yaml": "v: 1\n"}, "fix")
    await scm.commit_files("acme/infra", "choregos/p-1", {"platform/a.yaml": "v: 1\n"}, "fix")
    assert len(scm.commits) == 1  # type: ignore[attr-defined]
    first = await scm.open_pr("acme/infra", "choregos/p-1", "main", "fix", "body", False)
    second = await scm.open_pr("acme/infra", "choregos/p-1", "main", "fix", "body", False)
    assert second == first
