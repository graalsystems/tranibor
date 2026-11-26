# SPDX-License-Identifier: Apache-2.0
"""Private ranges a plugin server may reach, per organisation (ADR 0224 §6a, amends ADR 0096 §8).

The operator lists CIDRs (`global.plugins.servers.privateCidrs`); on a multi-organisation deployment each
is owned by one organisation (`privateCidrsByOrg`, keyed by slug: it is what an operator reads, the
organisation's id exists only once it is created; an alias of the organisation counts, ADR 0090 §1, §4).

- **At typing** (`check_hosts`, an instance's configuration saved): every host is resolved. An address in
  a range the organisation owns is admitted (the organisation's network bounds, ADR 0070, were judged on
  the name first). Any other private, carrier-grade, link-local or metadata address is a 422, and the
  metadata and link-local ranges are refused whatever is listed. A name that does not resolve is not
  judged here: the NetworkPolicy still decides at run time, as before.
- **At provision** (`admitted_targets`): the same resolution gives the exact addresses and ports the
  NetworkPolicy opens, and nothing else. The answer is the one at the moment of provisioning: a name whose
  address changes is followed at the next reconcile (an edit, an upgrade, a replayed install), not before.
- **At start-up** (`refuse_unbound_ranges`): a multi-organisation deployment whose listed CIDR no
  organisation owns does not start.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_adapters.private_egress import (
    IPNetwork,
    Resolver,
    admitted,
    classify,
    parse_cidrs,
    refuse_unbound,
    system_resolver,
)

from .. import edition
from ..config import Settings, get_settings
from ..db.models import Organization
from ..db.org_aliases import aliases_of
from ..errors import unprocessable

_RESOLVER: dict[str, Resolver] = {}


def use_resolver(resolver: Resolver | None) -> None:
    """Tests: what answers for a name (`fakes.resolver.FakeResolver`); `None` puts the system's back."""
    _RESOLVER.clear()
    if resolver is not None:
        _RESOLVER["current"] = resolver


def resolver() -> Resolver:
    return _RESOLVER.get("current", system_resolver)


def multi_organisation() -> bool:
    """The deployment hosts several organisations (the enterprise edition's `multi_org` feature)."""
    return "multi_org" in edition.features()


def refuse_unbound_ranges(settings: Settings | None = None) -> None:
    """At start-up: on a multi-organisation deployment, every listed CIDR is owned by an organisation.
    Raises `ValueError`, which stops the API."""
    settings = settings or get_settings()
    if settings.plugin_servers_egress_private_cidrs and multi_organisation():
        refuse_unbound(
            settings.plugin_servers_egress_private_cidrs, settings.plugin_servers_egress_private_cidrs_by_org
        )


async def ranges_of(session: AsyncSession, org_id: str, settings: Settings | None = None) -> list[IPNetwork]:
    """The private ranges the organisation may reach: all the listed ones on a single-organisation
    deployment, those it owns (by slug or alias) on a multi-organisation one. Off when nothing is listed."""
    settings = settings or get_settings()
    if not settings.plugin_servers_egress_private_cidrs:
        return []
    if not multi_organisation():
        return parse_cidrs(settings.plugin_servers_egress_private_cidrs, where="privateCidrs")
    organisation = await session.get(Organization, org_id)
    if organisation is None:
        return []
    names = {organisation.slug, *await aliases_of(session, org_id)}
    by_org = settings.plugin_servers_egress_private_cidrs_by_org
    owned = [c for name, cidrs in by_org.items() if name in names for c in cidrs]
    return parse_cidrs(owned, where="privateCidrsByOrg")


async def _answers(host: str) -> list[str]:
    try:
        return await resolver()(host)
    except OSError:
        return []


async def check_hosts(
    session: AsyncSession,
    org_id: str,
    hosts: Iterable[tuple[str, int]],
    purpose: str,
    settings: Settings | None = None,
) -> None:
    """At typing: 422 on a host that resolves to an address the egress rules would never open for this
    organisation (private, carrier-grade, link-local or metadata, unless listed for it)."""
    ranges = await ranges_of(session, org_id, settings)
    for host, _port in hosts:
        for address in await _answers(host):
            kind = classify(address)
            if kind == "public":
                continue
            if kind == "never":
                why = "a link-local, metadata or loopback address, closed whatever the deployment lists"
            elif admitted(address, ranges):
                continue
            else:
                why = (
                    "a private address: this deployment lists no range for this organisation that holds it"
                    if not ranges
                    else "a private address outside the ranges this organisation may reach"
                )
            raise unprocessable(
                f"{purpose}: `{host}` resolves to {address}, {why}",
                [{"loc": ["body", "config"], "msg": f"`{host}` -> {address}: {why}"}],
            )


async def admitted_targets(
    session: AsyncSession,
    org_id: str,
    hosts: Iterable[tuple[str, int]],
    settings: Settings | None = None,
) -> tuple[tuple[str, str, int], ...]:
    """At provision: `(host, address, port)` for each address of each host that sits in a range the
    organisation owns; what the NetworkPolicy opens beyond the public internet, exactly."""
    ranges = await ranges_of(session, org_id, settings)
    if not ranges:
        return ()
    found: set[tuple[str, str, int]] = set()
    for host, port in hosts:
        for address in await _answers(host):
            if admitted(address, ranges):
                found.add((host, address, port))
    return tuple(sorted(found))
