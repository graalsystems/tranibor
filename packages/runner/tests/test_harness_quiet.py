"""The harnesses do not call home from a run (#1987).

Measured on the dev gateway on 2026-11-19, each harness in its own container behind the runner's
CONNECT listener: claude-agent-acp 0.85.1 opened 3 tunnels to `api.anthropic.com:443` a run although
`DISABLE_TELEMETRY` was set, gemini-cli 0.60.0 13 to 36 to `play.googleapis.com:443` (its usage
statistics). Measured again in the runner image 0.27.15 over ACP, one prompt against a fake model:
Claude Code opens none with `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, gemini-cli none with
`privacy.usageStatisticsEnabled: false`, and opencode none already (S40-25).
"""

from __future__ import annotations

import json
from pathlib import Path

from tranibor_contracts import ModelRef
from tranibor_runner.backends import get_backend
from tranibor_runner.backends.base import LaunchPlan

from .test_units import _minimal_stage_input


def test_claude_code_is_told_to_send_nothing_but_its_model_calls(tmp_path: Path) -> None:
    stage_input = _minimal_stage_input(tmp_path)
    stage_input.agent.launch.env = {"DISABLE_TELEMETRY": "0", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "0"}
    backend = get_backend("claude-code")
    model = ModelRef(
        litellm_model="anthropic/claude-haiku-4-5", base_url="http://litellm:4000", api_format="anthropic"
    )
    plan = backend.launch_plan(stage_input, tmp_path)
    env = {**plan.env, **backend.model_env(model, None)}  # the order the runner merges them in
    for switch in (
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "DISABLE_TELEMETRY",
        "DISABLE_ERROR_REPORTING",
        "DISABLE_AUTOUPDATER",
    ):
        assert env[switch] == "1", f"{switch}: an agent's launch env does not turn it back on"


def _gemini_plan(tmp_path: Path) -> LaunchPlan:
    stage_input = _minimal_stage_input(tmp_path)
    stage_input.model.provider_model = "gemini/gemini-2.5-pro"  # gemini-cli runs on Gemini only (#1990)
    return get_backend("gemini-cli").launch_plan(stage_input, tmp_path)


def _quiet(settings: dict[str, object]) -> None:
    assert settings["privacy"] == {"usageStatisticsEnabled": False}
    assert settings["general"] == {"enableAutoUpdate": False, "enableAutoUpdateNotification": False}


def test_gemini_cli_sends_no_usage_statistics_and_checks_no_update(tmp_path: Path) -> None:
    plan = _gemini_plan(tmp_path)
    workspace = json.loads(plan.files[".gemini/settings.json"])
    _quiet(workspace)
    assert workspace["selectedAuthType"] == "api-key", "the rest of the settings is unchanged"


def test_gemini_cli_keeps_them_off_under_a_repositorys_own_settings(tmp_path: Path) -> None:
    """`materialize` never overwrites a repository's `.gemini/settings.json`: the user's settings,
    under the run's `$HOME`, carry the switches too, and gemini-cli merges them beneath."""
    workspace, home = tmp_path / "ws", tmp_path / "home"
    (workspace / ".gemini").mkdir(parents=True)
    (workspace / ".gemini" / "settings.json").write_text('{"theme": "the project\'s"}')
    plan = _gemini_plan(workspace)
    plan.materialize(workspace)
    plan.materialize_home(home)

    assert json.loads((workspace / ".gemini" / "settings.json").read_text()) == {"theme": "the project's"}
    _quiet(json.loads((home / ".gemini" / "settings.json").read_text()))
