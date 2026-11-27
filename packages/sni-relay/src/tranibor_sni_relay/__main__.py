# SPDX-License-Identifier: Apache-2.0
"""`python -m tranibor_sni_relay`: the relay's command, as `choregos-relay` (`server.main`)."""

from __future__ import annotations

from .server import main

raise SystemExit(main())
