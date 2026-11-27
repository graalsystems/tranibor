# SPDX-License-Identifier: Apache-2.0
"""What the console shows of a profile (ADR 0224, S86-05): the profiles the installed plugins declare, each
with the servers that implement it and the instances the organisation runs of those servers.

Read only, and from what is installed: the profile documents the installed versions ship, the servers'
`implements` as installed. Nothing here judges a server against a profile (the installer does, S86-02)."""

from __future__ import annotations

from typing import Any

import yaml
from sqlalchemy.ext.asyncio import AsyncSession
from tranibor_core.plugins.profiles import profile_name

from . import plugin_installer as installer
from . import plugin_servers
from .plugin_files import plugin_text


async def profiles_of(session: AsyncSession, org_id: str) -> list[dict[str, Any]]:
    """The installed plugins' profiles in `<plugin>/<name>@<version>` order, each with its implementations."""
    rows = await installer.installed(session, org_id)
    found: list[dict[str, Any]] = []
    for row in rows:
        for ref in (row.manifest.get("contents") or {}).get("profiles") or []:
            text = await plugin_text(session, org_id, row.name, str(ref))
            document = yaml.safe_load(text) if text else None
            if not (isinstance(document, dict) and "name" in document and "version" in document):
                continue
            full = profile_name(row.name, document)
            found.append(
                {
                    "name": str(document["name"]),
                    "plugin": row.name,
                    "profile": full,
                    "version": int(document["version"]),
                    "description": str(document.get("description") or ""),
                    "tools": [
                        {
                            "name": str(t["name"]),
                            "access": str(t.get("access") or "read"),
                            "description": t.get("description"),
                        }
                        for t in document.get("tools") or []
                        if isinstance(t, dict) and "name" in t
                    ],
                    "implementations": await _implementations(session, org_id, rows, full),
                }
            )
    return sorted(found, key=lambda p: p["profile"])


async def _implementations(
    session: AsyncSession, org_id: str, rows: list[Any], profile: str
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        for server, doc in sorted((row.details.get("servers") or {}).items()):
            if isinstance(doc, dict) and profile in (doc.get("implements") or []):
                instances = await plugin_servers.instances_of(session, org_id, row.name, server)
                out.append({"plugin": row.name, "server": server, "instances": instances})
    return out
