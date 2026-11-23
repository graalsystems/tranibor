# SPDX-License-Identifier: Apache-2.0
"""An approval rule of a policy: who approves a step, and when (schemas/policy.schema.json).

Its own module because both the core's policy (`tranibor_contracts.policy`) and the software-delivery
plugin's policy section (`tranibor_contracts.delivery`, the approval of a merge) hold one."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .enums import Risk, Size
from .workflow import Strict


class ApprovalRule(Strict):
    required: Literal["always", "never", "by_size", "by_risk"]
    group: str | None = None
    sizes: list[Size] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    timeout_hours: int | None = Field(default=None, ge=1)


__all__ = ["ApprovalRule"]
