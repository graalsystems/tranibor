# SPDX-License-Identifier: Apache-2.0
"""`choregos admin temporal-keys`: an organisation's Temporal data keys (ADR 0224).

Each organisation's Temporal payloads are sealed under a random data key, wrapped by the deployment's key
wrapper. These commands list the keys (never a key itself), rotate the active one, and shred them all:
crypto-shredding, after which the organisation's Temporal payloads can never be read again. They call
`/orgs/{org}/temporal-keys…` and need the organisation's administrator.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from .client import Client, NoOrganisationError

temporal_keys_app = typer.Typer(help="An organisation's Temporal data keys: list, rotate, shred (ADR 0224)")
console = Console()

OrgOption = Annotated[str | None, typer.Option("--org", help="the organisation; the profile's otherwise")]


def _target(org: str | None) -> tuple[Client, str]:
    from . import main

    api = main.client()
    try:
        return api, api.profile.organisation(org)
    except NoOrganisationError as exc:
        main.fail(str(exc))
        raise


@temporal_keys_app.command("list")
def keys_list(org: OrgOption = None) -> None:
    """The organisation's Temporal data keys: id, state, wrapping key, dates."""
    api, slug = _target(org)
    answer: dict[str, Any] = api.get(f"/orgs/{slug}/temporal-keys")
    if not answer.get("available"):
        console.print(
            "[yellow]![/yellow] no key wrapper is configured: this organisation's Temporal keys are "
            "derived from the master key (ADR 0220) and cannot be shredded"
        )
    table = Table("key", "state", "wrapped by", "created", "shredded", title=f"Temporal keys of {slug}")
    for key in answer.get("keys") or []:
        table.add_row(
            key["key_id"],
            key["state"],
            key["kek_id"],
            key["created_at"],
            key.get("shredded_at") or "—",
        )
    console.print(table)


@temporal_keys_app.command("rotate")
def keys_rotate(org: OrgOption = None) -> None:
    """A new data key for the organisation's new Temporal payloads; the retired ones still open theirs."""
    api, slug = _target(org)
    key = api.post(f"/orgs/{slug}/temporal-keys/rotation")
    console.print(
        f"[green]✓[/green] {slug}: new Temporal data key {key['key_id']} (wrapped by {key['kek_id']})"
    )


@temporal_keys_app.command("shred")
def keys_shred(
    reason: Annotated[str, typer.Option("--reason", help="why, kept in the audit log")],
    confirm: Annotated[
        str | None,
        typer.Option("--confirm", help="the organisation's slug again; asked for when absent"),
    ] = None,
    org: OrgOption = None,
) -> None:
    """Destroy every Temporal data key of the organisation: its Temporal payloads can never be read again.

    Irreversible. Terminate the organisation's open workflows first: they fail their next task."""
    api, slug = _target(org)
    typed = confirm if confirm is not None else typer.prompt(f"Type `{slug}` to shred its Temporal keys")
    answer = api.post(f"/orgs/{slug}/temporal-keys/shredding", json={"confirm": typed, "reason": reason})
    shredded = answer.get("shredded") or []
    console.print(f"[green]✓[/green] {slug}: {len(shredded)} Temporal data key(s) shredded")
    console.print("Not erased:")
    for line in answer.get("not_erased") or []:
        console.print(f"  - {line}")


__all__ = ["temporal_keys_app"]
