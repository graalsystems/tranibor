# SPDX-License-Identifier: Apache-2.0
"""Private ranges a plugin server may reach, named by the operator (ADR 0224 §6a, amends ADR 0096 §8).

A plugin server's egress is the internet: its NetworkPolicy excepts the private, link-local and
carrier-grade ranges (`plugin_servers.PRIVATE_RANGES`, plus the IPv6 ones here). A system a plugin must
reach (a LAN controller, an on-premises directory) sits in exactly those ranges. A deployment may
therefore list CIDRs (`global.plugins.servers.privateCidrs`), and, on a multi-organisation
deployment, bind each to one organisation (`privateCidrsByOrg`). This module holds what has no state:

- `parse_cidrs` and `check_configuration`: the lists are networks, none overlaps a range that stays
  closed whatever is listed (`NEVER_RANGES`: the metadata and link-local ranges, loopback, the
  unspecified and multicast ranges), and a CIDR is bound to one organisation at most;
- `refuse_unbound`: on a multi-organisation deployment, a listed CIDR no organisation owns is refused;
- `classify`: what an address is for the egress rules (`never`, `private`, `public`);
- `admitted`: whether the ranges an organisation owns cover an address.

Which addresses a name resolves to is `system_resolver`'s (tests use `fakes.resolver.FakeResolver`).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Literal

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: Ranges the default NetworkPolicy excepts, and that a listed CIDR may open (the metadata endpoint's
#: and the link-local ranges excluded, see `NEVER_RANGES`): the cluster's, the node's, a carrier's.
PRIVATE_NETWORKS = (
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "100.64.0.0/10",
    "fc00::/7",
    "fe80::/10",
)
#: Ranges no list opens: the cloud metadata endpoints (169.254.169.254, fd00:ec2::254, 100.100.100.200,
#: 192.0.0.192), every link-local address, loopback, the unspecified address and multicast.
NEVER_RANGES = (
    "169.254.0.0/16",
    "fe80::/10",
    "fd00:ec2::/32",
    "100.100.100.200/32",
    "192.0.0.192/32",
    "127.0.0.0/8",
    "::1/128",
    "0.0.0.0/8",
    "::/128",
    "224.0.0.0/4",
    "ff00::/8",
)
_NEVER = tuple(ipaddress.ip_network(n) for n in NEVER_RANGES)
_PRIVATE = tuple(ipaddress.ip_network(n) for n in PRIVATE_NETWORKS)

Resolver = Callable[[str], Awaitable[list[str]]]
Kind = Literal["never", "private", "public"]


def _overlap(a: IPNetwork, b: IPNetwork) -> bool:
    return a.version == b.version and a.overlaps(b)


def parse_cidrs(entries: Iterable[str], *, where: str) -> list[IPNetwork]:
    """Each entry as a network. A host bit set (`10.0.0.5/24`) is refused: it would silently widen."""
    found: list[IPNetwork] = []
    for entry in entries:
        try:
            network = ipaddress.ip_network(str(entry).strip(), strict=True)
        except ValueError as bad:
            raise ValueError(f"{where}: `{entry}` is not a network ({bad})") from None
        closed = next((n for n in _NEVER if _overlap(network, n)), None)
        if closed is not None:
            raise ValueError(
                f"{where}: `{entry}` overlaps {closed}, which stays closed whatever is listed "
                "(metadata, link-local, loopback, multicast)"
            )
        if not any(network.version == p.version and network.subnet_of(p) for p in _PRIVATE):  # type: ignore[arg-type]
            raise ValueError(
                f"{where}: `{entry}` is not inside a private or carrier-grade range "
                f"({', '.join(PRIVATE_NETWORKS)}): public addresses are already open"
            )
        found.append(network)
    return found


def check_configuration(private: Iterable[str], by_org: Mapping[str, Iterable[str]]) -> None:
    """The lists as written: well-formed, outside the closed ranges, `privateCidrsByOrg` inside
    `privateCidrs`, no CIDR bound to two organisations. Raises `ValueError`."""
    listed = parse_cidrs(private, where="global.plugins.servers.privateCidrs")
    owner: dict[IPNetwork, str] = {}
    for org, cidrs in by_org.items():
        for network in parse_cidrs(cidrs, where=f"global.plugins.servers.privateCidrsByOrg[{org}]"):
            if not any(network.version == n.version and network.subnet_of(n) for n in listed):  # type: ignore[arg-type]
                raise ValueError(
                    f"global.plugins.servers.privateCidrsByOrg[{org}]: `{network}` is not inside "
                    "`privateCidrs`: list it there too (it opens nothing otherwise)"
                )
            for other, holder in owner.items():
                if holder != org and _overlap(network, other):
                    raise ValueError(
                        f"global.plugins.servers.privateCidrsByOrg: `{network}` ({org}) overlaps "
                        f"`{other}` ({holder}): a range belongs to one organisation"
                    )
            owner[network] = org


def refuse_unbound(private: Iterable[str], by_org: Mapping[str, Iterable[str]]) -> None:
    """On a multi-organisation deployment: a listed CIDR that no organisation owns is refused, since it
    would be open to every tenant. Raises `ValueError`."""
    check_configuration(private, by_org)
    bound = [n for cidrs in by_org.values() for n in parse_cidrs(cidrs, where="privateCidrsByOrg")]
    for network in parse_cidrs(private, where="privateCidrs"):
        if not any(network.version == b.version and network.subnet_of(b) for b in bound):  # type: ignore[arg-type]
            raise ValueError(
                f"global.plugins.servers.privateCidrs: `{network}` is not bound to an organisation in "
                "`privateCidrsByOrg` and this deployment hosts several organisations: a private range "
                "is open to one tenant, never to all"
            )


def classify(address: str) -> Kind:
    """`never` (a range no list opens), `private` (what the default policy excepts), else `public`.
    An IPv4-mapped IPv6 address is judged as the IPv4 address it carries."""
    ip: IPAddress = ipaddress.ip_address(address.strip().strip("[]"))
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if any(ip.version == n.version and ip in n for n in _NEVER):
        return "never"
    if any(ip.version == n.version and ip in n for n in _PRIVATE):
        return "private"
    return "public"


def admitted(address: str, ranges: Iterable[IPNetwork]) -> bool:
    """Whether `address` is private, not closed whatever is listed, and inside one of `ranges`."""
    if classify(address) != "private":
        return False
    ip: IPAddress = ipaddress.ip_address(address.strip().strip("[]"))
    return any(ip.version == n.version and ip in n for n in ranges)


def is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


async def system_resolver(host: str, *, timeout_s: float = 3.0) -> list[str]:
    """The addresses `host` resolves to: an IP literal is itself. A name that does not resolve, or does
    not within `timeout_s`, gives none (the NetworkPolicy still decides at run time)."""
    if is_literal(host):
        return [host.strip("[]")]
    try:
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout_s
        )
    except (OSError, TimeoutError):
        return []
    return sorted({str(info[4][0]).split("%")[0] for info in infos})
