# SPDX-License-Identifier: Apache-2.0
"""The ServiceNow TrackerAdapter: incidents, request items and changes as work items (ADR 0228).

Ported from the `servicenow` plugin's server (`tranibor-agents/plugins/servicenow/server`, Apache-2.0),
whose tracker profile a project could only reach where the plugin's server runs (Kubernetes, ADR 0096).
Here the platform calls the instance itself, like Jira or Azure Boards. What it keeps of the plugin:

* an item's key is its **number** (`INC0010001`, `RITM0010001`, `CHG0010001`);
* everything Tranibor writes is a **work note** (internal), never a customer-visible comment: a reply
  the caller reads stays the plugin's governed tool, which a person approves;
* `priority` is never written: ServiceNow computes it from impact and urgency;
* `create_item` files an **incident**, once per text: the record carries `correlation_id = tranibor:<key>`
  and a replay finds it instead of filing another;
* a state is designated by its **label** (`In Progress`), matched case-blind against the table's states
  (`sys_choice`); a closing state is refused there, since closing is `close_item`'s, with its close code;
* a change request moves only through the Change Management API, to a state its model reaches;
* the poll's candidates are the open items of the configured **assignment groups**, newest first.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from tranibor_contracts import InboundEvent, ProjectConfig, wire
from tranibor_core.domain import Comment, NewItem, TrackerStateMapping, WorkItemData

from ..errors import ConfigurationError, UpstreamError
from .fields import COST, FORMER_NAMES, RISK, RUN, SIZE, current_name
from .servicenow_api import (
    CLOSED_STATES,
    REFERENCE_FIELDS,
    TRACKER_TABLES,
    UPDATABLE_FIELDS,
    Condition,
    Query,
    ServiceNowClient,
    eq,
    is_sys_id,
    table_of_number,
)
from .servicenow_events import parse_servicenow_event, verify_servicenow_delivery
from .ticket_fields import TRACKER_FIELD, answers, named_person, tracker_block

log = structlog.get_logger(__name__)

#: `[tranibor:status]`: a work note is plain text, so the marker is not an HTML comment.
STATUS_COMMENT_MARKER = wire.marker("status", html=False)
#: What `close_item` writes as the close notes: the delivered item's review already happened.
CLOSE_NOTES = "Closed by Tranibor: the work item was delivered."
#: The most keys one poll answers, across the tables.
CANDIDATES_BOUND = 200
#: The journal entries read at most for one item.
JOURNAL_BOUND = 200
#: How each table is closed: the state label, and the close code ServiceNow asks for.
_CLOSE: dict[str, tuple[str, str | None]] = {
    "incident": ("Resolved", "Solution provided"),
    "sc_req_item": ("Closed Complete", None),
    "change_request": ("Closed", "successful"),
}
#: The person who asked, per table.
_REQUESTER = {"incident": "caller_id", "sc_req_item": "requested_for"}
#: Shown to the agents as the item's answers, by their ServiceNow names.
_SHOWN = (
    "priority",
    "impact",
    "urgency",
    "category",
    "subcategory",
    "assignment_group",
    "assigned_to",
    "cmdb_ci",
)
#: The fields the workflow's bookkeeping writes in a board (cost, size, risk, run): ServiceNow has no place
#: for them, so the status work note carries them and `set_fields` skips them.
_BOOKKEEPING = frozenset({COST, SIZE, RISK, RUN, *FORMER_NAMES.values()})
_LINK_WORDS = {"origin": "Originates from", "duplicate": "Duplicate of", "blocks": "Blocks"}
_DATE = "%Y-%m-%d %H:%M:%S"
_HTML_MARKER = re.compile(r"^<!--\s*([a-z]+):([A-Za-z0-9._:-]{1,80})\s*-->$")
_ANY_MARKER = re.compile(rf"\[(?:{wire.NAME}|{wire.FORMER_NAME}):[A-Za-z0-9._:-]{{1,80}}\]")
_JOURNAL_HEADER = re.compile(
    r"^(?P<on>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - (?P<by>[^\n]*?) "
    r"\((?P<kind>Work notes|Comments|Additional comments)\)$",
    re.MULTILINE,
)
_JOURNAL_KIND = {"work_notes": ("Work notes",), "comments": ("Comments", "Additional comments")}


class ServiceNowTracker:
    """A ServiceNow instance's incidents, request items and changes, as a `TrackerAdapter`."""

    owns_items = True

    def __init__(
        self,
        client: ServiceNowClient,
        *,
        assignment_groups: list[str] | tuple[str, ...],
        tables: list[str] | tuple[str, ...] | None = None,
        webhook_secret: str = "",
    ) -> None:
        self.client = client
        self.assignment_groups = tuple(g.strip() for g in assignment_groups if g.strip())
        self.tables = tuple(tables) if tables else TRACKER_TABLES
        unknown = sorted(set(self.tables) - set(TRACKER_TABLES))
        if unknown:
            raise ConfigurationError(
                f"`tables` names tables the tracker does not serve: {', '.join(unknown)}"
            )
        self.webhook_secret = webhook_secret

    # ───────────────────────── reading ─────────────────────────

    async def _item(self, key: str) -> tuple[str, dict[str, Any]]:
        table = table_of_number(key)
        row = await self.client.find_one(Query(table).where(eq("number", key)))
        if row is None:
            raise UpstreamError("servicenow", f"404: no {table} numbered {key}", status_code=404)
        return table, row

    def item_url(self, table: str, sys_id: str) -> str:
        return f"{self.client.base_url}/nav_to.do?uri={table}.do?sys_id={sys_id}"

    async def fetch_item(self, key: str) -> WorkItemData:
        table, row = await self._item(key)
        f, d = row["fields"], row["display"]
        requester = _REQUESTER.get(table)
        author = d.get(requester) if requester else None
        shown = [(name, d.get(name, f.get(name))) for name in _SHOWN if name in f]
        journal = await self._journal(table, str(f["sys_id"]), ("work_notes", "comments"))
        return WorkItemData(
            key=key,
            title=str(f.get("short_description") or ""),
            body=str(f.get("description") or ""),
            url=self.item_url(table, str(f["sys_id"])),
            state=d.get("state", f.get("state")),
            labels=[],
            item_type=table,
            assignees=[str(d["assigned_to"])] if d.get("assigned_to") else [],
            author=str(author) if author else None,
            created_at=_when(f.get("sys_created_on")),
            updated_at=_when(f.get("sys_updated_on")),
            closed_at=_when(f.get("closed_at")),
            fields={
                "servicenow": {"table": table, "sys_id": f["sys_id"], "state_value": f.get("state")},
                TRACKER_FIELD: tracker_block(
                    kind="servicenow",
                    key=key,
                    reporter=named_person(author),
                    creator=named_person(None),
                    labels=[],
                    answers=answers(shown),
                ),
            },
            comments=[_comment(entry) for entry in reversed(journal)],
        )

    async def list_candidates(self, project: ProjectConfig) -> list[str]:
        """The open items of the configured assignment groups (names or sys_ids), the most recently
        updated first across the tables, at most `CANDIDATES_BOUND`. An incident stays `active` once
        resolved: the closing states are left out by the query."""
        if not self.assignment_groups:
            return []
        ids = tuple(g for g in self.assignment_groups if is_sys_id(g))
        names = tuple(g for g in self.assignment_groups if not is_sys_id(g))
        found: list[dict[str, Any]] = []
        for table in self.tables:
            alternatives = [Condition("assignment_group", ids, "in")] if ids else []
            alternatives += [Condition("assignment_group.name", names, "in")] if names else []
            open_only = [Condition("state", value, "ne") for value in sorted(CLOSED_STATES[table])]
            query = Query(table).where(eq("active", "true"), *open_only).any_of(*alternatives)
            query.newest_first("sys_updated_on")
            found += await self.client.read(
                query, fields=("number", "sys_updated_on"), bound=CANDIDATES_BOUND
            )
        found.sort(key=lambda r: str(r["fields"].get("sys_updated_on") or ""), reverse=True)
        return [str(r["fields"]["number"]) for r in found[:CANDIDATES_BOUND]]

    async def _journal(self, table: str, sys_id: str, elements: tuple[str, ...]) -> list[dict[str, Any]]:
        """The record's journal, newest first, from `sys_journal_field`; or, when that table answers
        nothing (a user without its read right gets 200 and no row) or refuses, from the record's own
        rendered journal fields, which a user who reads the record reads too. Trusting an empty answer
        would write the same note twice."""
        query = Query("sys_journal_field").where(
            eq("element_id", sys_id), eq("name", table), Condition("element", elements, "in")
        )
        query.newest_first("sys_created_on")
        try:
            rows = await self.client.read(query, bound=JOURNAL_BOUND)
        except UpstreamError as error:
            if error.status_code != 403:
                raise
            rows = []
        if rows:
            return [
                {
                    "id": str(r["fields"].get("sys_id")),
                    "element": str(r["fields"].get("element")),
                    "value": str(r["fields"].get("value") or ""),
                    "on": r["fields"].get("sys_created_on"),
                    "by": str(r["fields"].get("sys_created_by") or ""),
                }
                for r in rows
            ]
        record = await self.client.get(table, sys_id, fields=(*elements, "sys_updated_on"))
        offset = _offset(record["fields"].get("sys_updated_on"), record["display"].get("sys_updated_on"))
        entries: list[dict[str, Any]] = []
        for element in elements:
            entries += _rendered(sys_id, element, str(record["display"].get(element) or ""), offset)
        return sorted(entries, key=lambda e: str(e["on"] or ""), reverse=True)

    # ───────────────────────── writing ─────────────────────────

    async def create_item(self, data: NewItem) -> str:
        """Files an incident, the only table a tracker creates in, idempotent by `correlation_id`. Of
        `data.fields`, only ServiceNow's own creatable fields are written (`priority` never); the others (a
        board's `issue_type`) mean nothing here and are left out."""
        if not data.title.strip():
            raise ConfigurationError("a ServiceNow incident needs a title")
        correlation = f"{wire.NAME}:{_digest(data.title, data.body)}"
        existing = await self.client.find_one(Query("incident").where(eq("correlation_id", correlation)))
        if existing is not None:
            return str(existing["fields"]["number"])
        extra = {
            name: str(value)
            for name, value in data.fields.items()
            if name in UPDATABLE_FIELDS | {"caller_id"} and value not in (None, "")
        }
        for name, value in extra.items():
            if name in REFERENCE_FIELDS and not is_sys_id(value):
                raise ConfigurationError(f"`{name}` takes a sys_id")
        values = {"short_description": data.title, "description": data.body, "correlation_id": correlation}
        record = await self.client.create("incident", {**values, **extra})
        if record["fields"].get("correlation_id") != correlation:
            # A user that cannot create the field gets 200 and no correlation id: the record exists, and a
            # replay would file another. Raising would make the caller retry and file one more each time.
            log.warning("servicenow.correlation_id_not_kept", number=record["fields"].get("number"))
        return str(record["fields"]["number"])

    async def set_state(self, key: str, state_mapping: TrackerStateMapping) -> None:
        """The DSL state's `status` is a state label of the item's table. ServiceNow has no labels: the
        mapping's `label` and `remove_labels` are said in the log, not written."""
        if state_mapping.label or state_mapping.remove_labels:
            log.debug("servicenow.labels_skipped", key=key)
        status = (state_mapping.status or "").strip()
        if not status:
            return
        table, row = await self._item(key)
        choices = await self.client.choices(table)
        value = {label.casefold(): v for v, label in choices.items()}.get(status.casefold())
        if value is None:
            states = ", ".join(choices.values()) or "none"
            raise ConfigurationError(f'"{status}" is not a state of {table} (states: {states})')
        if value in CLOSED_STATES[table]:
            raise ConfigurationError(
                f'"{status}" closes {key}: a ServiceNow item is closed by the workflow\'s close step, '
                "with its close code, never by a state"
            )
        if str(row["fields"].get("state")) == value:
            return  # a replayed activity changes nothing (ADR 0008)
        if table == "change_request":
            await self._move_change(row, value, choices[value], {})
        else:
            await self.client.update(table, str(row["sys_id"]), {"state": value})

    async def _move_change(self, row: dict[str, Any], value: str, label: str, also: dict[str, Any]) -> None:
        """Through the Change Management API, to a state the change's model reaches from its current one;
        any other is refused before anything is written, naming those it reaches."""
        current = str(row["fields"].get("state"))
        reachable = await self.client.change_next_states(str(row["sys_id"]), current)
        if value not in reachable:
            here = row["display"].get("state", current)
            said = ", ".join(reachable.values()) or "none"
            raise ConfigurationError(
                f'"{label}" is not reachable from "{here}" on this change\'s model: {said}'
            )
        await self.client.update_change(str(row["sys_id"]), {"state": value, **also})

    async def upsert_status_comment(
        self, key: str, markdown: str, marker: str = STATUS_COMMENT_MARKER
    ) -> None:
        """A journal is append-only: a new status work note is written only when its text changed."""
        plain = plain_marker(marker)
        body = markdown if plain in markdown or marker in markdown else f"{plain}\n{markdown}"
        table, row = await self._item(key)
        notes = await self._journal(table, str(row["sys_id"]), ("work_notes",))
        latest = next(
            (n for n in notes if wire.carries_marker(n["value"], plain) or marker in n["value"]), None
        )
        if latest is not None and latest["value"].strip() == body.strip():
            return
        await self.client.update(table, str(row["sys_id"]), {"work_notes": body})

    async def close_item(self, key: str) -> None:
        """Resolves an incident (close code and notes), closes a request item complete (a work note says
        why), closes a change through its model. Closing a closed item does nothing."""
        table, row = await self._item(key)
        if str(row["fields"].get("state")) in CLOSED_STATES[table]:
            return
        label, close_code = _CLOSE[table]
        choices = await self.client.choices(table)
        value = {lab.casefold(): v for v, lab in choices.items()}.get(label.casefold())
        if value is None:
            raise ConfigurationError(f'{table} has no state "{label}" on this instance')
        extra: dict[str, Any] = (
            {"close_code": close_code, "close_notes": CLOSE_NOTES}
            if close_code
            else {"work_notes": CLOSE_NOTES}
        )
        if table == "change_request":
            await self._move_change(row, value, label, extra)
            return
        await self.client.update(table, str(row["sys_id"]), {"state": value, **extra})

    async def comment(self, key: str, markdown: str) -> str:
        """A work note, never a customer-visible comment. It ends with a marker of its text, and a note that
        carries it already is not written again (a replayed activity, ADR 0008); the marker is its id."""
        if not markdown.strip():
            raise ConfigurationError("a work note cannot be empty")
        table, row = await self._item(key)
        sys_id = str(row["sys_id"])
        marker = wire.marker(f"note:{_digest(table, sys_id, markdown)}", html=False)
        notes = await self._journal(table, sys_id, ("work_notes",))
        if not any(wire.carries_marker(n["value"], marker) for n in notes):
            await self.client.update(table, sys_id, {"work_notes": f"{markdown}\n\n{marker}"})
        return marker

    async def link(self, a: str, b: str, relation: Literal["origin", "duplicate", "blocks"]) -> None:
        """ServiceNow has no generic link between an incident, a request item and a change (each relation is
        a field of its own, such as `parent_incident` or `rfc`): the link is a work note on `a` naming `b`."""
        await self.comment(a, f"{_LINK_WORDS[relation]} {b} (recorded by Tranibor).")

    async def set_fields(self, key: str, fields: dict[str, Any]) -> None:
        """Writes ServiceNow's updatable fields (impact, urgency, category, subcategory, assignment_group,
        assigned_to, cmdb_ci). The bookkeeping fields Tranibor writes in every board (cost, size, risk, run)
        have no place here and are skipped: the status work note carries them. `priority`, and any other
        field, is refused."""
        writes: dict[str, str] = {}
        for name, raw in fields.items():
            if current_name(name) in _BOOKKEEPING:
                continue
            if name == "priority":
                raise ConfigurationError(
                    "priority is never written: ServiceNow computes it from impact and urgency"
                )
            if name not in UPDATABLE_FIELDS:
                allowed = ", ".join(sorted(UPDATABLE_FIELDS))
                raise ConfigurationError(f"`{name}` is not a field the tracker writes (allowed: {allowed})")
            value = "" if raw is None else str(raw)
            if name in {"impact", "urgency"} and value not in {"1", "2", "3"}:
                raise ConfigurationError(f"`{name}` is 1 (high), 2 (medium) or 3 (low)")
            if name in REFERENCE_FIELDS and value and not is_sys_id(value):
                raise ConfigurationError(f"`{name}` takes a sys_id (or nothing, to clear it)")
            writes[name] = value
        if not writes:
            return
        table, row = await self._item(key)
        await self.client.update(table, str(row["sys_id"]), writes)

    # ───────────────────────── test ─────────────────────────

    async def test(self) -> dict[str, Any]:
        """The connector's **Test** button: the credential reads the first table served and its states."""
        table = self.tables[0]
        try:
            await self.client.page(Query(table).where(eq("active", "true")), fields=("number",), limit=1)
            states = await self.client.choices(table)
        except UpstreamError as error:
            return {"ok": False, "reason": _refusal(error.status_code, table)}
        return {"ok": True, "table": table, "states": list(states.values())}

    # ───────────────────────── webhooks ─────────────────────────

    def parse_webhook(self, headers: Mapping[str, str], body: bytes) -> list[InboundEvent]:
        return parse_servicenow_event(body, tables=self.tables)

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> bool:
        """The Business Rule's `X-Tranibor-Secret`, compared in constant time with `webhook_secret`."""
        return verify_servicenow_delivery(headers, self.webhook_secret)


