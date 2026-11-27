"""S87-21: a run on its person's own subscription, on that person's runner only, never hands the platform its
token (ADR 0227 §2, §5, §6).

The daemon of a personal outpost starts the runner with `TRANIBOR_RUN_PLACE=outpost:<id>`,
`TRANIBOR_RUN_CREDENTIAL=subscription`, `TRANIBOR_RUN_PROVIDER=anthropic`, and the token in
`CLAUDE_CODE_OAUTH_TOKEN`. The runner refuses such a run anywhere else, opens no model listener for it,
leaves no gateway variable in its agent's environment (the S87-07 fixture: an API key outranks a
subscription), scrubs every URL and body it sends the platform by the token's exact value and its shapes,
and refuses to push a diff that holds either. A gateway run is unchanged.

The canary matches no subscription shape on purpose: only the exact-value scrub can catch it, so the
platform's shape refusal (S87-20) cannot be what makes these tests pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from tranibor_contracts import ContextPack, StageInput, StageStatus
from tranibor_contracts.enums import RunCredential, SubscriptionProvider
from tranibor_core.hosted import LOOPBACK_KEY, loopback_url
from tranibor_core.subscription_credentials import SCRUBBED, shapes_in
from tranibor_runner.exits import Exit
from tranibor_runner.runner import Runner

from .conftest import FakeInternalClient, agent_script, loopback_ports, port_of, valid_result
from .test_agent_holds_no_run_token import _read_probe
from .test_agent_isolation import _settings, serving  # noqa: F401  (a fixture)

pytestmark = pytest.mark.asyncio

#: The person's token as the runner's environment holds it. It matches no shape (see the module's docstring).
CANARY = "-".join(["canary", "subscription", "5e1f" * 8])
TOKEN_VARIABLE = "CLAUDE_CODE_OAUTH_TOKEN"
#: What the fake agent says, writes or calls in place of the token it read from its own environment.
ECHO = "{env:CLAUDE_CODE_OAUTH_TOKEN}"
#: The run's gateway key, which a subscription run must not use. Built at run time (the CI's secrets scan).
GATEWAY = "-".join(["sk", "run", "8b2c" * 4])
#: The variables that would choose another credential than the subscription, or another endpoint.
GATEWAY_VARIABLES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "GEMINI_API_KEY",
    "GOOGLE_GEMINI_BASE_URL",
)
FIXTURE = Path(__file__).parent / "fixtures" / "subscription" / "from_source.json"


def _subscription(settings: Any, place: str = "outpost:op-1", **overrides: Any) -> Any:
    return settings.model_copy(
        update={
            "run_credential": RunCredential.SUBSCRIPTION,
            "run_provider": SubscriptionProvider.ANTHROPIC,
            "run_place": place,
            **overrides,
        }
    )


def _events(events: list[dict[str, Any]], type_: str) -> list[dict[str, Any]]:
    return [event["payload"] for event in events if event["type"] == type_]


def _spy_agent_env(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """The environment each agent of the test is started with, as the ACP client holds it."""
    import tranibor_runner.runner as module

    seen: list[dict[str, str]] = []
    real = module.AcpClient

    class _Spy(real):  # type: ignore[valid-type, misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            seen.append(dict(self.env))

    monkeypatch.setattr(module, "AcpClient", _Spy)
    return seen


class Platform:
    """The run's internal API at the end of the wire: every request as it left the runner's process."""

    def __init__(self, stage_input: StageInput) -> None:
        self.stage_input = stage_input
        self.sent: list[tuple[str, str, bytes]] = []
        #: What carried each request: `AsyncHTTPProxy` through a proxy, `AsyncConnectionPool` directly.
        self.through: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.findings: list[dict[str, Any]] = []
        self.questions: list[dict[str, Any]] = []
        self.results: list[dict[str, Any]] = []

    async def answer(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.sent.append((request.method, str(request.url), body))
        route = request.url.path.split(f"/runs/{self.stage_input.run_id}", 1)[-1]
        payload: Any = json.loads(body) if body else None
        answers: dict[str, Any] = {
            "/input": lambda: self.stage_input.model_dump(mode="json", by_alias=True),
            "/context": lambda: ContextPack.empty(self.stage_input.work_item.title).model_dump(mode="json"),
            "/git-token": lambda: {"token": None},
            "/tools": lambda: {"tools": []},
            "/events": lambda: self.events.extend(payload["events"]) or {"accepted": len(payload["events"])},
            "/findings": lambda: self.findings.append(payload) or {"finding_id": "f1", "remaining": 2},
            "/question": lambda: self.questions.append(payload) or {"status": "accepted"},
            "/result": lambda: self.results.append(payload) or {"status": "recorded"},
        }
        if route not in answers:
            return httpx.Response(404, json={"detail": f"not served here: {route}"})
        return httpx.Response(200, json=answers[route]())


@pytest.fixture
def platform(stage_input: StageInput, monkeypatch: pytest.MonkeyPatch) -> Platform:
    """The network itself: whatever client, transport or proxy the runner sends through, its request ends in
    an `httpx.AsyncHTTPTransport`, which hands it here instead of to a socket."""
    api = Platform(stage_input)

    async def handle(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        api.through.append(type(transport._pool).__name__)
        return await api.answer(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return api


def _leaks(sent: list[tuple[str, str, bytes]], value: str) -> list[str]:
    """Each request whose URL or body holds `value`, in clear or percent-encoded."""
    forms = {value, quote(value, safe="")}
    return [
        f"{method} {url}"
        for method, url, body in sent
        if any(form in url or form.encode() in body for form in forms)
    ]


def _tool_call(url: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """What the fake agent POSTs to the run's tool server: an MCP `tools/call`, as its MCP client would."""
    return {
        "url": url,
        "body": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    }


