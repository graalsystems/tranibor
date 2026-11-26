# SPDX-License-Identifier: Apache-2.0
"""The policy `sandbox.personal_runner` (S87-08, ADR 0226 §9, ADR 0227 §2): an addition only.

A policy that does not write it reads the platform's defaults (`never`, `[gateway]`, `refused`), so
every policy stored before it means what it meant; the personal posture's own defaults (`required`,
`[subscription]`, ADR 0228 §8) are applied by placement, not by the contract. Placement
(`activities/placement.py`, S87-18) reads it; here, the schema and the model say the same thing.
"""

from __future__ import annotations

import copy
import json
from typing import Any, get_args

import pytest
import tranibor_contracts as contracts
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from tranibor_contracts import Policy
from tranibor_contracts.policy import (
    CredentialSource,
    PersonalRunnerPlacement,
    PersonalRunnerPolicy,
    SandboxPolicy,
)

SCHEMA = contracts.load_schema("policy.schema.json")
NODE: dict[str, Any] = SCHEMA["properties"]["sandbox"]["properties"]["personal_runner"]
SOLO: dict[str, Any] = json.loads(
    (contracts.schemas_dir() / "examples" / "policy.solo.json").read_text(encoding="utf-8")
)


def _with(section: dict[str, Any]) -> dict[str, Any]:
    policy = copy.deepcopy(SOLO)
    policy.setdefault("sandbox", {})["personal_runner"] = section
    return policy


def _both(section: dict[str, Any]) -> tuple[bool, bool]:
    """(the schema accepts the policy holding it, the model accepts it)."""
    policy = _with(section)
    try:
        Policy.model_validate(policy)
    except ValidationError:
        model = False
    else:
        model = True
    return not list(Draft202012Validator(SCHEMA).iter_errors(policy)), model


def test_the_section_is_declared_in_the_schema_and_the_model_with_the_same_fields() -> None:
    assert "personal_runner" in SCHEMA["properties"]["sandbox"]["properties"]
    assert "personal_runner" in SandboxPolicy.model_fields
    assert (
        set(NODE["properties"])
        == set(PersonalRunnerPolicy.model_fields)
        == {
            "placement",
            "prefer_wait_minutes",
            "credential",
            "native",
        }
    )
    assert NODE["additionalProperties"] is False


def test_a_policy_that_does_not_write_it_reads_the_platforms_defaults() -> None:
    """Additions only (ADR 0168): a policy stored before the section means what it meant."""
    assert "personal_runner" not in SOLO.get("sandbox", {})
    assert not list(Draft202012Validator(SCHEMA).iter_errors(SOLO))
    section = Policy.model_validate(SOLO).sandbox.personal_runner
    assert section == PersonalRunnerPolicy()
    assert (section.placement, section.credential, section.native) == ("never", ["gateway"], "refused")
    assert section.prefer_wait_minutes == 10


def test_the_schema_and_the_model_have_the_same_defaults_and_choices() -> None:
    defaults = PersonalRunnerPolicy()
    for name in ("placement", "prefer_wait_minutes", "credential", "native"):
        assert NODE["properties"][name]["default"] == getattr(defaults, name), name
    assert NODE["properties"]["placement"]["enum"] == list(get_args(PersonalRunnerPlacement))
    assert NODE["properties"]["credential"]["items"]["enum"] == list(get_args(CredentialSource))
    assert NODE["properties"]["native"]["enum"] == ["refused", "accepted"]


@pytest.mark.parametrize(
    ("what", "section", "accepted"),
    [
        ("empty", {}, True),
        ("preferred", {"placement": "preferred", "prefer_wait_minutes": 1}, True),
        ("required with a subscription", {"placement": "required", "credential": ["subscription"]}, True),
        ("both credentials", {"credential": ["gateway", "subscription"]}, True),
        ("native accepted", {"native": "accepted"}, True),
        ("the longest wait", {"prefer_wait_minutes": 120}, True),
        ("no wait", {"prefer_wait_minutes": 0}, False),
        ("a wait over two hours", {"prefer_wait_minutes": 121}, False),
        ("a placement it does not know", {"placement": "always"}, False),
        ("a credential it does not know", {"credential": ["api_key"]}, False),
        ("no credential at all", {"credential": []}, False),
        ("a credential twice", {"credential": ["gateway", "gateway"]}, False),
        ("native as a boolean", {"native": True}, False),
        ("a field it does not describe", {"runner": "laptop"}, False),
    ],
)
def test_the_schema_and_the_model_accept_and_refuse_the_same_sections(
    what: str, section: dict[str, Any], accepted: bool
) -> None:
    assert _both(section) == (accepted, accepted), what


def test_the_section_writes_back_what_the_schema_accepts() -> None:
    policy = Policy.model_validate(_with({"placement": "required", "credential": ["subscription"]}))
    written = policy.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert written["sandbox"]["personal_runner"] == {
        "placement": "required",
        "prefer_wait_minutes": 10,
        "credential": ["subscription"],
        "native": "refused",
    }
    assert not list(Draft202012Validator(SCHEMA).iter_errors(written))
