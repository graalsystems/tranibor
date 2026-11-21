# SPDX-License-Identifier: Apache-2.0
"""The scriptable fakes of the `scm`, `ci` and `cd` slots (ADR 0005), the demonstration `cd` (S21-24) and
the scripted Azure DevOps server Azure Repos and Azure Pipelines are tested against."""

from __future__ import annotations

from .cd import FakeCd
from .ci import FakeCi
from .demo_cd import DemoCd, FakeDemoCd
from .scm import FakeScm

__all__ = ["DemoCd", "FakeCd", "FakeCi", "FakeDemoCd", "FakeScm"]
