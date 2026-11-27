# SPDX-License-Identifier: Apache-2.0
"""A person's vendor sign-in on their own runner, and where it may go (ADR 0227 §4).

A person runs `claude setup-token` on their own machine, and gives what it printed to
`choregos-outpost personal login claude`, which reads it from standard input only: never an argument (the
shell's history and the process list keep those), never an environment variable. Its shape is checked
(`tranibor_core.subscription_credentials`: a Claude subscription token, whole), and it is kept:

- in the OS keychain when the machine has one: macOS Keychain or a Secret Service (GNOME Keyring, KWallet),
  through `keyring`, the optional `keychain` extra of this package. Service `choregos-outpost`, user name the
  provider (`anthropic`). A keychain is reached by a local call (a D-Bus session on Linux), never by network;
- otherwise in `<state dir>/secrets/anthropic.token`: a directory 0700 and a file 0600, written beside and
  renamed into place. A file that its group or anyone else may read is not used (`Presence.problem`).

Nothing else holds it. `doctor` says whether it is there and where it is kept, never what it is
(`doctor.subscription_checks`); the daemon's log masks it wherever a line would carry it (it is remembered as
a known secret when it is read, `tranibor_core.redaction`); the journal never receives it. The platform never
sees it: the daemon announces the provider in its runner block only while a usable token is there
(`runner_block`), and puts the token only in the environment of a run whose credential is `subscription`
with that provider (`child_environment`), where the runner hands it to the vendor's CLI and nothing else.

The daemon loop that calls `runner_block` at each poll and `child_environment` for each run it starts is
S87-25's; `choregos local runner login claude` and the desktop app pass the person's standard input through
to this command without reading it (ADR 0229 §5, §8).
"""

from __future__ import annotations

import contextlib
import os
import re
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TextIO

from tranibor_contracts.enums import OfferKind, RunCredential, SubscriptionProvider
from tranibor_contracts.outpost import OutpostRun, OutpostRunner, RunnerMode
from tranibor_core.redaction import remember_secret
from tranibor_core.subscription_credentials import SUBSCRIPTION_SHAPES, SUBSCRIPTION_VARIABLES

#: The vendors a person signs in to with `personal login`, and the provider each one's subscription is.
#: Claude only (ADR 0227 §12): no Google sign-in path is allowed for this use yet.
VENDORS: Mapping[str, SubscriptionProvider] = MappingProxyType({"claude": SubscriptionProvider.ANTHROPIC})
#: The variable each provider's CLI reads its token from (the S87-07 fixture: `CLAUDE_CODE_OAUTH_TOKEN`).
TOKEN_VARIABLES: Mapping[SubscriptionProvider, str] = MappingProxyType(
    {SubscriptionProvider.ANTHROPIC: "CLAUDE_CODE_OAUTH_TOKEN"}
)
#: The shape a provider's token has, whole: a label of `SUBSCRIPTION_SHAPES`.
TOKEN_SHAPES: Mapping[SubscriptionProvider, str] = MappingProxyType(
    {SubscriptionProvider.ANTHROPIC: "Claude subscription token"}
)
#: How each provider's token is made, for the person who must make one.
MADE_BY: Mapping[SubscriptionProvider, str] = MappingProxyType(
    {SubscriptionProvider.ANTHROPIC: "`claude setup-token`"}
)
#: Where the file fallback lives, under the state directory.
SECRETS_DIR = "secrets"
#: The keychain's service name; the user name is the provider.
KEYCHAIN_SERVICE = "choregos-outpost"
#: The longest standard input read: a token is about a hundred characters.
MAX_INPUT = 4096
#: What a file holding a token may not grant: anything to its group or to others.
_TOO_OPEN = stat.S_IRWXG | stat.S_IRWXO
_WHOLE = {label: re.compile(pattern) for label, pattern in SUBSCRIPTION_SHAPES}
#: An Anthropic API key, which a person might paste by mistake: it pays per call, it is not a subscription.
_API_KEY = re.compile(r"^sk-ant-api\d{2}-")
#: The variables the daemon sets for a run itself, which a run's offer cannot set (ADR 0226 §9, ADR 0227 §2).
DAEMON_VARIABLES = frozenset(
    {
        "TRANIBOR_RUN_ID",
        "TRANIBOR_API_URL",
        "TRANIBOR_RUN_TOKEN",
        "TRANIBOR_RUN_PLACE",
        "TRANIBOR_RUN_CREDENTIAL",
        "TRANIBOR_RUN_PROVIDER",
    }
)
#: Every variable that holds a person's subscription token: none comes from the offer or the daemon's own
#: environment; the one a run gets is the daemon's to put there.
ALL_TOKEN_VARIABLES = frozenset(TOKEN_VARIABLES.values()) | SUBSCRIPTION_VARIABLES


class SecretError(Exception):
    """A token cannot be taken or used as it is; the message says why and what to do, never the value."""


class Keychain(Protocol):
    """What `keyring` offers: one password per service and user name."""

    def get_password(self, service: str, username: str) -> str | None: ...

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def delete_password(self, service: str, username: str) -> None: ...


