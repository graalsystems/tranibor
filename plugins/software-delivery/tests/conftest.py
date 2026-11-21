# SPDX-License-Identifier: Apache-2.0
"""The extension is registered as the API and the worker register it: through its entry point."""

from __future__ import annotations

import os

from tranibor_adapters import load_extensions

# The egress allowlist (S39-22) only warns in the connector types' suites, as in the adapters' own: the
# tests that prove the guard ask for `enforce` themselves.
os.environ.setdefault("TRANIBOR_EGRESS_MODE", "warn")

load_extensions()
