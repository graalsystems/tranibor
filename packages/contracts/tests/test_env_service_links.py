"""The R4 fallback (ADR 0176) leaves Kubernetes service links alone (#2057, #2084).

A Service named `choregos-pg` gives every pod of its namespace that keeps service links
`CHOREGOS_PG_SERVICE_HOST`, `CHOREGOS_PG_PORT=tcp://...` and the like. They are not old settings of the
platform: no warning for them, and no `TRANIBOR_PG_*` made from them. A real setting that ends in
`_PORT` still moves.
"""

from __future__ import annotations

import logging

import pytest
from tranibor_contracts.env import adopt_legacy_environment, adopted, is_service_link

#: What the kubelet wrote in an API pod of 0.27.17 for the `choregos-pg` and `choregos-api` Services.
LINKS = {
    "CHOREGOS_PG_SERVICE_HOST": "10.43.12.7",  # compat-ok
    "CHOREGOS_PG_SERVICE_PORT": "5432",  # compat-ok
    "CHOREGOS_PG_SERVICE_PORT_POSTGRES": "5432",  # compat-ok
    "CHOREGOS_PG_PORT": "tcp://10.43.12.7:5432",  # compat-ok
    "CHOREGOS_PG_PORT_5432_TCP": "tcp://10.43.12.7:5432",  # compat-ok
    "CHOREGOS_PG_PORT_5432_TCP_PROTO": "tcp",  # compat-ok
    "CHOREGOS_PG_PORT_5432_TCP_PORT": "5432",  # compat-ok
    "CHOREGOS_API_PORT_8000_TCP_ADDR": "10.43.0.4",  # compat-ok
    "CHOREGOS_EGRESS_PORT": "tcp://10.43.0.9:3128",  # compat-ok
}


@pytest.mark.parametrize(("name", "value"), sorted(LINKS.items()))
def test_each_variable_of_a_service_link_has_its_shape(name: str, value: str) -> None:
    assert is_service_link(name, value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHOREGOS_EGRESS_PORT", "3128"),  # compat-ok  (the egress proxy's own setting)
        ("CHOREGOS_DEMO_PORT", "8090"),  # compat-ok
        ("CHOREGOS_SANDBOX_ASSIGNMENT_PORT", "8765"),  # compat-ok
        ("CHOREGOS_API_URL", "http://choregos-api:8000"),  # compat-ok
    ],
)
def test_a_setting_that_ends_in_port_is_not_a_service_link(name: str, value: str) -> None:
    assert not is_service_link(name, value)


def test_a_service_link_is_neither_adopted_nor_reported(caplog: pytest.LogCaptureFixture) -> None:
    environ = dict(LINKS)
    with caplog.at_level(logging.WARNING, logger="tranibor.env"):
        touched = adopt_legacy_environment(environ, dotenv={})
    assert touched == []
    assert environ == LINKS, "no TRANIBOR_* is made from a service link"
    assert caplog.records == []
    assert adopted(LINKS) == LINKS


def test_a_real_old_setting_beside_service_links_still_moves(caplog: pytest.LogCaptureFixture) -> None:
    environ = {**LINKS, "CHOREGOS_S2084_EGRESS_PORT": "3128"}  # compat-ok
    with caplog.at_level(logging.WARNING, logger="tranibor.env"):
        touched = adopt_legacy_environment(environ, dotenv={})
    assert touched == ["TRANIBOR_S2084_EGRESS_PORT"]
    assert environ["TRANIBOR_S2084_EGRESS_PORT"] == "3128"
    assert [r.getMessage() for r in caplog.records] == [
        "CHOREGOS_S2084_EGRESS_PORT is deprecated: set TRANIBOR_S2084_EGRESS_PORT instead "  # compat-ok
        "(its value is used as it is)"
    ]