def system_keychain() -> Keychain | None:
    """The OS keychain through `keyring`, when `keyring` is installed and the machine has a backend that keeps
    secrets (macOS Keychain, a Secret Service). `None` otherwise, never an error: the token then goes to a
    file only its owner reads."""
    try:
        import keyring  # type: ignore[import-not-found, unused-ignore]
        from keyring.backends import fail  # type: ignore[import-not-found, unused-ignore]
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
        usable = not isinstance(backend, fail.Keyring) and float(getattr(backend, "priority", 0)) > 0
    except Exception:  # a backend that cannot even say what it is keeps nothing
        return None
    return keyring if usable else None  # type: ignore[no-any-return, unused-ignore]


@dataclass(frozen=True)
class Presence:
    """Whether a provider's token is on this runner, and where: never the token."""

    provider: SubscriptionProvider
    #: "the OS keychain", or the file's path; `None` when there is none.
    where: str | None
    #: Why a token that is there cannot be used: a file open to others, a value of another shape.
    problem: str | None = None

    @property
    def usable(self) -> bool:
        return self.where is not None and self.problem is None


def check_shape(provider: SubscriptionProvider, given: str) -> str:
    """`given` without its surrounding blanks, when it is one token of the provider's shape, whole. The error
    names what was expected, or what was given instead, never the value."""
    token = given.strip()
    made_by = MADE_BY[provider]
    if not token:
        raise SecretError(f"nothing on standard input: paste the token {made_by} printed, then press Enter")
    if _API_KEY.match(token):
        raise SecretError(
            "an Anthropic API key is not a subscription: it belongs to the model gateway of a deployment, "
            f"not to your runner. Give the token {made_by} printed"
        )
    if not _WHOLE[TOKEN_SHAPES[provider]].fullmatch(token):
        raise SecretError(
            f"this is not a Claude subscription token ({len(token)} characters; one token alone, as "
            f"{made_by} printed it, is expected)"
        )
    return token


def read_token(stdin: TextIO, prompt: TextIO, provider: SubscriptionProvider) -> str:
    """What the person gave on standard input, and nothing else: on a terminal, a line read without echo; from
    a pipe, what it carries (bounded)."""
    if stdin.isatty():
        prompt.write(f"Paste the token {MADE_BY[provider]} printed (it is not shown), then press Enter: ")
        prompt.flush()
        return _without_echo(stdin, prompt)
    return stdin.read(MAX_INPUT + 1)


def _without_echo(stdin: TextIO, prompt: TextIO) -> str:
    """One line of the terminal on standard input, with its echo off while it is typed."""
    import termios

    descriptor = stdin.fileno()
    before = termios.tcgetattr(descriptor)
    quiet = termios.tcgetattr(descriptor)
    quiet[3] &= ~termios.ECHO
    try:
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, quiet)
        return stdin.readline(MAX_INPUT + 1)
    finally:
        termios.tcsetattr(descriptor, termios.TCSAFLUSH, before)
        prompt.write("\n")


