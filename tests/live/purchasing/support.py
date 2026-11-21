"""What the purchasing server's live tests share: the environment the nightly job sets, the platform's own
MCP client pointed at the server, and the seeding of a fresh Odoo through its JSON-2 API.

Nothing here skips. The job starts Odoo, its database and the server itself, so a missing variable or a
service that does not answer is a broken job, and the test says so by failing (finding TST-3: a live test
that skips when its setup is missing reports green for a proof that never ran).
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from tranibor_adapters.mcp import McpClient
from tranibor_core.egress import AllowedRule, EgressPolicy, use_policy

#: The eleven operations of the profile (`PROFILE.md` of the purchasing server, plan S85 Decision 5).
OPERATIONS = frozenset(
    {
        "search_suppliers",
        "get_supplier",
        "list_framework_agreements",
        "list_contracts",
        "get_contract",
        "create_supplier",
        "update_supplier",
        "archive_supplier",
        "add_contract_note",
        "create_purchase_request",
        "cancel_purchase_request",
    }
)

#: What the job sets. They are not `TRANIBOR_LIVE_*`: `tests/live/conftest.py` turns a missing one of those
#: into a skip, and this job must never skip.
VARIABLES = (
    "PURCHASING_LIVE_MCP_URL",
    "PURCHASING_LIVE_MCP_TOKEN",
    "PURCHASING_LIVE_ODOO_URL",
    "PURCHASING_LIVE_ODOO_DATABASE",
    "PURCHASING_LIVE_ODOO_ADMIN_KEY",
    "PURCHASING_LIVE_ODOO_VERSION",
    "PURCHASING_LIVE_REGISTRY_SUPPORTED",
)


@dataclass(frozen=True)
class Environment:
    mcp_url: str
    mcp_token: str
    odoo_url: str
    database: str
    admin_key: str
    odoo_version: str
    #: Odoo 19 keeps a registration number on a partner; Odoo 20 does not (PROFILE.md): the job says which.
    registry_supported: bool


def environment() -> Environment:
    values = {name: os.environ.get(name, "") for name in VARIABLES}
    missing = [name for name, value in values.items() if not value]
    if missing:  # never a skip: the job that runs this module sets every one of them
        pytest.fail(f"the purchasing live job did not set: {', '.join(missing)}", pytrace=False)
    flag = values["PURCHASING_LIVE_REGISTRY_SUPPORTED"]
    assert flag in {"true", "false"}, f"PURCHASING_LIVE_REGISTRY_SUPPORTED is `true` or `false`, not {flag!r}"
    return Environment(
        mcp_url=values["PURCHASING_LIVE_MCP_URL"],
        mcp_token=values["PURCHASING_LIVE_MCP_TOKEN"],
        odoo_url=values["PURCHASING_LIVE_ODOO_URL"].rstrip("/"),
        database=values["PURCHASING_LIVE_ODOO_DATABASE"],
        admin_key=values["PURCHASING_LIVE_ODOO_ADMIN_KEY"],
        odoo_version=values["PURCHASING_LIVE_ODOO_VERSION"],
        registry_supported=flag == "true",
    )


# -- the purchasing server, over MCP -----------------------------------------------------------------


class ToolFailedError(AssertionError):
    """The server answered `isError`: `code` is the profile's (`not_supported`, `conflict`, ...)."""

    def __init__(self, tool: str, text: str) -> None:
        super().__init__(f"{tool}: {text}")
        self.text = text

    @property
    def code(self) -> str:
        # The SDK may prefix `Error executing tool <name>: `; the server's own text is `<code>: <message>`.
        text = re.sub(r"^Error executing tool \w+: ", "", self.text.strip())
        return text.split(":", 1)[0].strip()


class Server:
    """The platform's MCP client (`tranibor_adapters.mcp`) on the server's `/mcp`, with the bearer token."""

    def __init__(self, env: Environment) -> None:
        self.env = env
        self.played: set[str] = set()

    def _client(self, token: str | None = None) -> McpClient:
        return McpClient(
            self.env.mcp_url, token=self.env.mcp_token if token is None else token, timeout_s=60.0
        )

    def _policy(self) -> EgressPolicy:
        parts = httpx.URL(self.env.mcp_url)
        rule = AllowedRule(parts.host, (parts.port or 80,), ("api",))
        return EgressPolicy(org="live", rules=(rule,), mode="enforce")

    async def tools(self) -> set[str]:
        with use_policy(self._policy(), "api", "live:purchasing"):
            return {tool.name for tool in await self._client().list_tools()}

    async def raw(self, tool: str, arguments: dict[str, Any], *, token: str | None = None) -> dict[str, Any]:
        with use_policy(self._policy(), "api", "live:purchasing"):
            return await self._client(token).call_tool(tool, arguments)

    async def call(self, tool: str, /, **arguments: Any) -> dict[str, Any]:
        result = await self.raw(tool, arguments)
        if result.get("isError"):
            raise ToolFailedError(tool, _text(result))
        self.played.add(tool)
        structured = result.get("structuredContent")
        assert isinstance(structured, dict), f"{tool} answered no structured content: {result}"
        return structured

    async def fails(self, tool: str, /, **arguments: Any) -> str:
        """The profile's error code of a call that must fail; the operation counts as played."""
        result = await self.raw(tool, arguments)
        assert result.get("isError"), f"{tool} {arguments} was expected to fail and answered {result}"
        self.played.add(tool)
        return ToolFailedError(tool, _text(result)).code


