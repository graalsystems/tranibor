"""`next dev` must not dirty the tracked `apps/web/AGENTS.md`.

Next 16.4 writes a managed block (`<!-- BEGIN:nextjs-agent-rules -->`) into `AGENTS.md` when it
detects a coding agent, and rewrites it whenever its text changes between versions. That made a
development run modify a tracked file of the public repository. `agentRules: false` turns the write
off; with it off Next only removes a block it finds. So the file must hold none, and must not be
only a block (Next would then delete the file, and `apps/web/CLAUDE.md` imports it).
"""

from __future__ import annotations

import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parents[2] / "apps" / "web"


def test_next_config_turns_the_agent_rules_write_off() -> None:
    config = (WEB / "next.config.mjs").read_text(encoding="utf-8")
    assert re.search(r"^\s*agentRules:\s*false,", config, re.MULTILINE), (
        "next.config.mjs must set `agentRules: false`, or `next dev` rewrites AGENTS.md"
    )


def test_agents_md_holds_no_managed_block_and_says_something() -> None:
    text = (WEB / "AGENTS.md").read_text(encoding="utf-8")
    assert "nextjs-agent-rules" not in text, "a Next-managed block would be removed or rewritten"
    assert "nextjs-agent-feedback" not in text
    assert len(text.strip()) > 200
