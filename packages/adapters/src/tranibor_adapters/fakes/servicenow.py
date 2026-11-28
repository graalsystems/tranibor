# SPDX-License-Identifier: Apache-2.0
"""A scriptable ServiceNow instance, as an `httpx` transport: no network (ADR 0228).

It answers the calls `ServiceNowTracker` makes, from an in-memory instance: the Table API
(`/api/now/table/<table>`: list with an encoded query, get, create, update), the OAuth token endpoint
(`/oauth_token.do`) and the Change Management API (`/api/sn_chg_rest/change/<sys_id>[/nextstates]`).
Ported from the `servicenow` plugin's fake (Apache-2.0), it checks what a real instance checks and a loose
mock does not: the credential (basic, or a bearer token it issued), the encoded query it parses (`^`,
`^OR`, `^^`, `IN`, `!=`, `ORDERBYDESC`, a dot-walk on a reference), `sysparm_fields` and
`sysparm_display_value=all`, a change's state refused on the Table API and moved only along its model.

Four switches play what a real instance does to a user without the read rights the procedure asks for
(each seen on an instance): `journal_readable = False` answers `sys_journal_field` with no row,
`choices_readable = False` refuses `sys_choice` (403), `unwritable` holds the `(table, field)` a write
keeps silently (200 and the old value), `change_api_readable = False` answers `available_states: [null]`.
It proves the tracker speaks the documented protocol; it does not prove a real instance accepts it.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs

import httpx

DATE = "%Y-%m-%d %H:%M:%S"
#: field -> the table it references (for display values and dot-walks).
REFERENCES = {
    "assignment_group": "sys_user_group",
    "assigned_to": "sys_user",
    "caller_id": "sys_user",
    "requested_for": "sys_user",
    "cmdb_ci": "cmdb_ci",
}
STATES: dict[str, list[tuple[str, str]]] = {
    "incident": [("1", "New"), ("2", "In Progress"), ("3", "On Hold"), ("6", "Resolved"), ("7", "Closed"),
                 ("8", "Canceled")],
    # A real instance holds the request item's states on `task`, none on `sc_req_item`.
    "task": [("-5", "Pending"), ("1", "Open"), ("2", "Work in Progress"), ("3", "Closed Complete"),
             ("4", "Closed Incomplete"), ("7", "Closed Skipped")],
    "change_request": [("-5", "New"), ("-4", "Assess"), ("-3", "Authorize"), ("-2", "Scheduled"),
                       ("-1", "Implement"), ("0", "Review"), ("3", "Closed"), ("4", "Canceled")],
}  # fmt: skip
#: A normal change's model: from each state, the states its transitions reach (the next one, or Canceled).
CHANGE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "-5": ("-4", "4"), "-4": ("-3", "4"), "-3": ("-2", "4"), "-2": ("-1", "4"), "-1": ("0", "4"),
    "0": ("3", "4"), "3": (), "4": (),
}  # fmt: skip
_PREFIX = {"incident": "INC", "sc_req_item": "RITM", "change_request": "CHG"}
_JOURNAL = ("work_notes", "comments")
_OPERATORS = ("NOT LIKE", "LIKE", "IN", "!=", ">=", "<=", "=", ">", "<")
USER_DISPLAY = "Tranibor integration"


def sys_id_of(name: str) -> str:
    """The deterministic sys_id the fake gives a named record."""
    return hashlib.sha256(name.encode()).hexdigest()[:32]


def _split(query: str) -> list[str]:
    """Split on `^`, a doubled `^^` being a literal caret inside a value."""
    parts, current, i = [], [], 0
    while i < len(query):
        if query[i] == "^" and query[i + 1 : i + 2] == "^":
            current.append("^")
            i += 2
        elif query[i] == "^":
            parts.append("".join(current))
            current, i = [], i + 1
        else:
            current.append(query[i])
            i += 1
    return [p for p in [*parts, "".join(current)] if p]


def _condition(segment: str) -> tuple[str, str, str]:
    i = 0
    while i < len(segment) and (segment[i].islower() or segment[i].isdigit() or segment[i] in "_."):
        i += 1
    for op in _OPERATORS:
        if segment[i:].startswith(op):
            return segment[:i], op, segment[i + len(op) :]
    raise ValueError(f"unparseable condition {segment!r}")


def _matches(actual: Any, op: str, expected: str) -> bool:
    text = "" if actual is None else str(actual)
    checks = {
        "=": lambda: text == expected,
        "!=": lambda: text != expected,
        "IN": lambda: text in expected.split(","),
        "LIKE": lambda: expected.lower() in text.lower(),
        "NOT LIKE": lambda: expected.lower() not in text.lower(),
    }
    return checks[op]() if op in checks else (text > expected if op == ">" else text < expected)


class FakeServiceNowInstance:
    """`transport()` backs the tracker's `httpx.AsyncClient`; `requests` keeps every request."""

    def __init__(
        self,
        *,
        username: str = "tranibor.integration",
        password: str = "fake-password",
        client_id: str = "fake-client",
        client_secret: str = "fake-client-secret",
        token_ttl_s: int = 1800,
    ) -> None:
        self.username, self.password = username, password
        self.client_id, self.client_secret = client_id, client_secret
        self.token_ttl_s = token_ttl_s
        self.tables: dict[str, dict[str, dict[str, Any]]] = {}
        self.tokens: dict[str, datetime] = {}
        self.requests: list[httpx.Request] = []
        self.journal_readable = True
        self.choices_readable = True
        self.change_api_readable = True
        self.unwritable: set[tuple[str, str]] = set()
        self._numbers = itertools.count(10001)
        self._clock = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
        for name, choices in STATES.items():
            for seq, (value, label) in enumerate(choices):
                self.insert(
                    "sys_choice",
                    {
                        "name": name,
                        "element": "state",
                        "value": value,
                        "label": label,
                        "sequence": str(seq),
                        "inactive": "false",
                        "language": "en",
                    },
                )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.__call__)

    # ───────────────────────── seeding ─────────────────────────

    def now(self) -> str:
        """A clock that moves one minute per write, so `ORDER BY sys_updated_on` is deterministic."""
        self._clock += timedelta(minutes=1)
        return self._clock.strftime(DATE)

    def insert(self, table: str, record: dict[str, Any]) -> dict[str, Any]:
        record.setdefault("sys_id", secrets.token_hex(16))
        record.setdefault("sys_created_on", self.now())
        record.setdefault("sys_updated_on", record["sys_created_on"])
        record.setdefault("sys_mod_count", "0")
        self.tables.setdefault(table, {})[record["sys_id"]] = record
        return record

    def group(self, name: str) -> str:
        """An assignment group, created once; its sys_id."""
        sys_id = sys_id_of(f"group:{name}")
        self.tables.setdefault("sys_user_group", {}).setdefault(sys_id, {"sys_id": sys_id, "name": name})
        return sys_id

    def user(self, name: str) -> str:
        sys_id = sys_id_of(f"user:{name}")
        self.tables.setdefault("sys_user", {}).setdefault(sys_id, {"sys_id": sys_id, "name": name})
        return sys_id

    def seed(
        self, table: str, title: str, *, state: str = "1", group: str | None = "Service Desk", **fields: Any
    ) -> str:
        """A record of a tracker table, its number returned."""
        record = {
            "number": f"{_PREFIX[table]}{next(self._numbers):07d}",
            "short_description": title,
            "state": state,
            "active": "true",
            "impact": "3",
            "urgency": "3",
            "priority": "5",
            **{k: str(v) for k, v in fields.items()},
        }
        if group:
            record["assignment_group"] = self.group(group)
        if table == "incident" and state in {"7", "8"}:
            record["active"] = "false"
        return str(self.insert(table, record)["number"])

    def by_number(self, number: str) -> dict[str, Any]:
        for table in _PREFIX:
            for row in self.tables.get(table, {}).values():
                if row.get("number") == number:
                    return row
        raise KeyError(number)

    def journal(self, number: str, element: str = "work_notes") -> list[str]:
        """The values of a record's journal entries, oldest first."""
        sys_id = self.by_number(number)["sys_id"]
        rows = [r for r in self.tables.get("sys_journal_field", {}).values() if r["element_id"] == sys_id]
        return [
            r["value"] for r in sorted(rows, key=lambda r: r["sys_created_on"]) if r["element"] == element
        ]

    # ───────────────────────── HTTP ─────────────────────────

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/oauth_token.do" and request.method == "POST":
            return self._token(request)
        if not self._authorised(request):
            return httpx.Response(401, json={"error": {"message": "User Not Authenticated"}})
        parts = path.strip("/").split("/")
        if parts[:3] == ["api", "sn_chg_rest", "change"]:
            return self._change_api(request, parts[3:])
        if parts[:3] != ["api", "now", "table"] or len(parts) not in (4, 5):
            return httpx.Response(404, json={"error": {"message": "No such resource"}})
        table, sys_id = parts[3], parts[4] if len(parts) == 5 else None
        params = dict(request.url.params.items())
        if table == "sys_choice" and not self.choices_readable:
            return httpx.Response(403, json={"error": {"message": "Insufficient rights to query records"}})
        body = json.loads(request.content or b"{}") if request.method in {"POST", "PATCH"} else {}
        if request.method == "GET" and sys_id is None:
            return self._list(table, params)
        row = self.tables.get(table, {}).get(sys_id or "")
        if request.method == "POST" and sys_id is None:
            return self._create(table, body, params)
        if row is None:
            return httpx.Response(404, json={"error": {"message": "No Record found"}})
        if request.method == "GET":
            return httpx.Response(200, json={"result": self._render(table, row, params)})
        if request.method == "PATCH":
            if table == "change_request" and "state" in body and str(body["state"]) != row.get("state"):
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "message": "Operation Failed",
                            "detail": "aborted by Business Rule 'Change Model: Check State Transition'",
                        }
                    },
                )
            return self._write(table, row, body, params)
        return httpx.Response(405, json={"error": {"message": "Method not allowed"}})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
            return httpx.Response(401, json={"error": "access_denied"})
        token = secrets.token_hex(16)
        self.tokens[token] = datetime.now(UTC) + timedelta(seconds=self.token_ttl_s)
        return httpx.Response(200, json={"access_token": token, "expires_in": self.token_ttl_s})

    def _authorised(self, request: httpx.Request) -> bool:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            expiry = self.tokens.get(header.removeprefix("Bearer "))
            return expiry is not None and expiry > datetime.now(UTC)
        expected = "Basic " + base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return bool(header == expected)

    def _value(self, row: dict[str, Any], name: str) -> Any:
        if "." in name:
            head, tail = name.split(".", 1)
            ref = self.tables.get(REFERENCES.get(head, ""), {}).get(str(row.get(head, "")))
            return None if ref is None else ref.get(tail, "")
        return row.get(name, "")

    def _list(self, table: str, params: dict[str, str]) -> httpx.Response:
        if table == "sys_journal_field" and not self.journal_readable:
            return httpx.Response(200, json={"result": []}, headers={"X-Total-Count": "0"})
        groups: list[list[tuple[str, str, str]]] = []
        order: list[str] = []
        try:
            for segment in _split(params.get("sysparm_query", "")):
                if segment.startswith("ORDERBYDESC"):
                    order.append(segment.removeprefix("ORDERBYDESC"))
                elif segment.startswith("OR") and segment[2:3].islower() and groups:
                    groups[-1].append(_condition(segment[2:]))
                else:
                    groups.append([_condition(segment)])
        except ValueError as error:
            return httpx.Response(400, json={"error": {"message": str(error)}})
        rows = [
            r
            for r in self.tables.get(table, {}).values()
            if all(any(_matches(self._value(r, f), op, v) for f, op, v in g) for g in groups)
        ]
        for name in reversed(order):

            def sort_key(row: dict[str, Any], field: str = name) -> str:
                return str(self._value(row, field) or "")

            rows.sort(key=sort_key, reverse=True)
        limit, offset = int(params.get("sysparm_limit", "10000")), int(params.get("sysparm_offset", "0"))
        result = [self._render(table, r, params) for r in rows[offset : offset + limit]]
        return httpx.Response(200, json={"result": result}, headers={"X-Total-Count": str(len(rows))})

    def _create(self, table: str, body: dict[str, Any], params: dict[str, str]) -> httpx.Response:
        if table not in _PREFIX:
            return httpx.Response(403, json={"error": {"message": f"cannot create in {table}"}})
        journal = {k: body.pop(k) for k in _JOURNAL if k in body}
        record = {k: str(v) for k, v in body.items() if (table, k) not in self.unwritable}
        record.update(number=f"{_PREFIX[table]}{next(self._numbers):07d}", active="true", state="1")
        row = self.insert(table, record)
        self._journal(table, row, journal)
        return httpx.Response(201, json={"result": self._render(table, row, params)})

    def _write(
        self, table: str, row: dict[str, Any], body: dict[str, Any], params: dict[str, str]
    ) -> httpx.Response:
        journal = {k: body.pop(k) for k in _JOURNAL if k in body}
        row.update({k: str(v) for k, v in body.items() if (table, k) not in self.unwritable})
        if table == "incident":
            row["active"] = "false" if row.get("state") in {"7", "8"} else "true"
        if table in CHANGE_TRANSITIONS and row.get("state") in {"3", "4"}:
            row["active"] = "false"
        row["sys_mod_count"] = str(int(row.get("sys_mod_count", "0")) + 1)
        row["sys_updated_on"] = self.now()
        self._journal(table, row, journal)
        return httpx.Response(200, json={"result": self._render(table, row, params)})

    def _change_api(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        row = self.tables.get("change_request", {}).get(parts[0] if parts else "")
        if row is None:
            return httpx.Response(404, json={"error": {"message": "No Record found"}})
        reachable = CHANGE_TRANSITIONS.get(str(row.get("state")), ())
        if request.method == "GET" and parts[1:] == ["nextstates"]:
            if not self.change_api_readable:
                return httpx.Response(200, json={"result": {"available_states": [None], "state_label": {}}})
            labels = dict(STATES["change_request"])
            states = [str(row.get("state")), *reachable]
            return httpx.Response(
                200,
                json={"result": {"available_states": states, "state_label": {s: labels[s] for s in states}}},
            )
        if request.method == "PATCH" and len(parts) == 1:
            body = json.loads(request.content or b"{}")
            if (
                "state" in body
                and str(body["state"]) != row.get("state")
                and str(body["state"]) not in reachable
            ):
                return httpx.Response(
                    403,
                    json={
                        "error": {
                            "message": "Operation Failed",
                            "detail": "Change Model: Check State Transition",
                        }
                    },
                )
            return self._write("change_request", row, body, {"sysparm_display_value": "all"})
        return httpx.Response(405, json={"error": {"message": "Method not allowed"}})

    def _journal(self, table: str, row: dict[str, Any], entries: dict[str, Any]) -> None:
        for element, value in entries.items():
            if value not in (None, ""):
                self.insert(
                    "sys_journal_field",
                    {
                        "element_id": row["sys_id"],
                        "name": table,
                        "element": element,
                        "value": str(value),
                        "sys_created_by": self.username,
                    },
                )

    def _display(self, table: str, row: dict[str, Any], name: str) -> Any:
        value = self._value(row, name)
        if name in _JOURNAL:
            entries = [
                e
                for e in self.tables.get("sys_journal_field", {}).values()
                if e["element_id"] == row["sys_id"] and e["element"] == name
            ]
            label = {"work_notes": "Work notes", "comments": "Comments"}[name]
            entries.sort(key=lambda e: str(e["sys_created_on"]), reverse=True)
            return "".join(
                f"{e['sys_created_on']} - {USER_DISPLAY} ({label})\n{e['value']}\n\n" for e in entries
            )
        if name in REFERENCES and value:
            ref = self.tables.get(REFERENCES[name], {}).get(str(value))
            return "" if ref is None else ref.get("name", "")
        if name == "state":
            owner = table if table in STATES else "task"
            return dict(STATES[owner]).get(str(value), value)
        return value

    def _render(self, table: str, row: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
        fields = [f for f in params.get("sysparm_fields", "").split(",") if f] or list(row)
        if params.get("sysparm_display_value") != "all":
            return {name: self._value(row, name) for name in fields}
        return {
            name: {
                "value": "" if name in _JOURNAL else (self._value(row, name) or ""),
                "display_value": self._display(table, row, name) or "",
            }
            for name in fields
        }


__all__ = ["CHANGE_TRANSITIONS", "STATES", "FakeServiceNowInstance", "sys_id_of"]
