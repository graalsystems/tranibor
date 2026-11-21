# SPDX-License-Identifier: Apache-2.0
"""The plugin's content as the core ships it: the archive `plugins/offline/` locks (S85-10, S85-13).

The templates, agents, skills and evals are published from the platform's plugin repository; the core
runs its tests against the very bytes it ships offline, so a template that no longer works with this
commit's engine, job commands or schemas turns this suite red before a release carries it. Both stories
lock 0.2.1: 0.2.0, the first archive published (there is no 0.1.0 one), was refused at install and could
not compare, which these tests found (fixed by pull request 26 of the plugin repository).
"""

from __future__ import annotations

import atexit
import contextlib
from pathlib import Path

from tranibor_core.plugins import opened
from tranibor_core.plugins.sources import read_lock

OFFLINE = Path(__file__).resolve().parents[3] / "plugins" / "offline"
NAME = "procurement"

_stack = contextlib.ExitStack()
atexit.register(_stack.close)


def _root() -> Path:
    lock = read_lock(OFFLINE)
    assert lock is not None and NAME in lock, f"{NAME} is not in {OFFLINE}/plugins.lock"
    version, digest = lock[NAME]
    plugin = _stack.enter_context(opened(OFFLINE / f"{NAME}-{version}.tar.gz"))
    assert plugin.digest == digest, "the archive is the one the lock names"
    return plugin.root


#: The unpacked content of the locked archive, valid for the whole session.
ROOT = _root()
SOURCING = ROOT / "templates" / "procurement-sourcing"
OPERATIONS = ROOT / "templates" / "procurement-operations"
OFFER_COMPARISON = SOURCING / "workflows" / "offer-comparison.yaml"
SUPPLIER_ONBOARDING = OPERATIONS / "workflows" / "supplier-onboarding.yaml"
CONTRACT_RENEWALS = OPERATIONS / "workflows" / "contract-renewals.yaml"
