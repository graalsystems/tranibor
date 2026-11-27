# SPDX-License-Identifier: Apache-2.0
"""`choregos-relay serve | check`: the relay's command, what its image runs (ADR 0230 §12).

- `serve` runs the relay (`passthrough.Relay`) on the configuration its environment gives, until it
  is sent SIGTERM or SIGINT.
- `check` reads that configuration, says what the relay would serve, and exits.

The configuration is the environment, `TRANIBOR_RELAY_*`, as a container and its chart
(`charts/choregos-relay`) set it:

- `DOMAIN` (required): `<relay-domain>`; an installation's name is `<id>.<relay-domain>`;
- `RETENTION_DAYS` (required): how long a metadata line is kept, in whole days;
- `METADATA_PATH`: where the metadata lines are written (`/var/lib/choregos-relay/metadata.jsonl`);
- `OWN_HOST`: the relay's own host, which agents dial (the domain);
- `SNI_PORT` (443) and `OWN_PORT` (8443): the two listeners' ports;
- `OWN_PUBLIC_PORT`: the port agents dial the own host on, which their signatures bind (the own port);
- `OWN_TLS_CERT_FILE` and `OWN_TLS_KEY_FILE`: the own host's certificate and key, when the relay
  terminates that TLS itself (none: a load balancer or an Ingress in front of it does);
- `VERIFICATION_URI`: where an owner enters a user code (`https://<own host>/pair`);
- `IDLE_TIMEOUT_S`: a spliced connection idle this long is closed (900);
- `LIMIT_<NAME>`: each of `RelayLimits` (`LIMIT_CONCURRENT_STREAMS`, `LIMIT_STREAMS_PER_MINUTE`, ...).

There is no variable for a certificate or a key of an installation's name: the relay has none
(ADR 0230 §4). The chart also passes `TRANIBOR_RELAY_DATABASE_URL` and `TRANIBOR_RELAY_SIGNING_KEY_FILE`
from the operator's Secret; this relay reads neither, its registry being in memory
(`pairing.RelayRegistry`).

The command writes what it serves when it starts, and why it refuses a configuration, on stderr. It
writes nothing about a connection: the relay keeps one metadata line per connection and nothing else
(ADR 0230 §7). An error the event loop catches is written as its type alone, never with the
connection it came from.

Exit codes: 0 stopped on request; 2 the configuration is refused, or the relay could not open its
listeners or its metadata lines.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import os
import signal
import ssl
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, Final

from tranibor_contracts.relay import RELAY_PROTOCOL, protocol_window

from . import __version__
from .limits import RelayLimits
from .metadata import RelayConfigError
from .passthrough import Relay, RelayConfig

PREFIX: Final = "TRANIBOR_RELAY_"
#: Where the image and the chart keep the metadata lines: a volume of their own.
DEFAULT_METADATA_PATH: Final = Path("/var/lib/choregos-relay/metadata.jsonl")
#: The configuration is refused, or the relay could not open what it needs.
CANNOT_START: Final = 2
_DEFAULTS: Final = RelayConfig(domain="relay.invalid", metadata_path=DEFAULT_METADATA_PATH, retention=None)

__all__ = ["CANNOT_START", "DEFAULT_METADATA_PATH", "PREFIX", "main", "relay_config", "serve"]


def _say(text: str) -> None:
    print(f"choregos-relay: {text}", file=sys.stderr, flush=True)


def _integer(
    environ: Mapping[str, str], name: str, *, minimum: int, maximum: int | None = None
) -> int | None:
    raw = environ.get(PREFIX + name, "").strip()
    if not raw:
        return None
    if not (raw.isascii() and raw.isdigit()):
        raise RelayConfigError(f"{PREFIX}{name}={raw!r}: not a whole number")
    value = int(raw)
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"between {minimum} and {maximum}" if maximum is not None else f"at least {minimum}"
        raise RelayConfigError(f"{PREFIX}{name}={value}: must be {bound}")
    return value


def _port(environ: Mapping[str, str], name: str, default: int) -> int:
    """A port; 0 asks the system for a free one (a test's)."""
    value = _integer(environ, name, minimum=0, maximum=65_535)
    return default if value is None else value


def _limits(environ: Mapping[str, str]) -> RelayLimits:
    values: dict[str, Any] = {}
    for field in dataclasses.fields(RelayLimits):
        value = _integer(environ, f"LIMIT_{field.name.upper()}", minimum=1)
        if value is not None:
            values[field.name] = value
    return RelayLimits(**values)


def _own_tls(environ: Mapping[str, str]) -> ssl.SSLContext | None:
    cert = environ.get(PREFIX + "OWN_TLS_CERT_FILE", "").strip()
    key = environ.get(PREFIX + "OWN_TLS_KEY_FILE", "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise RelayConfigError(
            f"{PREFIX}OWN_TLS_CERT_FILE and {PREFIX}OWN_TLS_KEY_FILE go together: the own host's "
            "certificate and its key"
        )
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as unreadable:
        raise RelayConfigError(
            f"the own host's certificate and key do not load: {unreadable}"
        ) from unreadable
    return context


def relay_config(environ: Mapping[str, str]) -> RelayConfig:
    """The relay's configuration from `environ`; `RelayConfigError` says what is refused."""
    domain = environ.get(PREFIX + "DOMAIN", "").strip()
    if not domain:
        raise RelayConfigError(f"{PREFIX}DOMAIN is required: an installation's name is <id>.<relay-domain>")
    days = _integer(environ, "RETENTION_DAYS", minimum=1)
    if days is None:
        raise RelayConfigError(
            f"{PREFIX}RETENTION_DAYS is required: a relay keeps its metadata lines for a fixed retention, "
            "which its operator sets (ADR 0230 §7)"
        )
    verification = environ.get(PREFIX + "VERIFICATION_URI", "").strip() or None
    if verification is not None and not verification.startswith("https://"):
        raise RelayConfigError(f"{PREFIX}VERIFICATION_URI={verification!r}: an https URL")
    idle = environ.get(PREFIX + "IDLE_TIMEOUT_S", "").strip()
    try:
        idle_s = float(idle) if idle else _DEFAULTS.idle_timeout_s
    except ValueError as broken:
        raise RelayConfigError(f"{PREFIX}IDLE_TIMEOUT_S={idle!r}: not a number of seconds") from broken
    if not idle_s > 0:
        raise RelayConfigError(f"{PREFIX}IDLE_TIMEOUT_S={idle!r}: must be above 0")
    try:
        limits = _limits(environ)
    except ValueError as refused:
        raise RelayConfigError(str(refused)) from refused
    return RelayConfig(
        domain=domain,
        metadata_path=Path(environ.get(PREFIX + "METADATA_PATH", "").strip() or DEFAULT_METADATA_PATH),
        retention=timedelta(days=days),
        own_host=environ.get(PREFIX + "OWN_HOST", "").strip() or None,
        sni_port=_port(environ, "SNI_PORT", _DEFAULTS.sni_port),
        own_port=_port(environ, "OWN_PORT", _DEFAULTS.own_port),
        own_tls=_own_tls(environ),
        own_public_port=_integer(environ, "OWN_PUBLIC_PORT", minimum=1, maximum=65_535),
        limits=limits,
        verification_uri=verification,
        idle_timeout_s=idle_s,
    )


def _summary(relay: Relay, *, listening: bool) -> str:
    config = relay.config
    days = config.retention.days if config.retention else 0
    sni = f":{relay.sni_port}" if listening else f":{config.sni_port}"
    own = f":{relay.own_port}" if listening else f":{config.own_port}"
    tls = "with its own TLS" if config.own_tls else "behind whatever terminates its TLS"
    window = protocol_window(config.protocol)
    return (
        f"<id>.{config.domain} on the SNI listener {sni}, without TLS; the own host {relay.origin} "
        f"on {own}, {tls}; protocols {window.minimum} to {window.current}; metadata lines kept "
        f"{days} day{'' if days == 1 else 's'} in {config.metadata_path}"
    )


def _writable(path: Path) -> None:
    """Opens the metadata lines' file for appending, once: a relay that could not keep its lines does
    not start (`OSError`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8"):
        pass


def _quiet_loop_errors(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    """What the loop catches is written as its type: its context names a connection, an address."""
    error = context.get("exception")
    _say(f"error caught by the event loop: {type(error).__name__ if error else 'unknown'}")


async def serve(
    config: RelayConfig,
    *,
    stop: asyncio.Event | None = None,
    started: Callable[[Relay], Awaitable[None] | None] | None = None,
) -> None:
    """Runs the relay until `stop` is set, or until SIGTERM or SIGINT when none is given."""
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_quiet_loop_errors)
    if stop is None:
        stop = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
    relay = Relay(config)
    await asyncio.to_thread(_writable, config.metadata_path)
    await relay.start()
    try:
        _say("serving " + _summary(relay, listening=True))
        if started is not None:
            outcome = started(relay)
            if outcome is not None:
                await outcome
        await stop.wait()
    finally:
        await relay.stop()
    _say("stopped")


def _parser() -> argparse.ArgumentParser:
    window = protocol_window(RELAY_PROTOCOL)
    parser = argparse.ArgumentParser(
        prog="choregos-relay",
        description="The relay a personal installation dials out to: it routes a phone's TLS by SNI "
        "and never terminates it (ADR 0230). Configured by TRANIBOR_RELAY_* variables.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"choregos-relay {__version__} (protocols {window.minimum} to {window.current})",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the relay until SIGTERM")
    commands.add_parser("check", help="read the configuration, say what the relay would serve, and exit")
    return parser


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    """Runs the command; its exit code."""
    arguments = _parser().parse_args(argv)
    try:
        config = relay_config(os.environ if environ is None else environ)
        if arguments.command == "check":
            _say("would serve " + _summary(Relay(config), listening=False))
            return 0
        asyncio.run(serve(config))
    except RelayConfigError as refused:
        _say(f"refused: {refused}")
        return CANNOT_START
    except OSError as unopened:
        where = f" ({unopened.filename})" if unopened.filename else ""
        _say(f"could not start: {unopened.strerror or type(unopened).__name__}{where}")
        return CANNOT_START
    return 0
