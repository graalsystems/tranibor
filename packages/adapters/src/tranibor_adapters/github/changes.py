# SPDX-License-Identifier: Apache-2.0
"""Files proposed on a branch of a GitHub repository, as a pull request, through the deployment's App.

Git as a transport (ADR 0181, class (a)): a netops change (a Cilium policy, an nginx configuration) is
never applied by the platform, it is written on a branch of the GitOps repository and offered as a pull
request (`netops/gitops.py`, ADR 0052). That is all the core asks of a forge. The source control of
software delivery (`scm`: checks, reviews, the merge queue, a diff) is the `software-delivery` plugin's,
whose GitHub connector builds on this class.

Each write is idempotent: an existing branch is kept, an identical file is not rewritten, an open pull
request on the branch is reused, so a replayed action opens nothing twice (ADR 0008).
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote

from tranibor_core.domain import PrRef

from ..errors import UpstreamError
from .client import GitHubClient


class GitHubChanges:
    """Branch, commit and pull request on a GitHub repository; never a merge."""

    def __init__(self, client: GitHubClient, *, default_repo: str = "") -> None:
        self.client = client
        self.default_repo = default_repo

    async def ensure_branch(self, repo: str, name: str, base: str) -> None:
        try:
            await self.client.request("GET", f"/repos/{repo}/git/ref/heads/{name}", repo=repo)
            return
        except UpstreamError as exc:
            if exc.status_code != 404:
                raise
        base_ref = await self.client.request("GET", f"/repos/{repo}/git/ref/heads/{base}", repo=repo)
        await self.client.request(
            "POST",
            f"/repos/{repo}/git/refs",
            repo=repo,
            json={"ref": f"refs/heads/{name}", "sha": base_ref["object"]["sha"]},
        )

    async def commit_files(self, repo: str, branch: str, files: dict[str, str], message: str) -> str:
        """Writes files on a branch through the Contents API, one commit per modified file.

        For the PLATFORM to open a PR with changes (effect `gitops.pull_request`): the token stays
        here, the agent never holds it. Idempotent: a file already identical on the branch is not
        rewritten, so a replay creates no commit. Returns the SHA of the last commit written (empty
        if nothing changed).
        """
        last = ""
        for path, content in sorted(files.items()):
            url = f"/repos/{repo}/contents/{quote(path)}"
            current_sha = None
            try:
                current = await self.client.request("GET", url, repo=repo, params={"ref": branch})
            except UpstreamError as exc:
                if exc.status_code != 404:
                    raise
            else:
                current_sha = current.get("sha")
                if current.get("encoding") == "base64" and base64.b64decode(current.get("content", "")) == (
                    content.encode()
                ):
                    continue
            payload: dict[str, Any] = {
                "message": message,
                "content": base64.b64encode(content.encode()).decode(),
                "branch": branch,
            }
            if current_sha:
                payload["sha"] = current_sha
            written = await self.client.request("PUT", url, repo=repo, json=payload)
            last = str((written.get("commit") or {}).get("sha") or last)
        return last

    async def open_pr(self, repo: str, head: str, base: str, title: str, body: str, draft: bool) -> PrRef:
        owner = repo.split("/", 1)[0]
        existing = await self.client.request(
            "GET", f"/repos/{repo}/pulls", repo=repo, params={"head": f"{owner}:{head}", "state": "open"}
        )
        if existing:
            pr = existing[0]
        else:
            pr = await self.client.request(
                "POST",
                f"/repos/{repo}/pulls",
                repo=repo,
                json={"title": title, "head": head, "base": base, "body": body, "draft": draft},
            )
        return PrRef(repo=repo, number=int(pr["number"]), url=pr.get("html_url"), head=head, base=base)
