"""The CLI reads the change a work item proposes from its `change_request` link alone (ADR 0181 C1)."""

from __future__ import annotations

from tranibor_cli.neutral_names import change_request_of

PR = "https://github.com/northwind/billing-api/pull/7"


def test_the_change_request_is_the_link_and_the_former_field_is_not_read() -> None:
    assert change_request_of({"links": [{"rel": "change_request", "url": PR}]}) == PR
    assert change_request_of({"links": [{"rel": "other", "url": "x"}], "pr_url": PR}) is None
    assert change_request_of({"pr_url": PR}) is None
    assert change_request_of({"links": []}) is None
