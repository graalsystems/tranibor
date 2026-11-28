# SPDX-License-Identifier: Apache-2.0
"""The ServiceNow Table API, as the `servicenow` tracker calls it (ADR 0228).

Ported from the `servicenow` plugin's server (`tranibor-agents/plugins/servicenow/server`, Apache-2.0):
its client, its closed list of tables and fields, and its typed encoded queries, reduced to what a tracker
needs. The rules it keeps:

- **Authentication.** OAuth 2.0 client credentials (`/oauth_token.do`, the token cached until a minute
  before it expires, renewed once on a 401), or basic authentication of a dedicated integration user.
- **Closed lists.** Only the tables of `TABLE_FIELDS`, and in each only the fields listed there
  (`sysparm_fields` is always sent): a column added to the instance (a custom one holding personal data)
  never leaks through by default. Every read asks for `sysparm_display_value=all`, so a reference comes
  back with its sys_id and its label.
- **Typed queries.** An encoded query is built from conditions, never from a caller's raw string: a
  value cannot end its condition (`^` is doubled), carry a new line, or ask the instance to run script
  (`javascript:` is refused).
- **Writes are checked.** ServiceNow answers 200 to a write it did not apply (a field ACL, a refused
  choice): the answer is compared with what was written, and a kept value is an error.
- **Change requests.** A change's state moves through the Change Management API
  (`/api/sn_chg_rest/change`), only to a state its model reaches: the Table API's write of `state` is
  refused by the model's Business Rule.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..errors import ConfigurationError, UpstreamError
from ..http import RestClient

#: The rows one call reads at most.
MAX_ROWS = 50
#: A token is renewed this many seconds before ServiceNow says it expires.
TOKEN_MARGIN_S = 60
JOURNAL_ELEMENTS = ("work_notes", "comments")
#: The table a task table takes its state labels from when it has none of its own: a real instance holds
#: the request item's states on `task`, none on `sc_req_item`.
CHOICE_PARENT = "task"
#: ServiceNow's out-of-box state lists, used only when `sys_choice` answers 403 (an `itil` user without a
#: read right on it). A state renamed on the instance is then unknown: grant the read (the procedure says so).
DEFAULT_STATE_CHOICES: dict[str, dict[str, str]] = {
    "incident": {
        "1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled",
    },
    "change_request": {
        "-5": "New", "-4": "Assess", "-3": "Authorize", "-2": "Scheduled", "-1": "Implement", "0": "Review",
        "3": "Closed", "4": "Canceled",
    },
    CHOICE_PARENT: {
        "-5": "Pending", "1": "Open", "2": "Work in Progress", "3": "Closed Complete",
        "4": "Closed Incomplete", "7": "Closed Skipped",
    },
}  # fmt: skip

TABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "incident": (
        "sys_id", "number", "short_description", "description", "state", "impact", "urgency", "priority",
        "category", "subcategory", "assignment_group", "assigned_to", "caller_id", "cmdb_ci", "opened_at",
        "resolved_at", "closed_at", "close_code", "close_notes", "correlation_id", "active",
        "sys_created_on", "sys_updated_on", "sys_mod_count",
    ),
    "sc_req_item": (
        "sys_id", "number", "short_description", "description", "state", "stage", "cat_item",
        "requested_for", "impact", "urgency", "priority", "assignment_group", "assigned_to", "opened_at",
        "closed_at", "active", "sys_created_on", "sys_updated_on", "sys_mod_count",
    ),
    "change_request": (
        "sys_id", "number", "short_description", "description", "state", "type", "risk", "impact",
        "urgency", "priority", "category", "assignment_group", "assigned_to", "cmdb_ci", "correlation_id",
        "close_code", "close_notes", "active", "closed_at", "sys_created_on", "sys_updated_on",
        "sys_mod_count",
    ),
    "sys_choice": ("sys_id", "name", "element", "value", "label", "sequence", "inactive", "language"),
    "sys_journal_field": (
        "sys_id", "element_id", "name", "element", "value", "sys_created_on", "sys_created_by",
    ),
}  # fmt: skip
#: The tables whose records are work items, in the order the candidates are read.
TRACKER_TABLES: tuple[str, ...] = ("incident", "sc_req_item", "change_request")
NUMBER_PREFIXES: dict[str, str] = {"INC": "incident", "RITM": "sc_req_item", "CHG": "change_request"}
#: The stored state values that close an item, per table (ServiceNow's defaults).
CLOSED_STATES: dict[str, frozenset[str]] = {
    "incident": frozenset({"6", "7", "8"}),
    "sc_req_item": frozenset({"3", "4", "7"}),
    "change_request": frozenset({"3", "4"}),
}
#: What the tracker may write besides the state and the journal: never `priority`, which ServiceNow
#: computes from impact and urgency.
UPDATABLE_FIELDS: frozenset[str] = frozenset(
    {"impact", "urgency", "category", "subcategory", "assignment_group", "assigned_to", "cmdb_ci"}
)
REFERENCE_FIELDS: frozenset[str] = frozenset({"assignment_group", "assigned_to", "cmdb_ci", "caller_id"})

_SYS_ID = re.compile(r"^[0-9a-f]{32}$")
_NUMBER = re.compile(r"^(INC|RITM|CHG)[0-9]{7,10}$")
_FIELD = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)?$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_OPERATORS = {"eq": "=", "ne": "!=", "in": "IN"}


def is_sys_id(value: str) -> bool:
    return bool(_SYS_ID.match(value))


def table_of_number(number: str) -> str:
    """The tracker table a record number belongs to (`INC0010001` -> `incident`)."""
    if not _NUMBER.match(number):
        raise ConfigurationError(
            f"{number!r} is not an incident, request item or change number (INC…, RITM…, CHG…)"
        )
    return NUMBER_PREFIXES[re.sub(r"[0-9]+$", "", number)]


def escape_value(value: str) -> str:
    """A value as it can stand in a condition: carets doubled, script and control characters refused."""
    if _CONTROL.search(value):
        raise ConfigurationError("a ServiceNow query value cannot hold a control character or a new line")
    if "javascript:" in value.lower():
        raise ConfigurationError("a ServiceNow query value cannot hold 'javascript:'")
    return value.replace("^", "^^")


@dataclass(frozen=True)
class Condition:
    """`field <op> value`; `op` is `eq`, `ne` or `in` (a list)."""

    field: str
    value: str | tuple[str, ...]
    op: str = "eq"


@dataclass
class Query:
    """An encoded query: AND of groups, each group an OR of conditions, then an optional order."""

    table: str
    groups: list[list[str]] = field(default_factory=list)
    order_by: str = ""

    def where(self, *conditions: Condition) -> Query:
        self.groups.extend([self._encode(c)] for c in conditions)
        return self

    def any_of(self, *conditions: Condition) -> Query:
        if conditions:
            self.groups.append([self._encode(c) for c in conditions])
        return self

    def newest_first(self, name: str) -> Query:
        self.order_by = f"ORDERBYDESC{self._field(name)}"
        return self

    def encode(self) -> str:
        return "^".join(
            ["^OR".join(group) for group in self.groups] + ([self.order_by] if self.order_by else [])
        )

    def _field(self, name: str) -> str:
        served = TABLE_FIELDS[self.table]
        if not _FIELD.match(name) or (name not in served and name.split(".", 1)[0] not in served):
            raise ConfigurationError(f"field {name!r} is not served on {self.table}")
        return name

    def _encode(self, condition: Condition) -> str:
        name, operator = self._field(condition.field), _OPERATORS[condition.op]
        if isinstance(condition.value, tuple):
            if not condition.value or any("," in v for v in condition.value):
                raise ConfigurationError("an IN list needs values, none holding a comma")
            return f"{name}{operator}{','.join(escape_value(v) for v in condition.value)}"
        return f"{name}{operator}{escape_value(condition.value)}"


def eq(name: str, value: str) -> Condition:
    return Condition(name, value)


def normalise(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A record as `{sys_id, fields, display}`: `fields` holds the stored values (sys_ids, choice codes),
    `display` the label of each field whose label differs from its value (a group's name, a state's label)."""
    values: dict[str, Any] = {}
    display: dict[str, Any] = {}
    for name, cell in raw.items():
        if isinstance(cell, dict) and ("value" in cell or "display_value" in cell):
            value, label = cell.get("value"), cell.get("display_value")
        else:
            value = label = cell
        values[name] = value
        if label not in (None, "") and label != value:
            display[name] = label
    return {"sys_id": values.get("sys_id"), "fields": values, "display": display}


