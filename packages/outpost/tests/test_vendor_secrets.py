# SPDX-License-Identifier: Apache-2.0
"""S87-22: a person's Claude sign-in, kept on their runner, handed to their subscription runs (ADR 0227 §4).

`choregos-outpost personal login claude` reads what `claude setup-token` printed from standard input, and from
nowhere else: not an argument, which the shell's history and the process list keep, not an environment
variable. It checks the value's shape, and keeps it in the OS keychain when the machine has one (macOS
Keychain, Secret Service, through the optional `keyring`), else in `<state>/secrets/anthropic.token`, a file
only its owner reads. `doctor` says whether it is there, never what it is; neither the daemon's log nor its
journal ever holds it. The daemon announces `anthropic` in its runner block only while a usable token is
there, and the environment it builds for a run's runner carries the token only for a run whose credential is
`subscription` with the provider `anthropic`.
"""

from __future__ import annotations

import asyncio
import io
import json
import stat
from pathlib import Path
from typing import Any

import pytest
from tranibor_contracts.enums import OfferKind, RunCredential, SubscriptionProvider
from tranibor_contracts.outpost import OutpostRun
from tranibor_outpost import doctor, logs
from tranibor_outpost import secrets as vendor_secrets
from tranibor_outpost.cli import app
from tranibor_outpost.settings import Settings
from typer.testing import CliRunner

#: What `claude setup-token` prints: an OAuth token of a Claude subscription (the S87-20 shape).
TOKEN = "sk-ant-oat01-" + "Kc3" * 30
#: An Anthropic API key: not a subscription, refused here (it belongs to the model gateway).
API_KEY = "sk-ant-api03-" + "Pq8" * 30
TOKEN_FILE = Path("secrets") / "anthropic.token"


class FakeKeychain:
    """An OS keychain as `keyring` exposes one: a password per service and user name."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.items.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.items[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.items[(service, username)]


@pytest.fixture
def no_keychain(monkeypatch: pytest.MonkeyPatch) -> None:
    """A machine without a keychain backend (or without `keyring` installed): the file fallback."""
    monkeypatch.setattr(vendor_secrets, "system_keychain", lambda: None)


@pytest.fixture
def keychain(monkeypatch: pytest.MonkeyPatch) -> FakeKeychain:
    found = FakeKeychain()
    monkeypatch.setattr(vendor_secrets, "system_keychain", lambda: found)
    return found


def _cli(state: Path, *arguments: str, stdin: str = "", env: dict[str, str] | None = None) -> Any:
    return CliRunner().invoke(app, ["--state-dir", str(state), *arguments], input=stdin, env=env)


def _everything_written(state: Path, *, but: Path | None = None) -> str:
    """Every file under the state directory, as text, the token's own file aside."""
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(state.rglob("*"))
        if path.is_file() and path != but
    )


def _offer(credential: RunCredential, provider: SubscriptionProvider | None, **env: str) -> OutpostRun:
    return OutpostRun(
        id="run-7",
        kind=OfferKind.STAGE_RUN,
        run_id="run-7",
        api_url="https://runs.choregos.example/api/v1/internal",
        run_token=".".join(["header", "claims-of-run-7", "signature"]),
        env=env,
        credential=credential,
        provider=provider,
        isolation="container",
        lease_s=300,
        max_minutes=30,
    )


# ───────────────────────── standard input only ─────────────────────────