def plain_marker(marker: str) -> str:
    """`<!-- tranibor:status -->` as `[tranibor:status]`: a work note is plain text, where an HTML comment
    would show as written."""
    found = _HTML_MARKER.match(marker.strip())
    return f"[{found.group(1)}:{found.group(2)}]" if found else marker


def _refusal(status_code: int | None, table: str) -> str:
    if status_code == 401:
        return (
            "401: the instance refused the credential (a wrong password or client secret, or a locked user)"
        )
    if status_code == 403:
        return (
            f"403: the integration user cannot read {table} or sys_choice: give it `itil` and the read rights"
        )
    if status_code == 404:
        return "404: no ServiceNow Table API at this address: check `instance_url`"
    return f"{status_code}: ServiceNow refused to read {table}"


def _digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _when(value: Any) -> datetime | None:
    """`2026-10-27 08:00:00` (ServiceNow's stored value, UTC) as a datetime."""
    try:
        return datetime.strptime(str(value or ""), _DATE).replace(tzinfo=UTC)
    except ValueError:
        return None


def _comment(entry: Mapping[str, Any]) -> Comment:
    found = _ANY_MARKER.search(entry["value"])
    marker = wire.marker_twins(found.group(0))[0] if found else None
    when = _when(entry["on"])
    return Comment(
        id=entry["id"],
        author=entry["by"],
        body=entry["value"],
        marker=marker,
        **({"ts": when} if when else {}),
    )


