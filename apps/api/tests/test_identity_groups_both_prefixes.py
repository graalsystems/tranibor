"""ADR 0226 (R7): an identity provider's group is `tranibor:<org>:<group>`, and the former
`choregos:<org>:<group>` is still read for the window."""

from __future__ import annotations

import pytest
from tranibor_api.routers.auth import roles_from_groups
from tranibor_api.services.human_groups import groups_for_org
from tranibor_contracts import Role


@pytest.mark.parametrize("prefix", ["tranibor", "choregos"])
def test_a_namespaced_group_gives_its_role_under_either_prefix(prefix: str) -> None:
    roles = roles_from_groups([f"{prefix}:acme:org-admins", f"{prefix}:other:viewers"], "default")
    assert roles == {"acme": Role.ORG_ADMIN, "other": Role.VIEWER}


def test_the_highest_role_wins_whichever_prefix_names_it() -> None:
    roles = roles_from_groups(["choregos:acme:viewers", "tranibor:acme:org-admins"], "default")
    assert roles == {"acme": Role.ORG_ADMIN}


def test_a_group_with_another_word_before_it_is_bare_and_gives_nothing() -> None:
    assert roles_from_groups(["vendor:acme:org-admins"], "default") == {}


@pytest.mark.parametrize("prefix", ["tranibor", "choregos"])
def test_a_namespaced_group_counts_for_its_organisation_under_both_names(prefix: str) -> None:
    carried = groups_for_org([f"{prefix}:acme:architects", f"{prefix}:other:auditors"], "acme")
    assert "architects" in carried
    # An approval rule written with either prefix matches the person.
    assert {"tranibor:acme:architects", "choregos:acme:architects"} <= carried
    assert "auditors" not in carried, "another organisation's group is not this one's"
