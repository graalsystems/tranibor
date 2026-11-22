# SPDX-License-Identifier: Apache-2.0
"""The adapters' tests' name for the scripted transport, which is shipped with the fakes
(`tranibor_adapters.fakes.wire`) so that an extension's tests use the same one."""

from __future__ import annotations

from tranibor_adapters.fakes.wire import Reply, Wire, handler, no_wait

__all__ = ["Reply", "Wire", "handler", "no_wait"]
