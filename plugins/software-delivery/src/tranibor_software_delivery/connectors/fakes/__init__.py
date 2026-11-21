# SPDX-License-Identifier: Apache-2.0
"""The scriptable fakes of the `ci` and `cd` slots (ADR 0005), and the demonstration `cd` (S21-24)."""

from __future__ import annotations

from .cd import FakeCd
from .ci import FakeCi
from .demo_cd import DemoCd, FakeDemoCd

__all__ = ["DemoCd", "FakeCd", "FakeCi", "FakeDemoCd"]