def test_login_reads_the_token_from_standard_input_only(
    tmp_path: Path, no_keychain: None, daemon_log: io.StringIO
) -> None:
    """The token comes from standard input: given as an argument, or as an option, it is refused without
    being stored or echoed; an environment variable holding one is not read."""
    state = tmp_path / "state"
    secrets = vendor_secrets.VendorSecrets.at(state)

    as_argument = _cli(state, "personal", "login", "claude", TOKEN)
    as_option = _cli(state, "personal", "login", "claude", "--token", TOKEN)
    from_environment = _cli(state, "personal", "login", "claude", env={"CLAUDE_CODE_OAUTH_TOKEN": TOKEN})

    for refused in (as_argument, as_option):
        assert refused.exit_code == 2 and "standard input" in refused.output, refused.output
        assert TOKEN not in refused.output
    assert from_environment.exit_code == 2 and "nothing on standard input" in from_environment.output
    assert not secrets.presence(SubscriptionProvider.ANTHROPIC).usable and not (state / TOKEN_FILE).exists()

    stored = _cli(state, "personal", "login", "claude", stdin=TOKEN + "\n")

    assert stored.exit_code == 0, stored.output
    assert TOKEN not in stored.output and str(state / TOKEN_FILE) in stored.output
    assert secrets.token(SubscriptionProvider.ANTHROPIC) == TOKEN
    assert TOKEN not in daemon_log.getvalue()


@pytest.mark.parametrize(
    ("given", "said"),
    [
        (API_KEY, "an Anthropic API key is not a subscription"),
        ("", "nothing on standard input"),
        (TOKEN + " " + TOKEN, "not a Claude subscription token"),
        ("sk-ant-oat01-short", "not a Claude subscription token"),
    ],
)
def test_login_refuses_what_is_not_a_claude_subscription_token(
    tmp_path: Path, no_keychain: None, given: str, said: str
) -> None:
    """The shape is checked (`tranibor_core.subscription_credentials`); what is refused is named by its kind,
    never repeated."""
    state = tmp_path / "state"

    refused = _cli(state, "personal", "login", "claude", stdin=given)

    assert refused.exit_code == 2 and said in refused.output, refused.output
    assert (given not in refused.output) if given else True
    assert not (state / TOKEN_FILE).exists()


# ───────────────────────── where it is kept ─────────────────────────


def test_without_a_keychain_the_token_is_a_file_only_its_owner_reads(
    tmp_path: Path, no_keychain: None
) -> None:
    state = tmp_path / "state"
    assert _cli(state, "personal", "login", "claude", stdin=TOKEN).exit_code == 0

    path = state / TOKEN_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.read_text(encoding="utf-8").strip() == TOKEN

    # A second login replaces it, still 0600, with no temporary copy left beside it.
    again = "sk-ant-oat01-" + "Hy2" * 30
    assert _cli(state, "personal", "login", "claude", stdin=again).exit_code == 0
    assert path.read_text(encoding="utf-8").strip() == again
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and sorted(p.name for p in path.parent.iterdir()) == [
        "anthropic.token"
    ]

    # Opened to others by hand, it is no longer used: neither announced nor handed to a run.
    path.chmod(0o644)
    secrets = vendor_secrets.VendorSecrets.at(state)
    presence = secrets.presence(SubscriptionProvider.ANTHROPIC)
    assert not presence.usable and "open to others" in str(presence.problem)
    assert vendor_secrets.announced(secrets) == []


def test_with_a_keychain_the_token_is_kept_there_and_no_file_is_written(
    tmp_path: Path, keychain: FakeKeychain
) -> None:
    state = tmp_path / "state"
    stored = _cli(state, "personal", "login", "claude", stdin=TOKEN)

    assert stored.exit_code == 0 and "keychain" in stored.output, stored.output
    assert list(keychain.items.values()) == [TOKEN]
    assert not (state / TOKEN_FILE).exists()
    assert vendor_secrets.VendorSecrets.at(state).token(SubscriptionProvider.ANTHROPIC) == TOKEN

    assert _cli(state, "personal", "logout", "claude").exit_code == 0
    assert keychain.items == {}


def test_the_keychain_is_found_through_keyring_or_not_at_all() -> None:
    """`keyring` is an optional dependency (the `keychain` extra): without it, or without a backend that keeps
    secrets, the discovery answers `None` and never raises."""
    found = vendor_secrets.system_keychain()
    assert found is None or callable(getattr(found, "get_password", None))


