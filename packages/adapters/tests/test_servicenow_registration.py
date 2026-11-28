"""The `servicenow` tracker type declares itself: spec, validation, egress, account, factory (ADR 0228)."""

from __future__ import annotations

from typing import Any

import pytest
from tranibor_adapters import account_of, build, connector_types, resolved_configuration, spec_of
from tranibor_adapters.builtins import _servicenow_tracker
from tranibor_adapters.errors import ConfigurationError
from tranibor_adapters.fakes import FakeTracker
from tranibor_adapters.tracker.servicenow import ServiceNowTracker
from tranibor_adapters.tracker.servicenow_api import ServiceNowClient


@pytest.fixture(autouse=True)
def _no_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Under TRANIBOR_FAKES a plain-http address is allowed, and `build` hands out the fake tracker."""
    monkeypatch.delenv("TRANIBOR_FAKES", raising=False)


GOOD: dict[str, Any] = {
    "instance_url": "https://acme.service-now.com",
    "client_id": "c-1",
    "client_secret": "s",
    "assignment_groups": ["Service Desk"],
}


def test_the_type_is_registered_with_its_secrets_declared() -> None:
    types = {(k, t): spec for k, t, spec in connector_types()}
    spec = types[("tracker", "servicenow")]
    assert spec.display == "ServiceNow" and spec.capabilities == ("tracker",)
    assert set(spec.secret_fields) == {"password", "client_secret", "webhook_secret"}
    assert not set(spec.secret_fields) & set(spec.config_schema["properties"])
    assert spec.config_schema["required"] == ["instance_url", "assignment_groups"]
    assert spec.config_schema["properties"]["auth"]["default"] == "oauth"


def test_a_secret_is_a_reference_resolved_when_the_adapter_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONNECTOR_SN_SECRET", "s3cr3t")
    config = resolved_configuration(
        "tracker", "servicenow", {"client_id": "c"}, {"client_secret": "env:CONNECTOR_SN_SECRET"}
    )
    assert config == {"client_id": "c", "client_secret": "s3cr3t"}


def test_the_factory_builds_an_oauth_tracker_and_a_basic_one() -> None:
    oauth = _servicenow_tracker({**GOOD, "tables": ["incident"], "webhook_secret": "w"})
    assert isinstance(oauth, ServiceNowTracker) and isinstance(oauth.client, ServiceNowClient)
    assert oauth.client.oauth is not None and oauth.client.oauth.client_id == "c-1"
    assert oauth.tables == ("incident",) and oauth.webhook_secret == "w"
    basic = _servicenow_tracker(
        {**GOOD, "auth": "basic", "username": "tranibor.integration", "password": "p"}
    )
    assert basic.client.oauth is None and basic.tables == ("incident", "sc_req_item", "change_request")
    assert isinstance(build("tracker", "servicenow", GOOD), ServiceNowTracker)


def test_under_fakes_the_platform_gets_the_fake_tracker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANIBOR_FAKES", "1")
    assert isinstance(build("tracker", "servicenow", GOOD), FakeTracker)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"instance_url": ""}, "instance_url"),
        ({"instance_url": "http://acme.service-now.com"}, "https"),
        ({"auth": "token"}, "auth"),
        ({"client_id": " "}, "client_id"),
        ({"auth": "basic"}, "username"),
        ({"assignment_groups": []}, "assignment_groups"),
        ({"assignment_groups": "Service Desk"}, "assignment_groups"),
        ({"assignment_groups": ["A,B"]}, "comma"),
        ({"assignment_groups": ["javascript:alert(1)"]}, "javascript"),
        ({"tables": ["problem"]}, "tables"),
        ({"tables": []}, "tables"),
    ],
)
def test_a_bad_configuration_is_refused_by_name(change: dict[str, Any], message: str) -> None:
    spec = spec_of("tracker", "servicenow")
    assert spec is not None and spec.check_config is not None
    with pytest.raises(ConfigurationError, match=message):
        spec.check_config({**GOOD, **change})
    with pytest.raises(ConfigurationError, match=message):
        _servicenow_tracker({**GOOD, **change})


def test_the_egress_is_the_instance_host() -> None:
    from tranibor_adapters.registry import egress_hosts

    assert egress_hosts("tracker", "servicenow", GOOD) == [("acme.service-now.com", 443)]


def test_the_account_is_the_integration_user_or_the_oauth_client() -> None:
    assert account_of("tracker", "servicenow", {"client_id": "c-1"}) == "c-1"
    assert account_of("tracker", "servicenow", {"auth": "basic", "username": "svc"}) == "svc"
    assert account_of("tracker", "servicenow", {"auth": "basic"}) is None