# ───────────────────────── where a subscription run may run ─────────────────────────


@pytest.mark.parametrize("place", ["", "k8s_job", "outpost:"])
async def test_refused_off_an_outpost(
    stage_input: StageInput, runner_settings: Any, monkeypatch: pytest.MonkeyPatch, place: str
) -> None:
    """Anything but its person's outpost (`outpost:<id>`, from the daemon's claim) refuses a subscription run
    before its agent starts, with an exit code of its own and `run.failed` at the `credential` phase."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    agent_script(
        {"turns": [{"messages": ["started"], "result": valid_result()}]},
        stage_input,
        runner_settings.workspace,
    )
    client = FakeInternalClient(stage_input)
    runner = Runner(_subscription(runner_settings, place=place), client)  # type: ignore[arg-type]

    outcome = await runner.execute(stage_input, client)  # type: ignore[arg-type]

    assert outcome.exit_code is Exit.SUBSCRIPTION_OFF_RUNNER, outcome.detail
    assert len({int(code) for code in Exit.__members__.values()}) == len(Exit.__members__), (
        "a code of its own"
    )
    assert "outpost" in Exit.SUBSCRIPTION_OFF_RUNNER.explanation
    [failed] = _events(client.events, "run.failed")
    assert failed["phase"] == "credential" and failed["reason"] == "subscription_off_runner"
    assert [event["type"] for event in client.events] == ["run.failed"], "nothing started: no clone, no agent"
    assert not client.results
    assert CANARY not in json.dumps(client.events)


# ───────────────────────── nothing of the token reaches the platform ─────────────────────────


@pytest.mark.parametrize("scenario", ["speaks", "exits"])
async def test_the_fake_agents_echo_of_the_token_is_scrubbed(
    stage_input: StageInput,
    runner_settings: Any,
    platform: Platform,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """The fake ACP agent reads the token from its environment and echoes it on every path the runner
    forwards: its text, a tool call's arguments, a finding and a question through the run's tool server and
    its result (`speaks`); its error output, when it dies before `initialize` (`exits`). No URL and no body
    the runner sends holds it; each hit is journaled as a count, never as the value."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    tools = runner_settings.tools_url
    turns: dict[str, dict[str, Any]] = {
        "speaks": {
            "turns": [
                {
                    "messages": [f"the token I hold is {ECHO}"],
                    "tool_calls": [
                        {
                            "tool": "Bash",
                            "kind": "execute",
                            "input": {"command": f"curl -H 'x-api-key: {ECHO}'"},
                        }
                    ],
                    "http": [
                        _tool_call(
                            tools,
                            "report_finding",
                            {
                                "title": f"a token {ECHO}",
                                "type": "security",
                                "severity": "high",
                                "evidence": ECHO,
                            },
                        ),
                        _tool_call(tools, "ask_human", {"question": f"is {ECHO} yours?"}),
                    ],
                    "result": valid_result(summary=f"done with {ECHO}"),
                }
            ]
        },
        "exits": {"exit_at_start": 3, "exit_stderr": [f"auth: token={ECHO} refused"]},
    }
    agent_script(turns[scenario], stage_input, runner_settings.workspace)
    runner = Runner(_subscription(runner_settings))

    outcome = await runner.run()

    assert platform.sent, "the runner reached its platform"
    assert _leaks(platform.sent, CANARY) == []
    assert all(shapes_in(url + body.decode(errors="replace")) == [] for _, url, body in platform.sent)
    redacted = _events(platform.events, "subscription.token_redacted")
    assert redacted and all(set(payload) == {"count"} for payload in redacted), redacted
    assert sum(payload["count"] for payload in redacted) > 0, "the agent did hold and echo the token"
    assert runner.scrubber is not None
    assert sum(payload["count"] for payload in redacted) == runner.scrubber.redacted, "every hit journaled"
    if scenario == "speaks":
        assert outcome.exit_code is Exit.OK, outcome.detail
        updates = json.dumps(_events(platform.events, "session/update"))
        assert f"the token I hold is {SCRUBBED}" in updates, "its text, scrubbed"
        assert f"x-api-key: {SCRUBBED}" in updates, "its tool call's arguments, scrubbed"
        assert [finding["evidence"] for finding in platform.findings] == [SCRUBBED]
        assert [question["text"] for question in platform.questions] == [f"is {SCRUBBED} yours?"]
        assert [result["summary"] for result in platform.results] == [f"done with {SCRUBBED}"]
    else:
        assert outcome.exit_code is Exit.AGENT_UNREACHABLE, outcome.detail
        [failed] = _events(platform.events, "run.failed")
        assert f"token={SCRUBBED}" in failed["stderr"], failed