def test_on_a_terminal_the_token_is_typed_without_echo() -> None:
    """Standard input on a terminal: the prompt goes to the error output, the line is read with the
    terminal's echo off, so the token never shows on the screen, and the echo is back on afterwards."""
    import os
    import pty
    import select
    import termios
    import threading
    import time

    master, slave = pty.openpty()
    prompt = io.StringIO()
    read: list[str] = []
    with open(slave, encoding="utf-8", closefd=True) as terminal:
        echoing = termios.tcgetattr(slave)[3] & termios.ECHO
        reader = threading.Thread(
            target=lambda: read.append(
                vendor_secrets.read_token(terminal, prompt, SubscriptionProvider.ANTHROPIC)
            )
        )
        reader.start()
        deadline = time.monotonic() + 10
        while termios.tcgetattr(slave)[3] & termios.ECHO and time.monotonic() < deadline:
            time.sleep(0.01)
        os.write(master, (TOKEN + "\n").encode())
        reader.join(10)
        echoed = ""
        while select.select([master], [], [], 0.2)[0]:  # what the terminal shows back, if anything
            echoed += os.read(master, 65536).decode(errors="replace")
        echo_after = termios.tcgetattr(slave)[3] & termios.ECHO
    os.close(master)

    assert echoing and read == [TOKEN + "\n"]
    assert TOKEN not in echoed, "the token was echoed on the terminal"
    assert "claude setup-token" in prompt.getvalue() and echo_after, "the terminal echoes again"


# ───────────────────────── never shown, never written elsewhere ─────────────────────────