class ServiceNowOAuth:
    """An OAuth 2.0 client of the instance (client credentials), its token cached in memory only."""

    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._token: tuple[str, float] | None = None

    async def token(self, client: Any, instance_url: str, *, renew: bool = False) -> str:
        if renew or self._token is None or self._token[1] <= time.monotonic():
            response = await client.post(
                f"{instance_url}/oauth_token.do",
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self._secret,
                },
            )
            if response.status_code != 200:
                raise UpstreamError(
                    "servicenow",
                    f"{response.status_code}: the instance refused the OAuth client's token",
                    status_code=response.status_code,
                )
            body = response.json()
            expires_in = int(body.get("expires_in", 1800))
            self._token = (str(body["access_token"]), time.monotonic() + max(expires_in - TOKEN_MARGIN_S, 0))
        return self._token[0]


class ServiceNowClient(RestClient):
    """The Table API of one instance, under a basic integration user or an OAuth client."""

    def __init__(
        self,
        instance_url: str,
        *,
        username: str | None = None,
        password: str | None = None,
        oauth: ServiceNowOAuth | None = None,
        **kwargs: Any,
    ) -> None:
        if (oauth is None) == (username is None):
            raise ConfigurationError(
                "a ServiceNow connector authenticates as a basic user or an OAuth client"
            )
        kwargs.setdefault("service", "servicenow")
        super().__init__(instance_url, auth=(username, password or "") if username else None, **kwargs)
        self.oauth = oauth
        self._choices: dict[str, dict[str, str]] = {}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self.oauth is None:
            return await super().request(method, path, **kwargs)
        for renew in (False, True):
            headers = dict(kwargs.get("headers") or {})
            headers["Authorization"] = (
                f"Bearer {await self.oauth.token(self._client, self.base_url, renew=renew)}"
            )
            try:
                return await super().request(method, path, **{**kwargs, "headers": headers})
            except UpstreamError as error:
                if error.status_code != 401 or renew:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    # ───────────────────────── the Table API ─────────────────────────

    def _params(self, table: str, fields: tuple[str, ...] | None = None) -> dict[str, str]:
        if table not in TABLE_FIELDS:
            raise ConfigurationError(f"table {table!r} is not served by the ServiceNow tracker")
        return {
            "sysparm_fields": ",".join(fields or TABLE_FIELDS[table]),
            "sysparm_display_value": "all",
            "sysparm_exclude_reference_link": "true",
        }

    async def page(
        self, query: Query, *, fields: tuple[str, ...] | None = None, limit: int = MAX_ROWS, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int | None]:
        """One page of rows, and how many the instance holds (`X-Total-Count`, when it says)."""
        params = {
            **self._params(query.table, fields),
            "sysparm_query": query.encode(),
            "sysparm_limit": str(max(1, min(limit, MAX_ROWS))),
        }
        if offset:
            params["sysparm_offset"] = str(offset)
        body, headers = await self.request(
            "GET", f"/api/now/table/{query.table}", params=params, with_headers=True
        )
        total = headers.get("X-Total-Count")
        rows = [normalise(row) for row in (body or {}).get("result") or []]
        return rows, int(total) if total and str(total).isdigit() else None

    async def read(
        self, query: Query, *, fields: tuple[str, ...] | None = None, bound: int
    ) -> list[dict[str, Any]]:
        """Up to `bound` rows, in pages of `MAX_ROWS`."""
        found: list[dict[str, Any]] = []
        while len(found) < bound:
            wanted = min(MAX_ROWS, bound - len(found))
            rows, total = await self.page(query, fields=fields, limit=wanted, offset=len(found))
            found.extend(rows)
            if len(rows) < wanted or (total is not None and len(found) >= total):
                break
        return found

    async def find_one(self, query: Query) -> dict[str, Any] | None:
        rows, _ = await self.page(query, limit=1)
        return rows[0] if rows else None

    async def get(self, table: str, sys_id: str, *, fields: tuple[str, ...] | None = None) -> dict[str, Any]:
        found = await self.request(
            "GET", f"/api/now/table/{table}/{_sys_id(sys_id)}", params=self._params(table, fields)
        )
        return normalise((found or {}).get("result") or {})

    async def create(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        created = await self.request(
            "POST", f"/api/now/table/{table}", params=self._params(table), json=values
        )
        return normalise((created or {}).get("result") or {})

    async def update(self, table: str, sys_id: str, values: dict[str, Any]) -> dict[str, Any]:
        path = f"/api/now/table/{table}/{_sys_id(sys_id)}"
        updated = await self.request("PATCH", path, params=self._params(table), json=values)
        record = normalise((updated or {}).get("result") or {})
        check_applied(table, values, record)
        return record

    # ───────────────────────── change requests ─────────────────────────

    async def change_next_states(self, sys_id: str, current: str) -> dict[str, str]:
        """`{value: label}` of the states a change's model lets it move to from `current`."""
        found = await self.request("GET", f"/api/sn_chg_rest/change/{_sys_id(sys_id)}/nextstates")
        result = (found or {}).get("result")
        if not isinstance(result, dict):
            raise UpstreamError("servicenow", "the Change Management API's next states are not an object")
        available = result.get("available_states") or []
        if any(value is None for value in available):
            # What a user without `sn_change_read` gets: 200 and `available_states: [null]`, not a 403.
            raise UpstreamError(
                "servicenow",
                "the Change Management API answered no next state: "
                "the integration user lacks `sn_change_read`",
                status_code=403,
            )
        labels = {str(k): str(v) for k, v in (result.get("state_label") or {}).items()}
        known = await self.choices("change_request")
        return {
            str(v): known.get(str(v)) or labels.get(str(v)) or str(v)
            for v in available
            if str(v) != str(current)
        }

    async def update_change(self, sys_id: str, values: dict[str, Any]) -> None:
        """A change's fields, its state included, through the Change Management API, read back and checked."""
        await self.request("PATCH", f"/api/sn_chg_rest/change/{_sys_id(sys_id)}", json=values)
        check_applied("change_request", values, await self.get("change_request", sys_id))

    # ───────────────────────── choices ─────────────────────────

    async def choices(self, table: str) -> dict[str, str]:
        """`{value: label}` of a table's states, from `sys_choice` (English labels), cached; a table without
        a list of its own takes `task`'s. A 403 falls back on the out-of-box list."""
        if table in self._choices:
            return self._choices[table]
        query = Query("sys_choice").where(
            Condition("name", (table, CHOICE_PARENT), "in"), eq("element", "state"), eq("inactive", "false")
        )
        try:
            rows = await self.read(query, fields=("name", "value", "label", "language"), bound=200)
        except UpstreamError as error:
            if error.status_code != 403:
                raise
            mapping = dict(DEFAULT_STATE_CHOICES.get(table) or DEFAULT_STATE_CHOICES[CHOICE_PARENT])
        else:
            by_name: dict[str, dict[str, str]] = {}
            for row in rows:
                if row["fields"].get("language") not in (None, "", "en"):
                    continue
                own = by_name.setdefault(str(row["fields"].get("name") or table), {})
                own[str(row["fields"].get("value"))] = str(row["fields"].get("label"))
            mapping = by_name.get(table) or by_name.get(CHOICE_PARENT, {})
        self._choices[table] = mapping
        return mapping


def check_applied(table: str, values: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    """Every field written that the answer carries holds the value written (journal fields carry none)."""
    fields = record.get("fields") or {}
    kept = sorted(
        name
        for name, asked in values.items()
        if name not in JOURNAL_ELEMENTS
        and name in fields
        and str(fields.get(name) or "") != str(asked if asked is not None else "")
    )
    if kept:
        raise UpstreamError(
            "servicenow",
            f"ServiceNow did not apply {', '.join(kept)} on {table}: the integration user's role cannot "
            "write it, or the instance refused the value",
        )


def _sys_id(value: str) -> str:
    if not is_sys_id(value):
        raise ConfigurationError("a sys_id is 32 lowercase hexadecimal characters")
    return value


def check_servicenow(cfg: Mapping[str, Any]) -> None:
    """The instance to call, the credential that fits it, the queue polled and the tables served."""
    from ..registry import fakes_enabled

    url = str(cfg.get("instance_url") or "").strip()
    try:
        parts = urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError on a port out of range
    except ValueError as invalid:
        raise ConfigurationError("`instance_url` is not a valid address") from invalid
    if parts.scheme not in ({"https", "http"} if fakes_enabled() else {"https"}) or not parts.hostname:
        raise ConfigurationError(
            "`instance_url` must be the https address of the instance (https://<name>.service-now.com)"
        )
    auth = cfg.get("auth", "oauth")
    if auth not in {"oauth", "basic"}:
        raise ConfigurationError(
            "`auth` is `oauth` (an OAuth client, the default) or `basic` (an integration user)"
        )
    needed = "client_id" if auth == "oauth" else "username"
    if not str(cfg.get(needed) or "").strip():
        raise ConfigurationError(f"`auth: {auth}` needs `{needed}`")
    groups = cfg.get("assignment_groups")
    if (
        not isinstance(groups, list)
        or not groups
        or not all(isinstance(g, str) and g.strip() for g in groups)
    ):
        raise ConfigurationError(
            "`assignment_groups` names the groups whose open items Tranibor takes on (names or sys_ids)"
        )
    for group in groups:
        escape_value(group)
        if "," in group:
            raise ConfigurationError(f"an assignment group cannot hold a comma: {group!r}")
    tables = cfg.get("tables")
    if tables is not None and (
        not isinstance(tables, list) or not tables or any(t not in TRACKER_TABLES for t in tables)
    ):
        raise ConfigurationError(f"`tables` is a non-empty subset of {', '.join(TRACKER_TABLES)}")


def servicenow_egress(cfg: Mapping[str, Any]) -> list[tuple[str, int]]:
    """The instance's host and port, and nothing else: the token endpoint is on the instance too."""
    from tranibor_core.egress import host_port_of

    found = host_port_of(cfg.get("instance_url"))
    return [found] if found else []


def servicenow_client(cfg: Mapping[str, Any]) -> ServiceNowClient:
    """The client of one connector's configuration: an OAuth client (the default) or a basic user."""
    url = str(cfg["instance_url"]).rstrip("/")
    if cfg.get("auth", "oauth") == "basic":
        return ServiceNowClient(url, username=str(cfg["username"]), password=str(cfg.get("password") or ""))
    oauth = ServiceNowOAuth(str(cfg["client_id"]), str(cfg.get("client_secret") or ""))
    return ServiceNowClient(url, oauth=oauth)