async def test_the_scrub_holds_when_the_platform_is_reached_through_a_proxy(
    stage_input: StageInput, runner_settings: Any, platform: Platform, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person's machine may reach its installation through a proxy of its own, which httpx takes from the
    environment only for a client without a transport of its own: the scrub is not a transport, so the client
    still goes through the proxy, and still scrubs the URL and the body."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    for name in ("HTTP_PROXY", "http_proxy"):
        monkeypatch.setenv(name, "http://proxy.person.example:3128")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    from tranibor_runner.subscription import Scrubber, platform_client

    scrubber = Scrubber([CANARY])
    client = platform_client(_subscription(runner_settings), scrubber)
    try:
        await client.post_events([{"seq": 1, "type": "note", "payload": {"text": f"held {CANARY}"}}])
        await client.relay_tool_call(CANARY, json.dumps({"q": CANARY}).encode(), script=None, within_s=5)
    finally:
        await client.aclose()

    assert platform.through == ["AsyncHTTPProxy", "AsyncHTTPProxy"], "through the person's proxy"
    assert _leaks(platform.sent, CANARY) == []
    assert scrubber.take() == 3 and scrubber.take() == 0, "counted once, journaled once"


async def test_the_scrub_takes_out_the_shapes_without_a_value() -> None:
    """A provider whose token the runner cannot read (`google` has no variable yet, ADR 0227 §12) is still
    scrubbed of every subscription shape; a value too short to be a token is not scrubbed by its value."""
    from tranibor_runner.subscription import Scrubber

    shaped = "-".join(["sk", "ant", "ort01", "Wd4" * 10])
    google = ".".join(["ya29", "a0Kd" * 6])
    scrubber = Scrubber(["short"])
    request = httpx.Request("POST", f"http://localhost:0/x/{shaped}", json={"a": google, "b": "short"})

    scrubbed = await scrubber.request(request)

    assert str(scrubbed.url).endswith("/x/[SUBSCRIPTION%20CREDENTIAL%20REMOVED]")
    assert json.loads(scrubbed.content) == {"a": SCRUBBED, "b": "short"}
    assert scrubbed.headers["content-length"] == str(len(scrubbed.content))
    assert scrubber.take() == 2
    untouched = httpx.Request("GET", "http://localhost:0/x/input")
    assert await scrubber.request(untouched) is untouched, "nothing to take out: the request as it came"


#: Another subscription token, caught by its shape only. Built at run time (the CI's secrets scan).
SHAPED = "-".join(["sk", "ant", "oat01", "Zq7" * 12])


@pytest.mark.parametrize("leaked", [ECHO, SHAPED], ids=["exact value", "shape"])
async def test_push_refused_when_the_diff_holds_the_token(
    stage_input: StageInput,
    runner_settings: Any,
    toy_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    leaked: str,
) -> None:
    """The agent writes its token (or another subscription token) into a file in its scope: the runner commits
    it locally and does not push. The journal names the refusal and the shape, never the value; the result
    fails, saying why."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    agent_script(
        {
            "turns": [
                {
                    "writes": [{"path": "src/orders.py", "content": f"TOKEN = '{leaked}'\n"}],
                    "result": valid_result(),
                }
            ]
        },
        stage_input,
        runner_settings.workspace,
    )
    client = FakeInternalClient(stage_input)

    outcome = await Runner(_subscription(runner_settings), client).execute(stage_input, client)  # type: ignore[arg-type]

    assert outcome.exit_code is Exit.OK, outcome.detail
    [push] = _events(client.events, "git.push")
    assert push["ok"] is False and push["refused"] == "subscription_credential_in_diff", push
    assert push["exact_value"] is (leaked == ECHO), push
    assert push["shapes"] == ([] if leaked == ECHO else ["Claude subscription token"]), push
    branches = subprocess.run(
        ["git", "ls-remote", "--heads", str(toy_repo)], capture_output=True, text=True, check=True
    ).stdout
    assert "choregos/1-total" not in branches, "nothing was pushed"
    assert outcome.result is not None and outcome.result.status is StageStatus.FAILED
    assert outcome.result.reason == "subscription_credential_in_diff"
    assert CANARY not in json.dumps(push) and SHAPED not in json.dumps(push)


# ───────────────────────── the agent's model and environment ─────────────────────────

#: Which loopback ports answer, as the agent's own process finds them: `refused` is nobody listening, any
#: other error is named (the confinement's own refusal would be a `PermissionError`).
PORTS_PROBE = (
    "import socket, sys\n"
    "for label, port in (('model', sys.argv[1]), ('mcp', sys.argv[2])):\n"
    "    try:\n"
    "        socket.create_connection(('127.0.0.1', int(port)), timeout=3).close()\n"
    "        print(label + ' -> open')\n"
    "    except ConnectionRefusedError:\n"
    "        print(label + ' -> refused')\n"
    "    except OSError as exc:\n"
    "        print(label + ' -> ' + type(exc).__name__)\n"
)


@pytest.mark.parametrize("credential", [RunCredential.SUBSCRIPTION, RunCredential.GATEWAY])
async def test_no_model_listener_for_a_subscription_run(
    stage_input: StageInput,
    runner_settings: Any,
    serving: tuple[int, list[Any]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    credential: RunCredential,
) -> None:
    """With the agent in its own container, a subscription run serves it the MCP and CONNECT listeners and no
    model listener: the vendor's CLI reaches its provider itself, through the tunnel (ADR 0227 §5). A gateway
    run still gets its model listener and the loopback key."""
    port, _ = serving
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    step = stage_input.model_copy(update={"gateway_key": GATEWAY})
    # Unconfined: the kernel's port rule (ADR 0094) would refuse a port the agent was not given, listened on
    # or not, and the probe asks whether anyone listens.
    base = _settings(runner_settings, launcher_port=port, launcher_wait_seconds=10, confinement="off")
    settings = _subscription(base) if credential is RunCredential.SUBSCRIPTION else base
    ports = loopback_ports(avoid=[port_of(settings.tools_url)])
    probe = [sys.executable, "-c", PORTS_PROBE, str(ports[0]), str(ports[1])]
    agent_script(
        {"turns": [{"runs": [probe], "result": valid_result(summary="{grep}")}]}, step, settings.workspace
    )
    client = FakeInternalClient(step)
    runner = Runner(settings, client)  # type: ignore[arg-type]
    runner.loopback_ports = ports
    runner.model_transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    seen = _spy_agent_env(monkeypatch)

    outcome = await runner.execute(step, client)  # type: ignore[arg-type]

    assert outcome.exit_code is Exit.OK, outcome.detail
    answered = _read_probe(client.results[0].summary)
    [env] = seen
    if credential is RunCredential.SUBSCRIPTION:
        assert answered == {"model": "refused", "mcp": "open"}, answered
        assert not {"OPENAI_BASE_URL", "OPENAI_API_KEY"} & set(env), "no gateway, not even the loopback one"
        assert GATEWAY not in json.dumps(env)
    else:
        assert answered == {"model": "open", "mcp": "open"}, answered
        assert env["OPENAI_BASE_URL"] == loopback_url(ports[0]) and env["OPENAI_API_KEY"] == LOOPBACK_KEY


async def test_the_agent_env_has_no_gateway_variable(
    stage_input: StageInput, runner_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner's own environment has every gateway variable and the step carries a gateway key: the agent
    of a subscription run gets none of them (an API key outranks a subscription, S87-07), and its person's
    token, which is what it runs on."""
    outrank = json.loads(FIXTURE.read_text("utf-8"))["credential"]["outrank_a_subscription"]
    named = [name for name in outrank if name.isupper()]
    assert {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"} <= set(named) <= set(GATEWAY_VARIABLES)
    for name in GATEWAY_VARIABLES:
        monkeypatch.setenv(name, f"runner-own-{name.lower()}")
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    step = stage_input.model_copy(update={"gateway_key": GATEWAY})
    agent_script({"turns": [{"result": valid_result()}]}, step, runner_settings.workspace)
    client = FakeInternalClient(step)
    seen = _spy_agent_env(monkeypatch)
    handed: list[str | None] = []
    import tranibor_runner.runner as module

    real = module.get_backend

    def get_backend(name: str) -> Any:
        """The run's backend, noting the gateway key each `model_env` is handed."""
        backend = real(name)
        model_env = backend.model_env

        def noting(model: Any, key: str | None) -> dict[str, str]:
            handed.append(key)
            return model_env(model, key)

        monkeypatch.setattr(backend, "model_env", noting)
        return backend

    monkeypatch.setattr(module, "get_backend", get_backend)

    outcome = await Runner(_subscription(runner_settings), client).execute(step, client)  # type: ignore[arg-type]

    assert outcome.exit_code is Exit.OK, outcome.detail
    [env] = seen
    assert sorted(set(GATEWAY_VARIABLES) & set(env)) == []
    assert GATEWAY not in json.dumps(env) and "runner-own-" not in json.dumps(env)
    assert env[TOKEN_VARIABLE] == CANARY, "the vendor's CLI runs on its person's subscription"
    assert handed == [None], "no backend is handed the step's gateway key"


def test_the_agent_view_of_a_subscription_run_names_no_gateway(stage_input: StageInput) -> None:
    """In its own container, a gateway run's agent is told the loopback model listener and key; a
    subscription run's agent is told neither, and is not handed the step's key."""
    from tranibor_runner.loopback.agent import agent_view

    step = stage_input.model_copy(update={"gateway_key": GATEWAY})
    ports = (4201, 4202, 4203)

    subscription = agent_view(step, ports, credential=RunCredential.SUBSCRIPTION)
    gateway = agent_view(step, ports)

    assert subscription.gateway_key is None and subscription.model == step.model
    assert gateway.gateway_key == LOOPBACK_KEY and gateway.model.base_url == loopback_url(ports[0])


def test_the_runner_reads_the_daemons_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    """What the daemon of a personal outpost sets is what the runner reads; an empty variable is an unset
    one; a credential it does not know is refused, never taken for `gateway`."""
    from pydantic import ValidationError
    from tranibor_runner.config import RunnerSettings

    monkeypatch.setenv("TRANIBOR_RUN_PLACE", "outpost:op-7")
    monkeypatch.setenv("TRANIBOR_RUN_CREDENTIAL", "subscription")
    monkeypatch.setenv("TRANIBOR_RUN_PROVIDER", "anthropic")
    settings = RunnerSettings()
    assert (settings.run_place, settings.run_credential, settings.run_provider) == (
        "outpost:op-7",
        RunCredential.SUBSCRIPTION,
        SubscriptionProvider.ANTHROPIC,
    )
    monkeypatch.setenv("TRANIBOR_RUN_CREDENTIAL", "")
    monkeypatch.setenv("TRANIBOR_RUN_PROVIDER", "")
    unset = RunnerSettings()
    assert (unset.run_credential, unset.run_provider) == (RunCredential.GATEWAY, None)
    monkeypatch.setenv("TRANIBOR_RUN_CREDENTIAL", "subscriptoin")
    with pytest.raises(ValidationError):
        RunnerSettings()


# ───────────────────────── a gateway run ─────────────────────────


async def test_a_gateway_run_is_unchanged(
    stage_input: StageInput,
    runner_settings: Any,
    platform: Platform,
    toy_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No place, no credential: the run is a gateway run, as every run before ADR 0227. It is not refused, its
    agent keeps the gateway's variables, its client sends what it is given and its push is not read."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "runner-own-key")
    agent_script(
        {
            "turns": [
                {
                    "messages": [f"said {ECHO}"],
                    "writes": [{"path": "src/orders.py", "content": f"TOKEN = '{ECHO}'\n"}],
                    "result": valid_result(),
                }
            ]
        },
        stage_input,
        runner_settings.workspace,
    )
    seen = _spy_agent_env(monkeypatch)

    outcome = await Runner(runner_settings).run()

    assert outcome.exit_code is Exit.OK, outcome.detail
    [env] = seen
    assert env["OPENAI_API_KEY"] == stage_input.gateway_key and env["ANTHROPIC_API_KEY"] == "runner-own-key"
    assert _leaks(platform.sent, CANARY), "a gateway run's client is not the subscription's"
    assert not _events(platform.events, "subscription.token_redacted")
    [push] = _events(platform.events, "git.push")
    assert push["ok"] is True, push
    branches = subprocess.run(
        ["git", "ls-remote", "--heads", str(toy_repo)], capture_output=True, text=True, check=True
    ).stdout
    assert "choregos/1-total" in branches