def _text(result: dict[str, Any]) -> str:
    parts = [str(item.get("text", "")) for item in result.get("content", []) if isinstance(item, dict)]
    return " ".join(parts)


# -- Odoo, seeded through its JSON-2 API --------------------------------------------------------------


@dataclass(frozen=True)
class Seed:
    supplier_id: str
    supplier_name: str
    registration_number: str | None
    other_supplier_id: str
    other_supplier_name: str
    agreement_id: str
    agreement_title: str
    contract_id: str
    contract_title: str
    contract_end: dt.date
    today: dt.date


class Odoo:
    """A synchronous JSON-2 client, with the administrator's key, for the seeding only."""

    def __init__(self, env: Environment) -> None:
        self._http = httpx.Client(
            base_url=env.odoo_url,
            headers={"Authorization": f"bearer {env.admin_key}", "X-Odoo-Database": env.database},
            timeout=60.0,
        )

    def call(self, model: str, method: str, *, ids: list[int] | None = None, **named: Any) -> Any:
        body = dict(named) if ids is None else {"ids": ids, **named}
        response = self._http.post(f"/json/2/{model}/{method}", json=body)
        assert response.status_code == 200, (
            f"{model}.{method} answered {response.status_code}: {response.text[:400]}"
        )
        return response.json()

    def create(self, model: str, values: dict[str, Any]) -> int:
        return int(self.call(model, "create", vals_list=[values])[0])

    def read(self, model: str, number: int, fields: list[str]) -> dict[str, Any]:
        rows = self.call(model, "read", ids=[number], fields=fields)
        return dict(rows[0])


def seed(env: Environment) -> Seed:
    """A fresh database holds nothing of ours: two suppliers, one product, one open-ended framework
    agreement and one contract that ends in thirty days. Odoo has a single record for both (a confirmed
    blanket order), which is what the profile documents."""
    odoo = Odoo(env)
    today = dt.datetime.now(dt.UTC).date()
    country = odoo.call("res.country", "search_read", domain=[["code", "=", "FR"]], fields=["code"], limit=1)
    assert country, "Odoo has no country FR: the database was not initialised with the base module"
    registration = "552 100 554" if env.registry_supported else None
    supplier_values: dict[str, Any] = {
        "name": "Live Seed Supplies SA",
        "supplier_rank": 1,
        "country_id": country[0]["id"],
        "email": "orders@live-seed.example.test",
        "city": "Paris",
        "zip": "75001",
        "street": "1 rue de l'Exemple",
    }
    if registration:
        supplier_values["company_registry"] = registration
    supplier = odoo.create("res.partner", supplier_values)
    other = odoo.create(
        "res.partner", {"name": "Live Other Trading Ltd", "supplier_rank": 1, "country_id": country[0]["id"]}
    )
    product = odoo.create("product.product", {"name": "Live Seed Item", "purchase_ok": True})

    def blanket(title: str, vendor: int, start: dt.date, end: dt.date | None, terms: str) -> int:
        number = odoo.create(
            "purchase.requisition",
            {
                "requisition_type": "blanket_order",
                "reference": title,
                "vendor_id": vendor,
                "date_start": start.isoformat(),
                "date_end": end.isoformat() if end else False,
                "description": f"<p>{terms}</p>",
                "line_ids": [[0, 0, {"product_id": product, "product_qty": 10.0, "price_unit": 5.0}]],
            },
        )
        _confirm(odoo, number)
        return number

    agreement_title, contract_title = "LIVE-FA-001", "LIVE-CT-001"
    contract_end = today + dt.timedelta(days=30)
    agreement = blanket(
        agreement_title, supplier, today - dt.timedelta(days=30), None, "Seed framework terms"
    )
    contract = blanket(
        contract_title, supplier, today - dt.timedelta(days=300), contract_end, "Seed contract terms"
    )
    return Seed(
        supplier_id=str(supplier),
        supplier_name="Live Seed Supplies SA",
        registration_number=registration,
        other_supplier_id=str(other),
        other_supplier_name="Live Other Trading Ltd",
        agreement_id=str(agreement),
        agreement_title=agreement_title,
        contract_id=str(contract),
        contract_title=contract_title,
        contract_end=contract_end,
        today=today,
    )


def _confirm(odoo: Odoo, number: int) -> None:
    """Confirm a blanket order. The method is `action_confirm` where the state `confirmed` exists; the
    older name is tried if Odoo says the method is unknown. The state read back is what counts."""
    last = ""
    for method in ("action_confirm", "action_in_progress"):
        response = odoo._http.post(f"/json/2/purchase.requisition/{method}", json={"ids": [number]})
        if response.status_code == 200:
            break
        last = f"{method}: {response.status_code} {response.text[:300]}"
    state = odoo.read("purchase.requisition", number, ["state"])["state"]
    assert state == "confirmed", f"the blanket order {number} is {state!r} after confirming ({last})"