class VendorSecrets:
    """The tokens of a runner's person, by provider: in the keychain when there is one, else in files."""

    def __init__(self, state_dir: Path, keychain: Keychain | None) -> None:
        self.directory = state_dir / SECRETS_DIR
        self.keychain = keychain

    @classmethod
    def at(cls, state_dir: Path) -> VendorSecrets:
        """The runner's tokens under `state_dir`, with the machine's keychain if it has one."""
        return cls(state_dir, system_keychain())

    def path(self, provider: SubscriptionProvider) -> Path:
        return self.directory / f"{provider.value}.token"

    def store(self, provider: SubscriptionProvider, given: str) -> str:
        """Keep a token of the provider's shape (`check_shape`); where it was kept, as a person reads it. A
        keychain that refuses it leaves the file to keep it, and the answer says so."""
        token = check_shape(provider, given)
        remember_secret(token)
        if self.keychain is not None:
            try:
                self.keychain.set_password(KEYCHAIN_SERVICE, provider.value, token)
            except Exception as error:  # a locked or broken keychain: the file keeps it
                where = self._write(provider, token)
                return f"{where} (the OS keychain refused it: {type(error).__name__})"
            self._remove_file(provider)
            return "the OS keychain"
        return self._write(provider, token)

    def presence(self, provider: SubscriptionProvider) -> Presence:
        """Whether a token of the provider is here and usable, and where; never raises, never reads it out."""
        try:
            token, where = self._read(provider)
        except SecretError as error:
            return Presence(provider, str(self.path(provider)), str(error))
        if token is None:
            return Presence(provider, None)
        if not _WHOLE[TOKEN_SHAPES[provider]].fullmatch(token):
            return Presence(provider, where, "what is kept is not a Claude subscription token: sign in again")
        return Presence(provider, where)

    def token(self, provider: SubscriptionProvider) -> str | None:
        """The provider's token, for a run's environment only (`child_environment`); `None` when there is no
        usable one."""
        if not self.presence(provider).usable:
            return None
        token, _ = self._read(provider)
        return token

    def remove(self, provider: SubscriptionProvider) -> bool:
        """Forget the provider's token, wherever it is kept; whether there was one."""
        removed = False
        if self.keychain is not None:
            with contextlib.suppress(Exception):  # nothing kept there is not an error
                if self.keychain.get_password(KEYCHAIN_SERVICE, provider.value) is not None:
                    self.keychain.delete_password(KEYCHAIN_SERVICE, provider.value)
                    removed = True
        return self._remove_file(provider) or removed

    # ───────────────────────── the keychain, then the file ─────────────────────────

    def _read(self, provider: SubscriptionProvider) -> tuple[str | None, str | None]:
        if self.keychain is not None:
            try:
                kept = self.keychain.get_password(KEYCHAIN_SERVICE, provider.value)
            except Exception:  # a keychain that cannot be read now: the file may still hold one
                kept = None
            if kept:
                remember_secret(kept)
                return kept.strip(), "the OS keychain"
        path = self.path(provider)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            return None, None
        except OSError as error:
            raise SecretError(f"{path}: {error.strerror or error}") from error
        if mode & _TOO_OPEN:
            raise SecretError(
                f"{path} is open to others (mode {mode:04o}): a token is read by its owner only (chmod 600 "
                f"{path}, or sign in again)"
            )
        try:
            kept = path.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise SecretError(f"{path}: {error.strerror or error}") from error
        remember_secret(kept)
        return kept or None, str(path)

    def _write(self, provider: SubscriptionProvider, token: str) -> str:
        """The token in its file, 0600 in a directory 0700: written beside, then renamed over the old one."""
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        path = self.path(provider)
        beside = path.with_name(f".{path.name}.{os.getpid()}")
        with contextlib.suppress(FileNotFoundError):
            beside.unlink()
        descriptor = os.open(beside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(token + "\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(beside, path)
        except BaseException:
            with contextlib.suppress(OSError):
                beside.unlink()
            raise
        return str(path)

    def _remove_file(self, provider: SubscriptionProvider) -> bool:
        try:
            self.path(provider).unlink()
        except FileNotFoundError:
            return False
        return True


# ───────────────────────── what the daemon announces, and what a run gets ─────────────────────────


def announced(secrets: VendorSecrets) -> list[SubscriptionProvider]:
    """The providers whose sign-in is on this runner and usable now: what the runner block names, and only
    while it lasts (ADR 0227 §4). A name, never the credential."""
    return [provider for provider in TOKEN_VARIABLES if secrets.presence(provider).usable]


def runner_block(
    secrets: VendorSecrets,
    *,
    version: str,
    modes: Sequence[RunnerMode],
    offer_kinds: Sequence[OfferKind],
    run_capacity: int,
) -> OutpostRunner:
    """The `runner` block of a poll of protocol 2 (ADR 0226 §3), its `subscriptions` read from this runner's
    sign-ins at the moment it is built: the daemon loop (S87-25) builds one for each poll."""
    return OutpostRunner(
        version=version,
        modes=list(modes),
        subscriptions=announced(secrets),
        offer_kinds=list(offer_kinds),
        run_capacity=run_capacity,
    )


def child_environment(
    run: OutpostRun, *, outpost: str, secrets: VendorSecrets, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """The environment of the runner a run starts (ADR 0226 §9, ADR 0227 §2, §4): `base` (what the daemon
    lets a run inherit) and the offer's `env`, then what the daemon alone decides, where the run is
    (`TRANIBOR_RUN_PLACE=outpost:<outpost>`), what pays for its model, its id, its API and its token.

    The person's token is put there for a run whose credential is `subscription` with a provider that has
    one on this runner, under the variable its CLI reads, and for no other run. Neither `base` nor the offer
    can name a token or a variable the daemon decides: theirs are dropped. A subscription run claimed while a
    token was there and started after it went gets none, and its runner reports the CLI signed out
    (`subscription_login_required`)."""
    environment = {
        name: value
        for name, value in {**(base or {}), **run.env}.items()
        if name not in DAEMON_VARIABLES and name not in ALL_TOKEN_VARIABLES
    }
    environment.update(
        {
            "TRANIBOR_RUN_ID": run.run_id or run.id,
            "TRANIBOR_API_URL": run.api_url,
            "TRANIBOR_RUN_TOKEN": run.run_token,
            "TRANIBOR_RUN_PLACE": f"outpost:{outpost}",
            "TRANIBOR_RUN_CREDENTIAL": run.credential.value,
        }
    )
    if run.credential is RunCredential.SUBSCRIPTION and run.provider is not None:
        environment["TRANIBOR_RUN_PROVIDER"] = run.provider.value
        variable = TOKEN_VARIABLES.get(run.provider)
        token = secrets.token(run.provider) if variable else None
        if variable and token:
            environment[variable] = token
    return environment


__all__ = [
    "Keychain",
    "Presence",
    "SecretError",
    "VendorSecrets",
    "announced",
    "check_shape",
    "child_environment",
    "read_token",
    "runner_block",
    "system_keychain",
]
