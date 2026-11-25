# SPDX-License-Identifier: Apache-2.0
"""The stage roles of software delivery and their playbooks (ADR 0181, S83-12).

`fix_ci` repairs a red pipeline, `address_review` answers a review, `release_notes` writes the release
notes, `verify_prod` checks production. Their playbooks are this directory's `<role>/prompt.md`, written
like the core's (they include the core's `_base.md`); the extension adds the directory to the playbooks'
search path and declares each role in the contracts' open list `STAGE_ROLES`, so a workflow naming one
finds its playbook where the plugin is installed. Without it, the validator warns that the role has no
playbook (`role.playbook_not_found`), as for any role nobody brings, and a stored workflow still parses:
a role is a string of the workflow's pattern.
"""

from __future__ import annotations

from pathlib import Path

from tranibor_core.stage_roles import declare_stage_roles

from ..names import NAME, DeliveryRole

#: The directory of the plugin's playbooks.
ROLES_DIR = Path(__file__).parent


def declare_roles() -> list[str]:
    """Declare the plugin's roles and their playbooks. Idempotent."""
    declared = declare_stage_roles(ROLES_DIR, plugin=NAME)
    if set(declared) != {role.value for role in DeliveryRole}:
        raise RuntimeError(f"the playbooks of {ROLES_DIR} are not the plugin's roles: {sorted(declared)}")
    return declared
