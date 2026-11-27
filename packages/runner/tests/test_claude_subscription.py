"""S87-22: Claude Code on its person's own subscription, as the runner starts it (ADR 0227 §4, §5, §8).

The daemon of a personal outpost starts the runner of a subscription run with the person's token in
`CLAUDE_CODE_OAUTH_TOKEN` (`tranibor_outpost.secrets.child_environment`). The runner hands it to the vendor's
CLI and to nothing else: never to a gateway run's agent, never to the project's checks or to its own git, and
never beside a gateway variable, which would outrank it. The CLI runs on the provider's own model id, not on a
gateway alias the vendor does not know, under a configuration directory the runner owns inside the run's home,
so that its person's `~/.claude` (MCP servers, hooks, plugins, `CLAUDE.md`) never joins a governed run.

Signed out, the CLI says so in the ways the S87-07 fixture read from the pinned packages' source
(`fixtures/subscription/from_source.json`): ACP's `auth_required` (-32000, "Authentication required"), or a
result that holds "Please run /login". The fake ACP agent replays each, before the first prompt and in a turn,
and the run ends `subscription_login_required`, after one prompt at most: a reason of its own, which S87-23
sends to the person and never retries. A gateway run that meets the same error keeps its own reasons.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from tranibor_contracts import AgentRef, LaunchSpec, ModelRef, StageInput, StageStatus
from tranibor_contracts.enums import RunCredential, SubscriptionProvider
from tranibor_runner.backends import get_backend
from tranibor_runner.backends.claude_code import CLAUDE_CODE_QUIET_ENV
from tranibor_runner.runner import Runner
from tranibor_runner.workspace import agent_home

from .conftest import FakeInternalClient, agent_script, valid_result

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "subscription" / "from_source.json").read_text(encoding="utf-8")
)
TOKEN_VARIABLE = FIXTURE["credential"]["token_variable"]
CONFIG_DIR_VARIABLE = FIXTURE["credential"]["config_dir_variable"]
SIGNED_OUT = FIXTURE["signed_out"]
#: The person's token as the daemon put it in the runner's environment. No shape on purpose (see S87-21).
TOKEN = "-".join(["canary", "subscription", "7c3a" * 8])
#: The run's model as the gateway's catalogue resolved it: the alias the run names, the model behind it.
ALIAS = "platform/standard"
PROVIDER_MODEL = "anthropic/claude-sonnet-4-5-20250929"
#: What the provider calls it, and what the vendor's CLI must be pinned to on a subscription.
PROVIDER_ID = "claude-sonnet-4-5-20250929"
#: Every variable that would choose another credential, or another endpoint, than the subscription.
GATEWAY_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GEMINI_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
)
#: Every alias Claude Code may pick a model by: each must name the provider's id.
PINS = (
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
)
#: What the person runs to sign in again, which a signed-out run tells them.
SIGN_IN = "choregos-outpost personal login claude"


def _claude_code(stage_input: StageInput, prompts: Path) -> StageInput:
    """The step on Claude Code (the fake ACP agent speaks for it), on a gateway alias of a Claude model."""
    stage = stage_input.model_copy(deep=True)
    stage.agent = AgentRef(
        backend="claude-code", launch=LaunchSpec(command=list(stage_input.agent.launch.command))
    )
    stage.model = ModelRef(
        litellm_model=ALIAS,
        base_url="http://litellm:4000",
        api_format="anthropic",
        provider_model=PROVIDER_MODEL,
    )
    stage.agent.launch.env["FAKE_AGENT_PROMPTS"] = str(prompts)
    return stage


def _subscription(settings: Any) -> Any:
    return settings.model_copy(
        update={
            "run_credential": RunCredential.SUBSCRIPTION,
            "run_provider": SubscriptionProvider.ANTHROPIC,
            "run_place": "outpost:op-1",
        }
    )


def _spy_agent_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The environment each agent is started with, as the ACP client holds it."""
    import tranibor_runner.runner as module

    seen: list[dict[str, str]] = []
    real = module.AcpClient

    class _Spy(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen.append(dict(self.env))

    monkeypatch.setattr(module, "AcpClient", _Spy)
    return seen


def _prompts(path: Path) -> list[str]:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


async def _execute(
    stage: StageInput, settings: Any, script: dict[str, Any]
) -> tuple[Any, FakeInternalClient]:
    agent_script(script, stage, settings.workspace)
    client = FakeInternalClient(stage)
    runner = Runner(settings, client)  # type: ignore[arg-type]
    # A gateway run's turn that ends in an error asks the gateway once (S65-35): it serves the key here.
    runner.model_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    outcome = await runner.execute(stage, client)  # type: ignore[arg-type]
    return outcome, client


# ───────────────────────── the token reaches the vendor's CLI, and only it ─────────────────────────


@pytest.mark.asyncio
async def test_the_agent_env_has_the_token_and_no_gateway(
    stage_input: StageInput, runner_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's environment holds the token, every gateway variable and its person's own configuration
    directory; the step carries a gateway key. The agent of a subscription run gets the token, none of the
    gateway's variables, the provider's model id on every alias Claude Code may pick (and its settings file),
    the switches that keep its own traffic off, and a configuration directory inside the run's home."""
    for name in GATEWAY_VARIABLES:
        monkeypatch.setenv(name, f"runner-own-{name.lower()}")
    monkeypatch.setenv(TOKEN_VARIABLE, TOKEN)
    persons_own = str(Path.home() / ".claude")
    monkeypatch.setenv(CONFIG_DIR_VARIABLE, persons_own)
    stage = _claude_code(stage_input, tmp_path / "prompts.json")
    seen = _spy_agent_env(monkeypatch)

    outcome, client = await _execute(
        stage, _subscription(runner_settings), {"turns": [{"messages": ["done"], "result": valid_result()}]}
    )

    assert outcome.exit_code.name == "OK", outcome.detail
    [env] = seen
    assert env[TOKEN_VARIABLE] == TOKEN, "the vendor's CLI runs on its person's subscription"
    assert sorted(set(GATEWAY_VARIABLES) & set(env)) == []
    assert "runner-own-" not in json.dumps(env) and stage.gateway_key not in json.dumps(env)
    assert {name: env.get(name) for name in PINS} == dict.fromkeys(PINS, PROVIDER_ID), (
        "the provider's id, never the gateway's alias"
    )
    settings_file = runner_settings.workspace / ".claude" / "settings.json"
    assert json.loads(settings_file.read_text(encoding="utf-8"))["model"] == PROVIDER_ID
    assert {name: env.get(name) for name in CLAUDE_CODE_QUIET_ENV} == CLAUDE_CODE_QUIET_ENV
    home = agent_home(stage)
    config_dir = Path(env[CONFIG_DIR_VARIABLE])
    assert env["HOME"] == str(home) and config_dir.parent == home, "the runner's, inside the run's home"
    assert str(config_dir) != persons_own and not str(config_dir).startswith(str(Path.home()) + "/.claude")
    assert TOKEN not in json.dumps(client.events), "the scrub of S87-21 still holds"


@pytest.mark.asyncio
async def test_a_gateway_run_has_no_token_even_when_the_runners_env_has_one(
    stage_input: StageInput, runner_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A gateway run's agent never gets a subscription token: not from the runner's own environment, not from
    the step's launch environment. Nor does what the runner starts from its credential-free environment (the
    project's checks, the runner's git, a tool script). The gateway run keeps its model and its key."""
    from tranibor_runner.config import agent_base_environment

    monkeypatch.setenv(TOKEN_VARIABLE, TOKEN)
    monkeypatch.delenv(CONFIG_DIR_VARIABLE, raising=False)
    stage = _claude_code(stage_input, tmp_path / "prompts.json")
    stage.agent.launch.env[TOKEN_VARIABLE] = "from-the-step-" + "9" * 20
    seen = _spy_agent_env(monkeypatch)

    outcome, _ = await _execute(stage, runner_settings, {"turns": [{"result": valid_result()}]})

    assert outcome.exit_code.name == "OK", outcome.detail
    [env] = seen
    assert TOKEN_VARIABLE not in env and TOKEN not in json.dumps(env)
    assert TOKEN_VARIABLE not in agent_base_environment(), "the checks and git start from there"
    assert env["ANTHROPIC_API_KEY"] == stage.gateway_key and env["ANTHROPIC_MODEL"] == ALIAS
    assert CONFIG_DIR_VARIABLE not in env, "a gateway run is as it was"


def test_the_backend_pins_the_providers_model_id(tmp_path: Path) -> None:
    """The model of a subscription run is the provider's id: the model the gateway catalogue resolved the
    alias to (S87-18's map fills it for a subscription), without LiteLLM's route; else the run's name. Its
    environment names no gateway endpoint nor key, and keeps the CLI's own traffic off."""
    backend = get_backend("claude-code")

    def ref(name: str, provider_model: str | None = None) -> ModelRef:
        return ModelRef(
            litellm_model=name,
            base_url="http://litellm:4000",
            api_format="anthropic",
            provider_model=provider_model,
        )

    pinned = backend.subscription_model(ref(ALIAS, PROVIDER_MODEL))
    assert (pinned.litellm_model, pinned.provider_model) == (PROVIDER_ID, PROVIDER_ID)
    assert backend.subscription_model(ref("claude-opus-4-1")).litellm_model == "claude-opus-4-1"
    assert backend.subscription_model(ref("anthropic/claude-haiku-4-5")).litellm_model == "claude-haiku-4-5"
    env = backend.model_env(pinned, None)
    assert {name: env[name] for name in PINS} == dict.fromkeys(PINS, PROVIDER_ID)
    assert not {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"} & set(env)
    assert {name: env[name] for name in CLAUDE_CODE_QUIET_ENV} == CLAUDE_CODE_QUIET_ENV
    assert backend.subscription_env(tmp_path) == {CONFIG_DIR_VARIABLE: str(tmp_path / ".claude-runner")}
    assert SubscriptionProvider.ANTHROPIC in backend.subscriptions
    assert not get_backend("codex").subscriptions, "no other backend's CLI is a vendor's subscription CLI"


# ───────────────────────── signed out ─────────────────────────


def _signed_out_scripts() -> dict[str, dict[str, Any]]:
    """Each signed-out signal of the fixture, as the fake ACP agent replays it."""
    error = {"code": SIGNED_OUT["acp_error_code"], "message": SIGNED_OUT["acp_error_message"]}
    scripts: dict[str, dict[str, Any]] = {
        "initialize": {"fail_initialize": error},
        "session/new": {"fail_session_new": error},
        "prompt": {"turns": [{"error": error["message"], "error_code": error["code"]}]},
    }
    for index, text in enumerate(SIGNED_OUT["result_texts"]):
        # An adapter that fails the turn with the CLI's result as its message, and one that relays it as text.
        scripts[f"error-{index}"] = {"turns": [{"error": text}]}
        scripts[f"said-{index}"] = {"turns": [{"messages": [text]}]}
    return scripts


SIGNED_OUT_SCRIPTS = _signed_out_scripts()
BEFORE_THE_PROMPT = {"initialize", "session/new"}


@pytest.mark.asyncio
@pytest.mark.parametrize("signal", sorted(SIGNED_OUT_SCRIPTS))
async def test_signed_out_is_subscription_login_required(
    stage_input: StageInput,
    runner_settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
) -> None:
    """Replayed from the fixture: a signed-out CLI ends the subscription run `subscription_login_required`,
    whether it said so before its first prompt (the pre-flight) or in a turn, with an exit code of its own; it
    is prompted once at most (no repair, no DoD round, no reminder), and the result says how its person signs
    in again on their runner."""
    assert SIGNED_OUT["marker"] in " ".join(SIGNED_OUT["result_texts"])
    monkeypatch.setenv(TOKEN_VARIABLE, TOKEN)
    prompts = tmp_path / "prompts.json"
    stage = _claude_code(stage_input, prompts)

    outcome, client = await _execute(stage, _subscription(runner_settings), SIGNED_OUT_SCRIPTS[signal])

    [result] = client.results
    assert (result.status, result.reason) == (StageStatus.FAILED, "subscription_login_required"), (
        result.summary
    )
    assert outcome.exit_code.name == "SUBSCRIPTION_SIGNED_OUT", outcome.detail
    assert SIGN_IN in result.summary
    assert len(_prompts(prompts)) == (0 if signal in BEFORE_THE_PROMPT else 1), "never asked again"
    if signal in BEFORE_THE_PROMPT:
        [failed] = [event["payload"] for event in client.events if event["type"] == "run.failed"]
        assert failed["reason"] == "subscription_login_required" and failed["phase"] in {
            "initialize",
            "session",
        }
    assert TOKEN not in json.dumps(client.events) + result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize(("signal", "reason"), [("session/new", "agent_session_refused"), ("prompt", None)])
async def test_a_gateway_run_signed_out_keeps_its_own_reasons(
    stage_input: StageInput,
    runner_settings: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: str,
    reason: str | None,
) -> None:
    """The signatures are read for a subscription run only (ADR 0227 §8): a gateway run's agent that answers
    `auth_required` ends as it did before S87-22, never with a reason that sends a person to a runner it does
    not use."""
    prompts = tmp_path / "prompts.json"
    stage = _claude_code(stage_input, prompts)

    outcome, client = await _execute(stage, runner_settings, SIGNED_OUT_SCRIPTS[signal])

    [result] = client.results
    assert (
        result.reason != "subscription_login_required" and outcome.exit_code.name != "SUBSCRIPTION_SIGNED_OUT"
    )
    if reason is not None:
        assert result.reason == reason
