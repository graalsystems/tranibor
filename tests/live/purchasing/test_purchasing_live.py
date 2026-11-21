"""The purchasing server's profile against a real Odoo (S85-14).

The nightly job `purchasing-live` starts Odoo and its database as containers, initialises a database with
Contacts, Purchase and Purchase Agreements, seeds it through the JSON-2 API, runs the published image of the
purchasing server against it and runs this module. Every operation of the profile is played over MCP, the
writes and their compensations included, and each answer is checked against `PROFILE.md`.

Nothing here skips (finding TST-3). An operation Odoo cannot serve asserts the documented `not_supported`
answer instead: the category filter of the agreements on every Odoo, and the registration number on Odoo 20,
which has no `company_registry` on a partner. A defect this finds is a `finding` issue (origin S85-14),
fixed in the agents repository by a patch version, never bent around here.

The tests share one Odoo and run in file order (the reads, then the writes, then the check that all eleven
operations were played).

    # what the job runs (see docs/runbooks/purchasing-live.md):
    uv run pytest tests/live/purchasing -m purchasing_live -rs
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator

import pytest
from tranibor_adapters.mcp import McpError

from .support import OPERATIONS, Environment, Seed, Server, environment, seed

pytestmark = pytest.mark.purchasing_live

SUPPLIER_KEYS = {
    "id",
    "name",
    "registration_number",
    "vat_number",
    "country",
    "email",
    "phone",
    "website",
    "address_line",
    "city",
    "postal_code",
    "categories",
    "status",
    "archived_reason",
    "created_at",
    "updated_at",
}
AGREEMENT_KEYS = {
    "id",
    "supplier_id",
    "title",
    "category",
    "valid_from",
    "valid_to",
    "currency",
    "terms_summary",
}
CONTRACT_KEYS = {
    "id",
    "supplier_id",
    "title",
    "category",
    "start_date",
    "end_date",
    "currency",
    "value",
    "notes",
}


@pytest.fixture(scope="module")
def env() -> Environment:
    return environment()


@pytest.fixture(scope="module")
def data(env: Environment) -> Seed:
    return seed(env)


@pytest.fixture(scope="module")
def server(env: Environment) -> Server:
    return Server(env)


@pytest.fixture
def key() -> Iterator[str]:
    """A fresh `action_key`, as the platform fills one for each governed action."""
    yield f"live-{uuid.uuid4()}"


def _page(result: dict, rows: str) -> list[dict]:
    assert set(result) >= {rows, "total", "next_cursor"}, result
    assert isinstance(result["total"], int)
    assert isinstance(result[rows], list)
    return result[rows]


# -- the server itself ------------------------------------------------------------------------------


async def test_the_server_serves_exactly_the_eleven_operations_behind_its_token(
    server: Server, data: Seed
) -> None:
    assert await server.tools() == OPERATIONS
    with pytest.raises(McpError) as refused:
        await server.raw("get_supplier", {"supplier_id": data.supplier_id}, token="not-the-platforms-token")
    assert refused.value.code == 401


# -- reads ------------------------------------------------------------------------------------------


async def test_search_suppliers_by_name_and_country_and_paged(server: Server, data: Seed) -> None:
    found = _page(await server.call("search_suppliers", name="live seed"), "suppliers")
    assert [row["id"] for row in found] == [data.supplier_id]
    row = found[0]
    assert set(row) >= SUPPLIER_KEYS
    assert (row["name"], row["country"], row["status"]) == (data.supplier_name, "FR", "active")
    assert row["created_at"].endswith("Z") and "T" in row["created_at"]

    in_france = _page(await server.call("search_suppliers", country="FR"), "suppliers")
    assert {r["id"] for r in in_france} == {data.supplier_id, data.other_supplier_id}
    assert [r["name"] for r in in_france] == sorted(r["name"] for r in in_france)  # by name, then id
    assert _page(await server.call("search_suppliers", country="DE"), "suppliers") == []

    first = await server.call("search_suppliers", limit=1)
    assert len(first["suppliers"]) == 1 and first["total"] == 2 and first["next_cursor"]
    second = await server.call("search_suppliers", limit=1, cursor=first["next_cursor"])
    assert len(second["suppliers"]) == 1 and second["next_cursor"] is None
    assert {first["suppliers"][0]["id"], second["suppliers"][0]["id"]} == {
        data.supplier_id,
        data.other_supplier_id,
    }


async def test_search_suppliers_by_registration_number_or_its_documented_not_supported(
    env: Environment, server: Server, data: Seed
) -> None:
    if env.registry_supported:
        for spelling in ("552100554", "552 100 554", "552.100.554"):
            found = _page(await server.call("search_suppliers", registration_number=spelling), "suppliers")
            assert [r["id"] for r in found] == [data.supplier_id], spelling
        assert found[0]["registration_number"] == data.registration_number
        assert (
            _page(await server.call("search_suppliers", registration_number="999999999"), "suppliers") == []
        )
    else:  # Odoo 20: no `company_registry` on a partner (PROFILE.md): answered, never approximated
        assert await server.fails("search_suppliers", registration_number="552100554") == "not_supported"
        row = (await server.call("get_supplier", supplier_id=data.supplier_id))["supplier"]
        assert row["registration_number"] is None


async def test_get_supplier(server: Server, data: Seed) -> None:
    supplier = (await server.call("get_supplier", supplier_id=data.supplier_id))["supplier"]
    assert set(supplier) >= SUPPLIER_KEYS
    assert supplier["id"] == data.supplier_id and supplier["name"] == data.supplier_name
    assert (supplier["email"], supplier["city"], supplier["postal_code"]) == (
        "orders@live-seed.example.test",
        "Paris",
        "75001",
    )
    assert supplier["address_line"] == "1 rue de l'Exemple" and supplier["status"] == "active"
    assert supplier["archived_reason"] is None
    assert await server.fails("get_supplier", supplier_id="999999") == "not_found"


async def test_list_framework_agreements_and_the_category_filter_that_odoo_cannot_serve(
    server: Server, data: Seed
) -> None:
    today = data.today.isoformat()
    listed = await server.call("list_framework_agreements", valid_on=today)
    rows = _page(listed, "agreements")
    assert {r["id"] for r in rows} == {data.agreement_id, data.contract_id}  # one record is both (PROFILE.md)
    assert listed["total"] == 2 and listed["next_cursor"] is None
    agreement = next(r for r in rows if r["id"] == data.agreement_id)
    assert set(agreement) >= AGREEMENT_KEYS
    assert agreement["supplier_id"] == data.supplier_id and agreement["title"] == data.agreement_title
    assert agreement["valid_to"] is None and agreement["category"] == ""
    assert agreement["currency"] and agreement["terms_summary"] == "Seed framework terms"
    assert agreement["valid_from"] == (data.today - dt.timedelta(days=30)).isoformat()
    # valid_on: both ends included, and an agreement that has not started is not valid.
    assert _page(await server.call("list_framework_agreements", valid_on="2000-01-01"), "agreements") == []
    on_last_day = await server.call("list_framework_agreements", valid_on=data.contract_end.isoformat())
    assert {r["id"] for r in on_last_day["agreements"]} == {data.agreement_id, data.contract_id}
    after = data.contract_end + dt.timedelta(days=1)
    assert {
        r["id"]
        for r in (await server.call("list_framework_agreements", valid_on=after.isoformat()))["agreements"]
    } == {data.agreement_id}
    paged = await server.call("list_framework_agreements", limit=1)
    assert len(paged["agreements"]) == 1 and paged["total"] == 2 and paged["next_cursor"]
    # an Odoo agreement has no category: the filter is answered, not ignored
    assert await server.fails("list_framework_agreements", category="office-supplies") == "not_supported"


async def test_list_contracts(server: Server, data: Seed) -> None:
    everything = await server.call("list_contracts")
    assert {r["id"] for r in _page(everything, "contracts")} == {data.agreement_id, data.contract_id}

    soon = (data.today + dt.timedelta(days=60)).isoformat()
    ending = _page(await server.call("list_contracts", ending_before=soon), "contracts")
    assert [r["id"] for r in ending] == [data.contract_id]  # an open-ended one is never returned by it
    contract = ending[0]
    assert set(contract) >= CONTRACT_KEYS - {"notes"} | {"note_count"}
    assert contract["supplier_id"] == data.supplier_id and contract["title"] == data.contract_title
    assert contract["end_date"] == data.contract_end.isoformat() and contract["note_count"] == 0
    assert contract["value"] is None and contract["category"] == ""  # Odoo carries no total nor category
    # strictly before: the end date itself is not before itself
    assert (
        _page(await server.call("list_contracts", ending_before=data.contract_end.isoformat()), "contracts")
        == []
    )

    of_supplier = _page(await server.call("list_contracts", supplier_id=data.supplier_id), "contracts")
    assert {r["id"] for r in of_supplier} == {data.agreement_id, data.contract_id}
    assert _page(await server.call("list_contracts", supplier_id=data.other_supplier_id), "contracts") == []


async def test_get_contract(server: Server, data: Seed) -> None:
    contract = (await server.call("get_contract", contract_id=data.contract_id))["contract"]
    assert set(contract) >= CONTRACT_KEYS
    assert contract["id"] == data.contract_id and contract["notes"] == []
    assert contract["start_date"] == (data.today - dt.timedelta(days=300)).isoformat()
    assert await server.fails("get_contract", contract_id="999999") == "not_found"


# -- writes and their compensations -----------------------------------------------------------------


async def test_create_supplier_is_idempotent_refuses_a_duplicate_and_is_undone_by_archiving(
    env: Environment, server: Server, key: str
) -> None:
    arguments: dict = {
        "name": "Live Created Supplier",
        "country": "FR",
        "email": "hello@live-created.example.test",
        "city": "Lyon",
        "categories": ["office-supplies"],
    }
    if env.registry_supported:
        arguments["registration_number"] = "443 061 841"
    created = await server.call("create_supplier", action_key=key, **arguments)
    assert set(created) >= {"supplier_id", "supplier", "created"} and created["created"] is True
    supplier = created["supplier"]
    assert set(supplier) >= SUPPLIER_KEYS and supplier["id"] == created["supplier_id"]
    assert supplier["name"] == "Live Created Supplier" and supplier["country"] == "FR"
    assert supplier["categories"] == ["office-supplies"] and supplier["status"] == "active"

    replay = await server.call("create_supplier", action_key=key, **arguments)
    assert replay["supplier_id"] == created["supplier_id"]  # the first result, nothing written
    assert (await server.call("search_suppliers", name="live created"))["total"] == 1
    assert await server.fails("create_supplier", action_key=f"{key}-again", **arguments) == "conflict"

    # the compensation the platform plays: archive_supplier with the reason it fills in
    undone = await server.call(
        "archive_supplier",
        supplier_id=created["supplier_id"],
        reason="Creation undone.",
        action_key=f"{key}-undo",
    )
    assert undone["archived"] is True and undone["status"] == "archived"
    assert (await server.call("get_supplier", supplier_id=created["supplier_id"]))["supplier"][
        "status"
    ] == "archived"
    assert (await server.call("search_suppliers", name="live created"))["total"] == 0
    with_archived = await server.call("search_suppliers", name="live created", include_archived=True)
    assert [r["id"] for r in with_archived["suppliers"]] == [created["supplier_id"]]


async def test_a_registration_number_is_written_on_odoo_19_and_refused_on_odoo_20(
    env: Environment, server: Server, key: str
) -> None:
    arguments = {"name": "Live Numbered Supplier", "country": "FR", "registration_number": "732 829 320"}
    if env.registry_supported:
        created = await server.call("create_supplier", action_key=key, **arguments)
        assert created["supplier"]["registration_number"] == "732 829 320"
        found = await server.call("search_suppliers", registration_number="732829320")
        assert [r["id"] for r in found["suppliers"]] == [created["supplier_id"]]
        await server.call(
            "archive_supplier",
            supplier_id=created["supplier_id"],
            reason="Cleanup.",
            action_key=f"{key}-undo",
        )
    else:  # Odoo 20 has no `company_registry` on a partner: answered, and nothing is written (PROFILE.md)
        assert await server.fails("create_supplier", action_key=key, **arguments) == "not_supported"
        assert (await server.call("search_suppliers", name="live numbered"))["total"] == 0


async def test_update_supplier_reports_what_changed_and_its_compensation_restores_it(
    server: Server, key: str
) -> None:
    created = await server.call(
        "create_supplier", name="Live Updated Supplier", country="FR", city="Nantes", action_key=key
    )
    number = created["supplier_id"]
    changed = await server.call(
        "update_supplier",
        supplier_id=number,
        fields={"city": "Lille", "phone": "+33 1 23 45 67 89"},
        action_key=f"{key}-update",
    )
    assert set(changed) >= {"supplier_id", "supplier", "changed", "previous"}
    assert set(changed["changed"]) == {"city", "phone"} and changed["supplier"]["city"] == "Lille"
    assert changed["previous"]["city"] == "Nantes"
    assert (await server.call("get_supplier", supplier_id=number))["supplier"]["phone"] == "+33 1 23 45 67 89"

    # the compensation: update_supplier with `fields` = the result's `previous`, as JSON text
    restored = await server.call(
        "update_supplier",
        supplier_id=number,
        fields=json.dumps(changed["previous"]),
        action_key=f"{key}-compensation",
    )
    assert restored["supplier"]["city"] == "Nantes" and not restored["supplier"]["phone"]


async def test_archive_supplier_twice_changes_nothing_the_second_time(server: Server, key: str) -> None:
    number = (
        await server.call("create_supplier", name="Live Archived Supplier", country="FR", action_key=key)
    )["supplier_id"]
    first = await server.call(
        "archive_supplier", supplier_id=number, reason="No longer used.", action_key=f"{key}-1"
    )
    assert (first["supplier_id"], first["status"], first["previous_status"], first["archived"]) == (
        number,
        "archived",
        "active",
        True,
    )
    second = await server.call("archive_supplier", supplier_id=number, action_key=f"{key}-2")
    assert second["archived"] is False and second["status"] == "archived"
    assert await server.fails("archive_supplier", supplier_id="999999", action_key=f"{key}-3") == "not_found"


async def test_add_contract_note_is_read_back_counted_and_idempotent(
    server: Server, data: Seed, key: str
) -> None:
    text = "Renewal decision: renew for one year. <b>Live</b> & checked."
    added = await server.call("add_contract_note", contract_id=data.contract_id, text=text, action_key=key)
    assert set(added) >= {"contract_id", "note_id", "created_at"} and added["contract_id"] == data.contract_id
    assert added["created_at"].endswith("Z")
    replay = await server.call("add_contract_note", contract_id=data.contract_id, text=text, action_key=key)
    assert replay["note_id"] == added["note_id"]

    contract = (await server.call("get_contract", contract_id=data.contract_id))["contract"]
    assert [note["id"] for note in contract["notes"]] == [added["note_id"]]  # written once
    assert contract["notes"][0]["text"] == text  # plain text in, plain text out: Odoo escapes it
    listed = _page(await server.call("list_contracts", supplier_id=data.supplier_id), "contracts")
    assert {r["id"]: r["note_count"] for r in listed}[data.contract_id] == 1
    assert (
        await server.fails("add_contract_note", contract_id="999999", text="x", action_key=f"{key}-2")
        == "not_found"
    )


async def test_create_purchase_request_computes_its_total_and_cancel_undoes_it(
    server: Server, data: Seed, key: str
) -> None:
    lines = [
        {"description": "Paper A4", "quantity": "2", "unit_price": "3.50"},
        {"description": "Toner", "quantity": "1", "unit_price": "5.00", "unit": "box"},
    ]
    needed_by = (data.today + dt.timedelta(days=14)).isoformat()
    created = await server.call(
        "create_purchase_request",
        supplier_id=data.supplier_id,
        title="Live office supplies",
        lines=lines,
        currency="EUR",
        needed_by=needed_by,
        contract_id=data.contract_id,
        justification="Nightly proof.",
        action_key=key,
    )
    assert set(created) >= {"request_id", "status", "total", "currency", "request"}
    assert created["status"] == "draft" and created["total"] == "12.00" and created["currency"] == "EUR"
    request = created["request"]
    assert request["id"] == created["request_id"] and request["supplier_id"] == data.supplier_id
    assert request["contract_id"] == data.contract_id and request["title"] == "Live office supplies"
    assert request["justification"] == "Nightly proof." and request["needed_by"] == needed_by
    assert [
        (x["description"], x["quantity"], x["unit_price"], x["line_total"], x["unit"])
        for x in request["lines"]
    ] == [
        ("Paper A4", "2", "3.50", "7.00", "unit"),
        ("Toner", "1", "5.00", "5.00", "box"),
    ]
    replay = await server.call(
        "create_purchase_request",
        supplier_id=data.supplier_id,
        title="Live office supplies",
        lines=lines,
        currency="EUR",
        needed_by=needed_by,
        contract_id=data.contract_id,
        justification="Nightly proof.",
        action_key=key,
    )
    assert replay["request_id"] == created["request_id"]

    # the compensation the platform plays: cancel_purchase_request with the reason it fills in
    cancelled = await server.call(
        "cancel_purchase_request",
        request_id=created["request_id"],
        reason="Creation undone.",
        action_key=f"{key}-undo",
    )
    assert cancelled["cancelled"] is True and cancelled["status"] == "cancelled"
    assert cancelled["previous_status"] == "draft"
    again = await server.call(
        "cancel_purchase_request",
        request_id=created["request_id"],
        reason="Again.",
        action_key=f"{key}-again",
    )
    assert again["cancelled"] is False and again["status"] == "cancelled"
    assert await server.fails(
        "cancel_purchase_request", request_id="999999", reason="x", action_key=f"{key}-3"
    ) == ("not_found")


async def test_a_purchase_request_to_an_archived_supplier_is_a_conflict(server: Server, key: str) -> None:
    number = (await server.call("create_supplier", name="Live Gone Supplier", country="FR", action_key=key))[
        "supplier_id"
    ]
    await server.call("archive_supplier", supplier_id=number, reason="Gone.", action_key=f"{key}-archive")
    code = await server.fails(
        "create_purchase_request",
        supplier_id=number,
        title="Should not exist",
        lines=[{"description": "x", "quantity": "1", "unit_price": "1.00"}],
        action_key=f"{key}-request",
    )
    assert code == "conflict"


# -- rules that hold whatever the backend is, here on the real one ----------------------------------


async def test_a_write_without_an_action_key_and_a_bank_detail_are_refused_and_write_nothing(
    server: Server,
) -> None:
    assert await server.fails("create_supplier", name="Live Unkeyed Supplier", country="FR") == "refused"
    assert (
        await server.fails(
            "create_supplier",
            name="Live Bank Supplier",
            country="FR",
            iban="FR7630006000011234567890189",
            action_key="live-bank",
        )
        == "refused"
    )
    for name in ("live unkeyed", "live bank"):
        assert (await server.call("search_suppliers", name=name, include_archived=True))["total"] == 0


async def test_every_operation_of_the_profile_was_played(server: Server) -> None:
    """The last test: a profile operation no test above played is a hole in the night's proof."""
    assert server.played == OPERATIONS, f"not played: {sorted(OPERATIONS - server.played)}"
