# SPDX-License-Identifier: Apache-2.0
"""One agent run through the runner's own command, in the runner's step of a Tekton agent stage (S87-06).

Run by `tests/cluster/test_tekton_agent_run.py` as the step's program, with the step's own arguments
(`run --write-result-url $(results.result-url.path) --write-status $(results.status.path)`) and the step's
own environment (the run's token from its Secret, `TRANIBOR_RUNNER_AGENT_ISOLATION=container`, the
launcher's port, `TMPDIR=/exchange`, the Task's workspace). It calls the runner's CLI (`tranibor-runner
run`) unchanged, with one test double: the platform's internal API, in memory, in place of
`InternalClient` (as `tests/live/agent_shape/runner_side.py` does). The agent is the runner tests' scripted
ACP agent (`packages/runner/tests/fixtures/fake_agent.py`, handed in `S87_FAKE_AGENT`): the runner asks the
launcher of the agent's sidecar to start it, as it would start a harness.

Every event the runner journals is printed on its own line, `S87-06 event {json}`, as the API receives it;
the result it posts, `S87-06 result {json}`; and, once the CLI ended, who owns the file the agent wrote,
`S87-06 written {json}`.
Standard library, the runner's packages and `tranibor_contracts` only: it runs with the runner image alone.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Any

from tranibor_contracts import (
    AgentRef,
    Budget,
    Callbacks,
    ContextPack,
    LaunchSpec,
    ModelRef,
    Permissions,
    PlaybookRef,
    ProjectRef,
    StageInput,
    StageResult,
    TransitionRef,
    WorkItemRef,
)
from tranibor_runner import runner as runner_module
from tranibor_runner.cli import app
from tranibor_tools_mcp.client import Relayed

#: Where the agent's side can read the scripted agent: the exchange directory both containers mount.
EXCHANGE = pathlib.Path(os.environ.get("TMPDIR", "/exchange"))
#: What the agent writes in the Task's workspace, which both containers mount.
WRITTEN = "notes/s87-06.md"


def stage_input() -> StageInput:
    """A step without a repository (ADR 0012): the runner works in an empty versioned directory."""
    agent = EXCHANGE / "s87-fake-agent.py"
    agent.write_text(os.environ["S87_FAKE_AGENT"], encoding="utf-8")
    agent.chmod(0o644)
    script = {
        "turns": [
            {
                "messages": ["S87-06: the agent ran"],
                "writes": [{"path": WRITTEN, "content": "written by the agent in its sidecar\n"}],
                "result": {
                    "schema": "tranibor/StageResult/v1",
                    "status": "done",
                    "summary": "S87-06 agent run",
                    "evidence": {"tests_passed": True, "tests_run": 1},
                },
            }
        ]
    }
    run_id = os.environ["TRANIBOR_RUN_ID"]
    workspace = os.environ["TRANIBOR_WORKSPACE"]
    return StageInput(
        run_id=run_id,
        attempt=1,
        project=ProjectRef(slug="demo", org="s87"),
        work_item=WorkItemRef(key="s87/demo#6", title="S87-06", body="One agent run on Tekton.", size="S"),
        transition=TransitionRef(id="t-implement", role="implement", **{"from": "ready"}, to="in_progress"),
        agent=AgentRef(
            backend="codex",
            launch=LaunchSpec(
                command=[sys.executable, str(agent)],
                env={"FAKE_AGENT_SCRIPT": json.dumps(script), "TRANIBOR_WORKSPACE_PATH": workspace},
            ),
        ),
        model=ModelRef(
            litellm_model="platform/standard", base_url="http://litellm:4000", api_format="openai"
        ),
        budget=Budget(usd=1, max_turns=4, max_minutes=5),
        allowed_paths=["notes/**"],
        playbook=PlaybookRef(ref="implement@s87", prompt="Write the note."),
        permissions=Permissions(write_paths=["notes/**"]),
        callbacks=Callbacks(api_url=os.environ.get("TRANIBOR_API_URL", ""), run_token="[REDACTED]"),
    )


class InMemoryApi:
    """The platform's internal API for one run: hands the step, prints what the runner publishes."""

    def __init__(self, *_: Any, **__: Any) -> None:
        self.stage_input = stage_input()

    @staticmethod
    def _print(kind: str, payload: Any) -> None:
        print(f"S87-06 {kind} " + json.dumps(payload, default=str), flush=True)

    async def fetch_egress_token(self) -> str:
        return "s87-egress"

    async def fetch_input(self) -> StageInput:
        return self.stage_input

    async def post_events(self, events: list[dict[str, Any]]) -> int:
        for event in events:
            self._print("event", event)
        return len(events)

    async def post_result(self, result: StageResult) -> dict[str, Any]:
        self._print("result", result.model_dump(mode="json", by_alias=True, exclude_none=True))
        return {"status": "recorded"}

    async def fetch_context(self) -> ContextPack:
        return ContextPack.empty(self.stage_input.work_item.title)

    async def fetch_ticket(self) -> dict[str, Any]:
        return {"key": self.stage_input.work_item.key, "title": self.stage_input.work_item.title, "body": ""}

    async def fetch_git_token(self) -> str | None:
        return None

    async def fetch_skills(self) -> list[dict[str, Any]]:
        return []

    async def fetch_knowledge(self) -> list[dict[str, Any]]:
        return []

    async def fetch_inputs(self) -> list[dict[str, Any]]:
        return []

    async def fetch_ci_logs(self, tail: int = 500) -> str:
        return ""

    async def relay_tools(self) -> Relayed:
        return Relayed(200, b'{"tools": []}', "application/json")

    async def aclose(self) -> None:
        return None


def owner() -> None:
    """Who wrote the agent's file: the sidecar's uid (1001), not the runner's step (1000)."""
    written = pathlib.Path(os.environ["TRANIBOR_WORKSPACE"]) / WRITTEN
    stat = written.stat() if written.exists() else None
    InMemoryApi._print(
        "written", {"path": WRITTEN, "uid": stat.st_uid if stat else None, "runner_uid": os.getuid()}
    )


runner_module.InternalClient = InMemoryApi  # type: ignore[attr-defined,assignment]
try:
    app(args=sys.argv[1:], prog_name="tranibor-runner")
finally:
    owner()
