"""An organisation reads its spend in any ISO 4217 currency, at the rate it declares (ADR 0214).

The ledger stays as measured: the USD and EUR figures never move, `cost_display` is the extra column,
and every surface that shows it says it is converted at the organisation's declared rate."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from tranibor_api.display_currency import ISO_4217, read

from .kits.cost_ledger import seed_cost_ledger

ORG = "/api/v1/orgs/northwind"


async def _declare(client: AsyncClient, code: str = "MXN", rate: float = 17.5) -> dict[str, Any]:
    response = await client.patch(ORG, json={"currency": code, "fx_rate": rate})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def test_an_organisation_in_pesos_gets_converted_amounts_and_the_usd_ones_unchanged(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await seed_cost_ledger(project["id"])
    before = (await client.get(f"{ORG}/costs", params={"group_by": "agent"})).json()
    declared = await _declare(client)
    assert (declared["currency"], declared["fx_rate"]) == ("MXN", 17.5)
    assert declared["fx_rate_set_on"] == datetime.now(UTC).date().isoformat()

    report = (await client.get(f"{ORG}/costs", params={"group_by": "agent"})).json()
    assert report["currency"] == "MXN"
    assert report["fx"] == {"declared": True, "rate": 17.5, "set_on": declared["fx_rate_set_on"]}
    assert {r["key"]: r["cost_display"] for r in report["rows"]} == {"coordinator": 87.5, "reviewer": 35.0}
    assert report["total"]["cost_display"] == 122.5
    # the ledger as measured: nothing but the added column moved
    for line_before, line_after in zip(
        [*before["rows"], before["total"]], [*report["rows"], report["total"]], strict=True
    ):
        assert {k: v for k, v in line_after.items() if k != "cost_display"} == {
            k: v for k, v in line_before.items() if k != "cost_display"
        }
    assert report["total"]["cost_usd"] == 7.0 and report["total"]["cost_eur"] == 3.5

    project_report = (await client.get(f"/api/v1/projects/{project['id']}/costs")).json()
    assert project_report["currency"] == "MXN" and project_report["total"]["cost_display"] == 122.5
    assert any(
        o["currency"] == "MXN" and o["fx_rate"] == 17.5
        for o in (await client.get("/api/v1/orgs")).json()["items"]
    )


async def test_the_csv_carries_the_converted_column_and_says_how_it_was_converted(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await seed_cost_ledger(project["id"])
    declared = await _declare(client)
    text = (await client.get(f"{ORG}/costs.csv", params={"group_by": "agent"})).text.lstrip("﻿")
    rows = list(csv.reader(io.StringIO(text), delimiter=";"))
    header = rows[0]
    assert header[:3] == ["agent", "cout_usd", "cout_eur"] and header[-1] == "cost_mxn"
    coordinator = next(r for r in rows if r and r[0] == "coordinator")
    assert coordinator[1] == "5.000000" and coordinator[-1] == "87.500000"
    assert ["declared_rate", "1 USD = 17.5 MXN", declared["fx_rate_set_on"]] in rows
    assert any(r and r[0] == "note" and "declared" in r[1] for r in rows)


async def test_a_ledger_currency_has_no_converted_note(client: AsyncClient, project: dict[str, Any]) -> None:
    await seed_cost_ledger(project["id"])
    report = (await client.get(f"{ORG}/costs")).json()
    assert report["currency"] == "EUR" and report["fx"] == {"declared": False, "rate": None, "set_on": None}
    assert report["total"]["cost_display"] == report["total"]["cost_eur"] == 3.5
    await client.patch(ORG, json={"currency": "USD"})
    usd = (await client.get(f"{ORG}/costs")).json()
    assert usd["total"]["cost_display"] == usd["total"]["cost_usd"] == 7.0 and not usd["fx"]["declared"]
    csv_text = (await client.get(f"{ORG}/costs.csv")).text
    assert "cost_usd" not in csv_text.splitlines()[0] and "declared_rate" not in csv_text


@pytest.mark.parametrize("code", ["XYZ", "ZZZ", "mxn", "MEXICAN", "", "M1N"])
async def test_an_unknown_code_is_refused(client: AsyncClient, admin: str, code: str) -> None:
    assert (await client.patch(ORG, json={"currency": code, "fx_rate": 2.0})).status_code == 422
    assert (await client.get("/api/v1/orgs")).json()["items"][0]["currency"] == "EUR"


async def test_a_currency_with_no_declared_rate_is_refused_not_guessed(
    client: AsyncClient, admin: str
) -> None:
    refused = await client.patch(ORG, json={"currency": "MXN"})
    assert refused.status_code == 422 and "no default rate" in refused.json()["detail"]
    current = (await client.get("/api/v1/orgs")).json()["items"][0]
    assert (current["currency"], current["fx_rate"]) == ("EUR", None), "the refusal changed nothing"
    # nor does a rate that is not a positive number pass
    for rate in (0, -3, "abc"):
        assert (await client.patch(ORG, json={"currency": "MXN", "fx_rate": rate})).status_code == 422


async def test_a_rate_is_only_declared_for_a_currency_the_ledger_does_not_hold(
    client: AsyncClient, admin: str
) -> None:
    assert (await client.patch(ORG, json={"currency": "EUR", "fx_rate": 0.9})).status_code == 422
    assert (await client.patch(ORG, json={"currency": "USD", "fx_rate": 1.0})).status_code == 422
    assert (await client.patch(ORG, json={"fx_rate": 17.5})).status_code == 422, "EUR takes no rate either"


async def test_a_declared_rate_is_never_carried_to_another_currency(client: AsyncClient, admin: str) -> None:
    await _declare(client, "MXN", 17.5)
    assert (await client.patch(ORG, json={"currency": "COP"})).status_code == 422
    assert (await client.patch(ORG, json={"name": "Northwind"})).json()["fx_rate"] == 17.5
    redeclared = await _declare(client, "MXN", 18.25)
    assert redeclared["fx_rate"] == 18.25
    cop = await _declare(client, "COP", 4100.0)
    assert (cop["currency"], cop["fx_rate"]) == ("COP", 4100.0)
    eur = (await client.patch(ORG, json={"currency": "EUR"})).json()
    assert (eur["currency"], eur["fx_rate"], eur["fx_rate_set_on"]) == ("EUR", None, None)


async def test_a_rate_alone_updates_the_declared_one(client: AsyncClient, admin: str) -> None:
    await _declare(client, "MXN", 17.5)
    changed = await client.patch(ORG, json={"fx_rate": 19.0})
    assert changed.status_code == 200 and changed.json()["fx_rate"] == 19.0


async def test_budgets_stay_in_usd_whatever_the_display_currency(
    client: AsyncClient, project: dict[str, Any]
) -> None:
    await _declare(client)
    total = (await client.get(f"{ORG}/costs")).json()["total"]
    assert total["budget_usd"] == 150.0, "the ceiling is set and enforced in USD, never converted"


def test_the_table_of_codes_holds_the_currencies_a_subsidiary_asks_for() -> None:
    assert {"MXN", "COP", "BRL", "CLP", "PEN", "ARS", "EUR", "USD", "GBP", "JPY"} <= ISO_4217
    assert not {"XAU", "XXX", "XTS", "EU", "usd"} & ISO_4217


def test_a_stored_currency_without_a_usable_rate_reads_as_the_ledger_euro() -> None:
    assert read(None).code == "EUR" and read({}).code == "EUR"
    assert read({"currency": "MXN"}).code == "EUR", "no rate: never a guess"
    assert read({"currency": "MXN", "fx_rate": 0}).code == "EUR"
    assert read({"currency": "MXN", "fx_rate": 17.5, "fx_rate_set_on": "2026-11-20"}).set_on == date(
        2026, 11, 20
    )
    assert read({"currency": "USD", "fx_rate": 3}).rate is None
