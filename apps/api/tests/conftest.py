"""API fixtures: ephemeral database, application, identities, test project.

They are the test kit's (`tranibor_testkit.api`, ADR 0181 round D), shared with the first-party plugins'
API tests; this suite adds its own kits' fixtures.
"""

from __future__ import annotations

from tranibor_testkit.api import *  # noqa: F403 - the suite's fixtures, and the names its tests import
from tranibor_testkit.api import SUITE_ON_POSTGRES, login  # noqa: F401 - imported by `from .conftest`

# Shared fixtures of the kits: every test module of the directory can use them by name.
from .kits.identity_seams import extended_client, identity_extension  # noqa: F401
