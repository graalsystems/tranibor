"""S87-21: a run on its person's own subscription, on that person's runner only, never hands the platform its
token (ADR 0227 §2, §5, §6).

The daemon of a personal outpost starts the runner with `TRANIBOR_RUN_PLACE=outpost:<id>`,
`TRANIBOR_RUN_CREDENTIAL=subscription`, `TRANIBOR_RUN_PROVIDER=anthropic`, and the token in
`CLAUDE_CODE_OAUTH_TOKEN`. The runner refuses such a run anywhere else, opens no model listener for it,
leaves no gateway variable in its agent's environment (the S87-07 fixture: an API key outranks a
subscription), scrubs every URL and body it sends the platform, or forwards to an MCP server, by the token's
exact value and its shapes, uploads no deliverable that holds either, and refuses to push a history that holds
either, however a merge or a replace ref hides it. A gateway run is unchanged.

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
from tranibor_adapters.fakes import FakeHostedAgent
from tranibor_adapters.fakes.mcp import FakeMcpServer
from tranibor_contracts import ContextPack, McpServerRef, StageInput, StageStatus, ToolsRef, artifact_digest
from tranibor_contracts.enums import RunCredential, SubscriptionProvider
from tranibor_core.hosted import LOOPBACK_KEY, loopback_url
from tranibor_core.subscription_credentials import SCRUBBED, shapes_in
from tranibor_runner.exits import Exit
from tranibor_runner.runner import Runner
from tranibor_runner.workspace import git_dir_for

from .conftest import FakeInternalClient, agent_script, loopback_ports, port_of, valid_result
from .test_agent_holds_no_run_token import _read_probe
from .test_agent_isolation import _settings, serving  # noqa: F401  (a fixture)
from .test_hosted_step import CRM, CRM_SECRET, _hosted, _run

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
#: A platform service the step names as an MCP server (the project's memory), and the key the runner holds for
#: it. Built at run time (the CI's secrets scan).
MEMORY_HOST = "memory.internal"
MEMORY_KEY = "-".join(["mem", "key", "41d7" * 4])


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
        #: (name, bytes) of each deliverable stored, after the digest check the API makes.
        self.outputs: list[tuple[str, bytes]] = []
        #: (headers, JSON-RPC message) of each request the project's memory server received.
        self.memory: list[tuple[dict[str, str], dict[str, Any]]] = []

    async def answer(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.sent.append((request.method, str(request.url), body))
        if request.url.host == MEMORY_HOST:
            message = json.loads(body or b"{}")
            self.memory.append((dict(request.headers), message))
            text = {"content": [{"type": "text", "text": "remembered"}]}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": message.get("id"), "result": text})
        route = request.url.path.split(f"/runs/{self.stage_input.run_id}", 1)[-1]
        if route.startswith("/outputs/"):
            # What `routers/internal.py` does: the bytes received must hash to the digest announced.
            if request.headers.get("x-choregos-digest") != (received := artifact_digest(body)):
                return httpx.Response(422, json={"detail": f"the bytes received hash to {received}"})
            self.outputs.append((route.removeprefix("/outputs/"), body))
            return httpx.Response(200, json={"digest": received, "size": len(body)})
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["speaks", "exits", "mcp"])
async def test_the_fake_agents_echo_of_the_token_is_scrubbed(
    stage_input: StageInput,
    runner_settings: Any,
    platform: Platform,
    serving: tuple[int, list[Any]],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    """The fake ACP agent reads the token from its environment and echoes it on every path the runner
    forwards: its text, a tool call's arguments, a finding and a question through the run's tool server and
    its result (`speaks`); its error output, when it dies before `initialize` (`exits`); a `tools/call` to the
    project's memory server, in its arguments and its session id, through the runner's MCP listener when the
    agent is in its own container (`mcp`). No URL and no body the runner sends, to its platform or to a server
    it forwards to, holds it; each hit is journaled as a count, never as the value."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    settings, step = _subscription(runner_settings), stage_input
    if scenario == "mcp":
        port, _ = serving
        # Unconfined, as on a machine whose kernel cannot confine: the listener, not the kernel, is the wall.
        settings = _subscription(
            _settings(runner_settings, launcher_port=port, launcher_wait_seconds=10, confinement="off")
        )
        memory = McpServerRef(url=f"http://{MEMORY_HOST}/mcp", env={"Authorization": MEMORY_KEY})
        step = stage_input.model_copy(
            update={"tools": stage_input.tools.model_copy(update={"mcp": {"memory": memory}})}
        )
        platform.stage_input = step
    ports = loopback_ports(avoid=[port_of(settings.tools_url)])
    tools = settings.tools_url
    turns: dict[str, dict[str, Any]] = {
        "mcp": {
            "turns": [
                {
                    "http": [
                        {
                            **_tool_call(
                                f"{loopback_url(ports[1])}/mcp/memory",
                                "remember",
                                {"note": f"mine is {ECHO}"},
                            ),
                            "headers": {"accept": "application/json", "mcp-session-id": f"session-{ECHO}"},
                        }
                    ],
                    "result": valid_result(),
                }
            ]
        },
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
    agent_script(turns[scenario], step, settings.workspace)
    runner = Runner(settings)
    runner.loopback_ports = ports

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
    elif scenario == "mcp":
        assert outcome.exit_code is Exit.OK, outcome.detail
        [(headers, message)] = platform.memory
        assert message["params"]["arguments"] == {"note": f"mine is {SCRUBBED}"}, (
            "the call reached it, scrubbed"
        )
        assert headers["mcp-session-id"] == f"session-{SCRUBBED}", "the header the agent chose, scrubbed"
        assert headers["authorization"] == MEMORY_KEY, "the server's own credential, as the step declared it"
        assert CANARY not in json.dumps(headers)
    else:
        assert outcome.exit_code is Exit.AGENT_UNREACHABLE, outcome.detail
        [failed] = _events(platform.events, "run.failed")
        assert f"token={SCRUBBED}" in failed["stderr"], failed


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


#: The start of what an agent that reached the runner's own repository runs there (process mode on a machine
#: whose kernel cannot confine it, ADR 0146 §5). `$1`: the runner's git directory, `$2`: the work branch,
#: `$3`: what to hide in the history.
_IN_THE_RUNNERS_REPOSITORY = (
    'set -e; GD="$1"; BRANCH="$2"; LEAK="$3"\n'
    'g() { git --git-dir="$GD" --work-tree=. -c user.name=agent -c user.email=agent@example.invalid \\\n'
    '      -c commit.gpgsign=false -c core.hooksPath=/dev/null "$@"; }\n'
)
#: How the history hides the token from a plain read, in front of what a push carries:
#: - an evil merge: a side branch with an innocent commit, merged back with `--no-commit`, and the token added
#:   by the merge's resolution only, which neither parent holds; `git log --patch` prints no diff for a merge;
#: - a replaced commit: the token committed, then `git replace` makes every read of that commit read an
#:   innocent one instead, and the tree is reset to it; `git push` ignores replace refs, sends the real one.
HIDDEN_IN_HISTORY = {
    "evil merge": _IN_THE_RUNNERS_REPOSITORY
    + (
        "g checkout -q -b side\n"
        "mkdir -p tests && printf 'def test_side():\\n    pass\\n' > tests/test_side.py\n"
        "g add tests/test_side.py && g commit -q -m 'a test on the side'\n"
        'g checkout -q "$BRANCH" && g merge -q --no-ff --no-commit side\n'
        'printf "TOKEN = \'%s\'\\n" "$LEAK" >> src/orders.py\n'
        "g add src/orders.py && g commit -q -m 'Merge branch side'\n"
    ),
    "replaced commit": _IN_THE_RUNNERS_REPOSITORY
    + (
        "BASE=$(g rev-parse HEAD)\n"
        'printf "TOKEN = \'%s\'\\n" "$LEAK" >> src/orders.py\n'
        "g add src/orders.py && g commit -q -m 'a fix'\n"
        'g replace "$(g rev-parse HEAD)" "$(g commit-tree "$BASE^{tree}" -p "$BASE" -m \'a fix\')"\n'
        "g reset -q --hard\n"
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("hiding", sorted(HIDDEN_IN_HISTORY))
async def test_push_refused_when_the_history_hides_the_token(
    stage_input: StageInput,
    runner_settings: Any,
    toy_repo: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    hiding: str,
) -> None:
    """The token is in what a push carries, and a plain read of the commits misses it: an evil merge (in what
    the merge's resolution added, which a merge commit's own `git log --patch` does not show) or a replaced
    commit (every read of it reads another). The runner reads a merge against its first parent, the whole
    difference since the run began too, and the objects as they are, never their replacements: the push is
    refused all the same."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    settings = _subscription(runner_settings, confinement="off", git_dir_root=tmp_path / "git")
    git_dir = git_dir_for(settings.workspace, settings.git_dir_root)
    branch = "choregos/1-total"
    script = ["sh", "-c", HIDDEN_IN_HISTORY[hiding], hiding, str(git_dir), branch, ECHO]
    agent_script({"turns": [{"runs": [script], "result": valid_result()}]}, stage_input, settings.workspace)
    client = FakeInternalClient(stage_input)

    outcome = await Runner(settings, client).execute(stage_input, client)  # type: ignore[arg-type]

    assert outcome.exit_code is Exit.OK, outcome.detail

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "--git-dir", str(git_dir), *args], capture_output=True, text=True, check=True
        ).stdout

    assert CANARY in git("--no-replace-objects", "show", "HEAD:src/orders.py"), "the push would carry it"
    assert CANARY not in git("log", "--patch", "--text", f"main..{branch}"), "a plain read finds nothing"
    if hiding == "evil merge":
        assert len(git("rev-list", "--parents", "-n1", "HEAD").split()) == 3, "the agent's merge is the head"
    [push] = _events(client.events, "git.push")
    assert push == {
        "ok": False,
        "refused": "subscription_credential_in_diff",
        "exact_value": True,
        "shapes": [],
    }, push
    branches = subprocess.run(
        ["git", "ls-remote", "--heads", str(toy_repo)], capture_output=True, text=True, check=True
    ).stdout
    assert branch not in branches, "nothing was pushed"
    assert outcome.result is not None and outcome.result.status is StageStatus.FAILED
    assert outcome.result.reason == "subscription_credential_in_diff"


@pytest.mark.asyncio
@pytest.mark.parametrize("leaked", [ECHO, SHAPED], ids=["exact value", "shape"])
async def test_an_output_holding_the_token_is_not_uploaded(
    stage_input: StageInput,
    runner_settings: Any,
    platform: Platform,
    monkeypatch: pytest.MonkeyPatch,
    leaked: str,
) -> None:
    """A deliverable is the file the agent made, and the platform checks its digest: scrubbed on its way, it
    would arrive altered and be refused for a digest that does not match, a reason that says nothing of the
    token. The runner does not send it: it is journaled `outputs.refused` with its name and the shapes, never
    the value, and the result fails with `subscription_credential_in_output`. A clean deliverable of the same
    run is stored as it was made."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    writes = [
        {"path": ".choregos/outputs/notes.md", "content": f"the token is {leaked}\n"},
        {"path": ".choregos/outputs/clean.md", "content": "nothing to hide\n"},
    ]
    agent_script(
        {"turns": [{"writes": writes, "result": valid_result()}]}, stage_input, runner_settings.workspace
    )

    outcome = await Runner(_subscription(runner_settings)).run()

    assert outcome.exit_code is Exit.OK, outcome.detail
    assert platform.outputs == [("clean.md", b"nothing to hide\n")], "the clean one, as it was made"
    assert [url for method, url, _ in platform.sent if method == "PUT" and "notes.md" in url] == [], (
        "the other one is never sent"
    )
    assert _events(platform.events, "outputs.refused") == [
        {
            "file": "notes.md",
            "reason": "subscription_credential_in_output",
            "exact_value": leaked == ECHO,
            "shapes": [] if leaked == ECHO else ["Claude subscription token"],
        }
    ]
    [stored] = _events(platform.events, "outputs.stored")
    assert stored == {"stored": ["clean.md"], "refused": ["notes.md: subscription_credential_in_output"]}
    [result] = platform.results
    assert (result["status"], result["reason"]) == ("failed", "subscription_credential_in_output")
    assert _leaks(platform.sent, CANARY) == [] and _leaks(platform.sent, SHAPED) == []


@pytest.mark.asyncio
async def test_the_mcp_listener_of_a_hosted_step_scrubs_too(
    stage_input: StageInput, runner_settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same listener serves a hosted agent's container (ADR 0080), where it adds the server's credential
    itself rather than verbatim: a subscription run's scrub holds there too."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    crm = FakeMcpServer(tools=[{"name": "lookup", "description": "", "inputSchema": {"type": "object"}}])
    crm.token = CRM_SECRET
    agent = FakeHostedAgent(script=[("mcp", "crm", "lookup", {"customer": f"acme {CANARY}"})], answer="Acme.")
    tools = ToolsRef(mcp={"crm": McpServerRef(url=CRM, env={"Authorization": f"Bearer {CRM_SECRET}"})})

    outcome, client = await _run(
        _hosted(stage_input, tools=tools), agent, _subscription(runner_settings), mcp=crm
    )

    assert outcome.exit_code is Exit.OK, outcome.detail
    assert crm.calls == [("lookup", {"customer": f"acme {SCRUBBED}"})], "the call reached it, scrubbed"
    assert CANARY not in json.dumps(crm.received)
    assert sum(payload["count"] for payload in _events(client.events, "subscription.token_redacted")) == 1


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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_a_gateway_run_is_unchanged(
    stage_input: StageInput,
    runner_settings: Any,
    platform: Platform,
    toy_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No place, no credential: the run is a gateway run, as every run before ADR 0227. It is not refused, its
    agent keeps the gateway's variables, its client sends what it is given, and neither its push nor its
    deliverables are read."""
    monkeypatch.setenv(TOKEN_VARIABLE, CANARY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "runner-own-key")
    agent_script(
        {
            "turns": [
                {
                    "messages": [f"said {ECHO}"],
                    "writes": [
                        {"path": "src/orders.py", "content": f"TOKEN = '{ECHO}'\n"},
                        {"path": ".choregos/outputs/notes.md", "content": f"said {ECHO}\n"},
                    ],
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
    assert platform.outputs == [("notes.md", f"said {CANARY}\n".encode())]
    assert not _events(platform.events, "outputs.refused")
    [push] = _events(platform.events, "git.push")
    assert push["ok"] is True, push
    branches = subprocess.run(
        ["git", "ls-remote", "--heads", str(toy_repo)], capture_output=True, text=True, check=True
    ).stdout
    assert "choregos/1-total" in branches
