# SPDX-License-Identifier: Apache-2.0
"""The plugins' content as the core ships it: the archives `plugins/offline/` locks (S85-10, S85-13).

The templates, agents, skills and evals are published from the platform's plugin repository; the core
runs its tests against the very bytes it ships offline, so a template that no longer works with this
commit's engine, job commands or schemas turns this suite red before a release carries it. The trade is
two plugins since 0.3.0 (#2134, pull request 27 of the plugin repository): `procurement` (the comparison of
offers, no server: S85-10) and `procurement-operations` (onboarding, requests, renewals and the `purchasing`
server: S85-13). 0.2.0, the first archive published, was refused at install and could not compare, which
these tests found (fixed in 0.2.1).
"""

from __future__ import annotations

import atexit
import contextlib
from pathlib import Path

from tranibor_core.plugins import opened
from tranibor_core.plugins.sources import read_lock

OFFLINE = Path(__file__).resolve().parents[3] / "plugins" / "offline"
NAME = "procurement"
OPERATIONS_NAME = "procurement-operations"

_stack = contextlib.ExitStack()
atexit.register(_stack.close)


def _root(name: str) -> Path:
    lock = read_lock(OFFLINE)
    assert lock is not None and name in lock, f"{name} is not in {OFFLINE}/plugins.lock"
    version, digest = lock[name]
    plugin = _stack.enter_context(opened(OFFLINE / f"{name}-{version}.tar.gz"))
    assert plugin.digest == digest, "the archive is the one the lock names"
    return plugin.root


#: The unpacked content of each locked archive, valid for the whole session.
ROOT = _root(NAME)
OPERATIONS_ROOT = _root(OPERATIONS_NAME)
SOURCING = ROOT / "templates" / "procurement-sourcing"
OPERATIONS = OPERATIONS_ROOT / "templates" / "procurement-operations"
OFFER_COMPARISON = SOURCING / "workflows" / "offer-comparison.yaml"
SUPPLIER_ONBOARDING = OPERATIONS / "workflows" / "supplier-onboarding.yaml"
CONTRACT_RENEWALS = OPERATIONS / "workflows" / "contract-renewals.yaml"