def _offset(value: Any, label: Any) -> timedelta | None:
    """The reading user's offset from UTC: a date's label minus its stored (UTC) value."""
    try:
        return datetime.strptime(str(label), _DATE) - datetime.strptime(str(value), _DATE)
    except ValueError:
        return None


def _rendered(sys_id: str, element: str, text: str, offset: timedelta | None) -> list[dict[str, Any]]:
    """The entries of a rendered journal field (`<date> - <author> (Work notes)`, then the text). Without a
    header the whole text is one entry: the markers it holds are still found."""
    if not text.strip():
        return []
    headers = [m for m in _JOURNAL_HEADER.finditer(text) if m.group("kind") in _JOURNAL_KIND[element]]
    if not headers:
        return [{"id": f"journal:{_digest(sys_id, element, text)}", "element": element, "value": text.strip(),
                 "on": None, "by": ""}]  # fmt: skip
    entries = []
    for i, header in enumerate(headers):
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        value = text[header.end() : end].strip("\n").rstrip()
        on = header.group("on")
        if offset is not None:
            on = (datetime.strptime(on, _DATE) - offset).strftime(_DATE)
        entry_id = f"journal:{_digest(sys_id, element, on, value)}"
        entries.append(
            {"id": entry_id, "element": element, "value": value, "on": on, "by": header.group("by")}
        )
    return entries


__all__ = ["CANDIDATES_BOUND", "CLOSE_NOTES", "STATUS_COMMENT_MARKER", "ServiceNowTracker", "plain_marker"]
