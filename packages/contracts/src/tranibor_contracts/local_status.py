# SPDX-License-Identifier: Apache-2.0
"""What `choregos local status|doctor --json` prints (ADR 0229 §2, `schemas/local-status.schema.json`).

A personal installation (ADR 0228) is driven from outside by `choregos local`, and the desktop app
knows it only through that command's `--json` (ADR 0229 §6): this shape is the only one it reads.
`status`, `doctor`, `open --print-url` and `relay enable` print it, the last two with the field that
is theirs (`open_url`, `relay.pairing`). It is not one of ADR 0168's frozen surfaces: the app always
calls the CLI sidecar of its own release, so both read the same version of it, and the shape is
closed. It says what is running and what was checked; it carries no secret: a runner's providers
are announced by name, never with a credential, and a pairing carries its user code, never its
device code.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Final, Literal, Self

from pydantic import Field, field_validator, model_validator

from .outpost import DAEMON_VERSION_PATTERN
from .relay import VERIFICATION_URI_PATTERN, RelayHostname, UserCode
from .workflow import Strict

#: A URL on the loopback door, where the edge publishes its one port (ADR 0228 §7). The console
#: window of the desktop app stays on that origin (ADR 0229 §7).
LOOPBACK_URL_PATTERN: Final = r"^https?://(127\.0\.0\.1|\[::1\]|localhost)(:[0-9]{1,5})?(/[^\s]*)?$"
SERVICE_NAME_PATTERN: Final = r"^[a-z0-9][a-z0-9_-]{0,62}$"
PROVIDER_NAME_PATTERN: Final = r"^[a-z][a-z0-9-]{0,31}$"
CHECK_NAME_PATTERN: Final = r"^[a-z][a-z0-9_.-]{0,63}$"

Version = Annotated[str, Field(pattern=DAEMON_VERSION_PATTERN)]
LoopbackUrl = Annotated[str, Field(pattern=LOOPBACK_URL_PATTERN, max_length=4096)]
ServiceName = Annotated[str, Field(pattern=SERVICE_NAME_PATTERN)]
ProviderName = Annotated[str, Field(pattern=PROVIDER_NAME_PATTERN)]
CheckName = Annotated[str, Field(pattern=CHECK_NAME_PATTERN)]

#: The stack as a whole: no data directory yet, no container engine answering, or what Compose says.
StackState = Literal["not_initialised", "unavailable", "stopped", "starting", "running", "degraded"]
#: A service's container as Compose reports it, or `missing`: the bundle declares it, no container is.
ServiceState = Literal["running", "created", "restarting", "paused", "exited", "dead", "removing", "missing"]
ServiceHealth = Literal["healthy", "unhealthy", "starting"]
#: The personal runner (ADR 0226), from its pid file and its presence in `/me/runners` (ADR 0229 §4).
RunnerState = Literal[
    "not_installed", "not_enrolled", "pending_approval", "stopped", "running", "disconnected"
]


def _distinct(names: list[str], what: str) -> list[str]:
    if len(set(names)) != len(names):
        raise ValueError(f"a {what} is listed once")
    return names


class LocalStack(Strict):
    """The Compose stack of the personal bundle."""

    state: StackState
    #: The images' version: the CLI's own (ADR 0229 §2). Null before the first `up`.
    version: Version | None = None
    #: The container engine found: Docker Engine, or Podman's Docker-compatible socket.
    engine: Literal["docker", "podman"] | None = None
    #: The console on the loopback door, without a ticket.
    url: LoopbackUrl | None = None


class LocalService(Strict):
    """One service of the bundle."""

    name: ServiceName
    state: ServiceState
    #: Null for a service without a health check.
    health: ServiceHealth | None = None


class LocalRunner(Strict):
    """The personal runner, `choregos-outpost` (ADR 0226), as `choregos local` supervises it."""

    state: RunnerState
    #: The daemon's own release; null when it is not installed.
    version: Version | None = None
    #: The providers its instance announces, by name only (`claude`): never a credential, never a
    #: token's shape (ADR 0227).
    providers: Annotated[list[ProviderName], Field(max_length=16)] = Field(default_factory=list)

    @field_validator("providers")
    @classmethod
    def _a_provider_is_listed_once(cls, providers: list[str]) -> list[str]:
        return _distinct(providers, "provider")


class LocalRelayPairing(Strict):
    """`relay enable` while the owner has not entered the code: what the app shows them (ADR 0230 §6).
    The device code stays with the agent."""

    user_code: UserCode
    verification_uri: Annotated[str, Field(pattern=VERIFICATION_URI_PATTERN, max_length=2000)]
    expires_at: datetime


class LocalRelay(Strict):
    """The relay agent (ADR 0230): off, being paired, or on under its hostname."""

    enabled: bool
    #: `<id>.<relay-domain>`, when enabled.
    hostname: RelayHostname | None = None
    #: `relay enable`, until the owner enters the code.
    pairing: LocalRelayPairing | None = None

    @model_validator(mode="after")
    def _enabled_has_a_hostname_and_no_pairing(self) -> Self:
        if self.enabled and (self.hostname is None or self.pairing is not None):
            raise ValueError("an enabled relay has a hostname and no pairing under way")
        if not self.enabled and self.hostname is not None:
            raise ValueError("a relay that is not enabled has no hostname")
        return self


class LocalBackups(Strict):
    """The last backup `choregos local backup` made (S87-30), and where backups go."""

    last: datetime | None = None
    #: A directory of the person's machine.
    location: Annotated[str, Field(min_length=1, max_length=4096)] | None = None


class LocalCheck(Strict):
    """One check of `doctor`: what it looked at, whether it passed, and what it found."""

    name: CheckName
    ok: bool
    detail: Annotated[str, Field(max_length=2000)]


class LocalStatus(Strict):
    """The JSON of `choregos local status|doctor|open --print-url|relay enable --json`."""

    #: The CLI that printed it: the desktop app's own sidecar, of its own release.
    cli_version: Version
    stack: LocalStack
    services: Annotated[list[LocalService], Field(max_length=64)]
    runner: LocalRunner
    relay: LocalRelay
    backups: LocalBackups
    #: `doctor` lists every check; `status` may list none.
    checks: Annotated[list[LocalCheck], Field(max_length=128)]
    #: `open --print-url` only: the console's URL with a single-use owner ticket (ADR 0228), on the
    #: loopback door.
    open_url: LoopbackUrl | None = None

    @field_validator("services")
    @classmethod
    def _a_service_is_listed_once(cls, services: list[LocalService]) -> list[LocalService]:
        _distinct([service.name for service in services], "service")
        return services

    @field_validator("checks")
    @classmethod
    def _a_check_is_listed_once(cls, checks: list[LocalCheck]) -> list[LocalCheck]:
        _distinct([check.name for check in checks], "check")
        return checks
