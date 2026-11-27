"""A name table for the plugin servers' private egress, in place of DNS.

Shared by the private egress tests (S86-06) and the DNS policy tests (#2277): what two test modules share
lives in `kits/` (S45-42).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from tranibor_adapters.fakes.resolver import FakeResolver


@pytest.fixture
def dns() -> Iterator[FakeResolver]:
    """No DNS in a test: names answer from a table."""
    from tranibor_api.services.private_egress import use_resolver

    resolver = FakeResolver(
        {
            "controller.lan.example": ["10.20.1.5"],
            "elsewhere.lan.example": ["10.30.1.5"],
            "metadata.lan.example": ["169.254.169.254"],
            "v6metadata.lan.example": ["fd00:ec2::254"],
            "mixed.lan.example": ["10.20.1.5", "169.254.169.254"],
            "api.vendor.example": ["93.184.216.34"],
        }
    )
    use_resolver(resolver)
    yield resolver
    use_resolver(None)
