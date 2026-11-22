# SPDX-License-Identifier: Apache-2.0
"""An in-memory GitOps repository: what a netops change is proposed on (`netops/gitops.py`).

Branches, files written by commit, pull requests opened and never merged by the platform, as
`github.changes.GitHubChanges` does on GitHub: an identical file is not rewritten, an open pull request
on the branch is reused, so a replay opens nothing twice."""

from __future__ import annotations

from tranibor_core.domain import PrRef, PrState


class FakeGitOps:
    def __init__(self) -> None:
        self.branches: dict[str, dict[str, str]] = {}
        #: (repository, branch) -> {path: content} written by `commit_files`, and the commits in order.
        self.files: dict[tuple[str, str], dict[str, str]] = {}
        self.commits: list[tuple[str, str, str, list[str]]] = []
        self.prs: dict[tuple[str, int], PrState] = {}
        #: What the platform merged: always empty, a proposal is merged by the repository's own review.
        self.merge_queue: list[PrRef] = []
        self._next_number = 1

    async def ensure_branch(self, repo: str, name: str, base: str) -> None:
        self.branches.setdefault(repo, {}).setdefault(name, base)

    async def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        tree = self.files.setdefault((repo, branch), {})
        changes = {path: content for path, content in files.items() if tree.get(path) != content}
        if changes:
            tree.update(changes)
            self.commits.append((repo, branch, message, sorted(changes)))
        return f"commit-{len(self.commits)}"

    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> PrRef:
        for pr in self.prs.values():
            if pr.ref.repo == repo and pr.ref.head == head and not pr.merged:
                return pr.ref
        number = self._next_number
        self._next_number += 1
        url = f"https://fake.git/{repo}/pull/{number}"
        ref = PrRef(repo=repo, number=number, url=url, head=head, base=base)
        self.prs[(repo, number)] = PrState(ref=ref, title=title, body=body, draft=draft)
        return ref
