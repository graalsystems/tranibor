"""What production is, and the fakes it refuses (S80-03)."""

from __future__ import annotations

import pytest
from tranibor_core.production import (
    PERSONAL_ENVIRONMENT,
    PRODUCTION_ENVIRONMENTS,
    FakesInProductionError,
    fakes_on,
    is_personal,
    is_production,
    refuse_fakes_in_production,
    refuse_fakes_in_this_environment,
)


def test_production_is_staging_prod_and_a_personal_installation() -> None:
    """The values that refuse a superuser, the development sign-in and `verify_tls: false` everywhere."""
    assert frozenset({"staging", "prod", "personal"}) == PRODUCTION_ENVIRONMENTS


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " y ", "t", True])
def test_the_fakes_are_on_as_the_settings_read_them(value: str | bool) -> None:
    assert fakes_on(value)


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", None, False])
def test_and_off_otherwise(value: str | bool | None) -> None:
    assert not fakes_on(value)


@pytest.mark.parametrize("environment", ["staging", "prod", " PROD "])
def test_the_fakes_are_refused_in_production_naming_both_settings(environment: str) -> None:
    with pytest.raises(FakesInProductionError) as refused:
        refuse_fakes_in_production(environment=environment, fakes="1", process="thing")
    message = str(refused.value)
    assert "TRANIBOR_FAKES" in message and f"TRANIBOR_ENV={environment.strip()}" in message
    assert "the thing refuses to start" in message and "global.fakes" in message


@pytest.mark.parametrize(("environment", "fakes"), [("dev", "1"), ("test", True), (None, "1"), ("prod", "0")])
def test_a_bench_or_production_without_fakes_passes(environment: str | None, fakes: str | bool) -> None:
    refuse_fakes_in_production(environment=environment, fakes=fakes, process="thing")


def test_read_from_a_process_environment() -> None:
    with pytest.raises(FakesInProductionError):
        refuse_fakes_in_this_environment("thing", {"TRANIBOR_ENV": "prod", "TRANIBOR_FAKES": "yes"})
    refuse_fakes_in_this_environment("thing", {"TRANIBOR_FAKES": "yes"})


def test_a_personal_installation_is_production() -> None:
    """ADR 0228: `personal` is production, so every refusal of staging and prod reaches it."""
    assert PERSONAL_ENVIRONMENT in PRODUCTION_ENVIRONMENTS
    assert is_production("personal") and is_production(" Personal ")
    assert is_personal("personal") and not is_personal("prod") and not is_personal(None)


def test_the_fakes_are_refused_in_a_personal_installation() -> None:
    with pytest.raises(FakesInProductionError) as refused:
        refuse_fakes_in_production(environment="personal", fakes="1", process="API")
    assert "TRANIBOR_ENV=personal" in str(refused.value)