def test_never_in_the_log_the_doctor_or_the_journal(
    tmp_path: Path, no_keychain: None, daemon_log: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`doctor` reports the sign-in's presence and where it is kept; the value is in no line it prints, in no
    line of the daemon's log (even one that would carry it by mistake), and in no other file of the state
    directory, the journal's directory included, after a login, a doctor, a run's environment and a logout."""
    state = tmp_path / "state"
    settings = Settings(policy=tmp_path / "no-policy.yaml", state_dir=state)
    before = {check.name: check for check in asyncio.run(doctor.diagnose(settings))}
    assert before["subscription anthropic"].status == "skipped"
    assert "personal login claude" in before["subscription anthropic"].detail

    assert _cli(state, "personal", "login", "claude", stdin=TOKEN).exit_code == 0
    (state / "journal").mkdir(parents=True, exist_ok=True)
    checks = asyncio.run(doctor.diagnose(settings))
    found = {check.name: check for check in checks}
    environment = vendor_secrets.child_environment(
        _offer(RunCredential.SUBSCRIPTION, SubscriptionProvider.ANTHROPIC),
        outpost="op-1",
        secrets=vendor_secrets.VendorSecrets.at(state),
    )
    # Back to the daemon's log, which each command of the CLI had pointed at its own output.
    monkeypatch.setattr("sys.stdout", daemon_log)
    logs.configure("DEBUG")
    logs.get_logger().info("run.starting", environment=environment)
    typed = _cli(state, "--policy", str(settings.policy), "doctor")
    printed = doctor.render(checks) + doctor.as_json(checks) + typed.output

    assert "subscription anthropic" in typed.output, typed.output
    assert found["subscription anthropic"].status == "ok", found["subscription anthropic"]
    assert str(state / TOKEN_FILE) in found["subscription anthropic"].detail
    assert environment["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN, "the positive control: the run gets it"
    assert TOKEN not in printed
    assert TOKEN not in daemon_log.getvalue() and "run.starting" in daemon_log.getvalue()
    assert TOKEN not in _everything_written(state, but=state / TOKEN_FILE)

    assert _cli(state, "personal", "logout", "claude").exit_code == 0
    assert not (state / TOKEN_FILE).exists() and TOKEN not in _everything_written(state)


# ───────────────────────── announced while present, handed to its runs only ─────────────────────────


def test_anthropic_is_announced_only_while_a_token_is_present(tmp_path: Path, no_keychain: None) -> None:
    state = tmp_path / "state"
    secrets = vendor_secrets.VendorSecrets.at(state)

    def announced() -> list[SubscriptionProvider]:
        block = vendor_secrets.runner_block(
            secrets, version="0.29.5", modes=["docker"], offer_kinds=[OfferKind.STAGE_RUN], run_capacity=1
        )
        return list(block.subscriptions)

    assert announced() == []
    assert _cli(state, "personal", "login", "claude", stdin=TOKEN).exit_code == 0
    assert announced() == [SubscriptionProvider.ANTHROPIC]
    assert _cli(state, "personal", "logout", "claude").exit_code == 0
    assert announced() == []
    assert _cli(state, "personal", "login", "claude", stdin=TOKEN).exit_code == 0
    (state / TOKEN_FILE).unlink()
    assert announced() == [], "removed by hand: no longer announced"


def test_the_token_reaches_only_a_subscription_run_of_its_provider(
    tmp_path: Path, keychain: FakeKeychain
) -> None:
    """The runner's environment for a run: the token for a `subscription` run of `anthropic`, under the
    variable the vendor's CLI reads; never for a gateway run, even when the daemon's own environment holds
    one, nor for another provider; and the offer's environment cannot name the token nor what the daemon
    itself decides (where the run is, what pays for its model)."""
    state = tmp_path / "state"
    assert _cli(state, "personal", "login", "claude", stdin=TOKEN).exit_code == 0
    secrets = vendor_secrets.VendorSecrets.at(state)
    daemons_own = {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": TOKEN}
    forged = {
        "CLAUDE_CODE_OAUTH_TOKEN": "forged-" + "f" * 30,
        "TRANIBOR_RUN_PLACE": "k8s_job",
        "LANG": "C.UTF-8",
    }

    subscription = vendor_secrets.child_environment(
        _offer(RunCredential.SUBSCRIPTION, SubscriptionProvider.ANTHROPIC, **forged),
        outpost="op-1",
        secrets=secrets,
        base=daemons_own,
    )
    gateway = vendor_secrets.child_environment(
        _offer(RunCredential.GATEWAY, None, **forged), outpost="op-1", secrets=secrets, base=daemons_own
    )
    google = vendor_secrets.child_environment(
        _offer(RunCredential.SUBSCRIPTION, SubscriptionProvider.GOOGLE), outpost="op-1", secrets=secrets
    )

    assert subscription["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    decided = ("TRANIBOR_RUN_PLACE", "TRANIBOR_RUN_CREDENTIAL", "TRANIBOR_RUN_PROVIDER", "TRANIBOR_RUN_ID")
    assert {name: subscription[name] for name in decided} == {
        "TRANIBOR_RUN_PLACE": "outpost:op-1",
        "TRANIBOR_RUN_CREDENTIAL": "subscription",
        "TRANIBOR_RUN_PROVIDER": "anthropic",
        "TRANIBOR_RUN_ID": "run-7",
    }
    assert subscription["LANG"] == "C.UTF-8" and subscription["PATH"] == "/usr/bin"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in gateway and TOKEN not in json.dumps(gateway)
    assert "forged-" not in json.dumps(gateway) and gateway["TRANIBOR_RUN_PLACE"] == "outpost:op-1"
    assert gateway["TRANIBOR_RUN_CREDENTIAL"] == "gateway" and "TRANIBOR_RUN_PROVIDER" not in gateway
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in google and TOKEN not in json.dumps(google)

    # Signed out between the claim and the start: no token, and the runner reports the CLI signed out.
    assert _cli(state, "personal", "logout", "claude").exit_code == 0
    signed_out = vendor_secrets.child_environment(
        _offer(RunCredential.SUBSCRIPTION, SubscriptionProvider.ANTHROPIC), outpost="op-1", secrets=secrets
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in signed_out
