# SPDX-License-Identifier: Apache-2.0
"""Profiles a plugin declares and the servers that implement them (ADR 0224, S86-02).

A profile is a contract of tools (`plugin-profile.schema.json`). The core treats it as opaque: it checks
its shape, and proves that a server which says `implements: [<plugin>/<name>@<version>]` keeps it. Nothing
here reads what a profile means (ADR 0181).

The proof is structural, on the closed subset of JSON Schema the meta-schema lists. A schema A is ACCEPTED
BY a schema B when every instance valid under A is valid under B; the comparison is sound, never complete:
when it cannot show that A is inside B it says so, it does not guess. A keyword outside the subset in a
server's schema makes the comparison fail."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from tranibor_contracts import load_schema

#: `<plugin>/<name>@<version>`, as `plugin-server.schema.json` writes it.
PROFILE_NAME = re.compile(r"^([a-z0-9][a-z0-9-]{0,62})/([a-z][a-z0-9-]{0,31})@([1-9][0-9]{0,8})$")

_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")
_REF = re.compile(r"^#/\$defs/([A-Za-z_][A-Za-z0-9_-]{0,63})$")


def profile_name(plugin: str, doc: Mapping[str, Any]) -> str:
    """The identity of a profile document of `plugin`: `<plugin>/<name>@<version>`."""
    return f"{plugin}/{doc['name']}@{doc['version']}"


def _meta() -> dict[str, Any]:
    return load_schema("plugin-profile.schema.json")


def _subset_validator() -> Draft202012Validator:
    meta = _meta()
    return Draft202012Validator({"$ref": "#/$defs/schema", "$defs": meta["$defs"]})


def subset_problems(schema: Any, where: str) -> list[str]:
    """What keeps `schema` outside the subset (a server's `input_schema`, written like a profile's: its
    `$defs` at the root). Empty when it is inside."""
    if not isinstance(schema, dict):
        return [f"{where}: a schema is a mapping"]
    body = {k: v for k, v in schema.items() if k != "$defs"}
    defs = schema.get("$defs") or {}
    if not isinstance(defs, dict):
        return [f"{where}: `$defs` is a mapping"]
    validator = _subset_validator()
    out: list[str] = []
    for label, part in [("", body), *((f"/$defs/{k}", v) for k, v in defs.items())]:
        for error in sorted(validator.iter_errors(part), key=lambda e: list(map(str, e.absolute_path))):
            at = "/".join(str(p) for p in error.absolute_path)
            out.append(f"{where}{label}{':' + at if at else ''}: {error.message}")
    out.extend(_ref_problems(body, defs, where))
    return out


def _refs_of(schema: Any) -> list[str]:
    """Every `$ref` under `schema`, through `properties`, `items` and `additionalProperties`."""
    if not isinstance(schema, dict):
        return []
    found = [schema["$ref"]] if isinstance(schema.get("$ref"), str) else []
    properties = schema.get("properties")
    for sub in properties.values() if isinstance(properties, dict) else []:
        found.extend(_refs_of(sub))
    for key in ("items", "additionalProperties"):
        found.extend(_refs_of(schema.get(key)))
    return found


def _ref_problems(body: Any, defs: Mapping[str, Any], where: str) -> list[str]:
    """A `$ref` names a `$defs` entry of the same document, and no definition reaches itself."""
    out: list[str] = []
    for ref in _refs_of(body):
        match = _REF.match(ref)
        if match is None or match.group(1) not in defs:
            out.append(f"{where}: `$ref` {ref!r} names no `#/$defs/<name>` of the document")
    graph = {k: [m.group(1) for r in _refs_of(v) if (m := _REF.match(r))] for k, v in defs.items()}
    for start in sorted(graph):
        seen: set[str] = set()
        stack = list(graph[start])
        while stack:
            current = stack.pop()
            if current == start:
                out.append(f"{where}/$defs/{start}: reaches itself through `$ref` (no cycle in a profile)")
                break
            if current in seen or current not in graph:
                continue
            seen.add(current)
            stack.extend(graph[current])
    return out


def profile_problems(doc: Any, ref: str) -> list[str]:
    """A profile file: the meta-schema (which holds the subset), then what a schema cannot say: unique tool
    names, `$ref`s that resolve, no cycle."""
    out = [
        f"{ref}{':' + at if at else ''}: {message}"
        for at, message in (
            ("/".join(str(p) for p in e.absolute_path), e.message)
            for e in sorted(
                Draft202012Validator(_meta()).iter_errors(doc), key=lambda e: list(map(str, e.absolute_path))
            )
        )
    ]
    if out or not isinstance(doc, dict):
        return out
    names = [str(t["name"]) for t in doc["tools"]]
    out.extend(
        f"{ref}: tool `{n}` is declared twice" for n in sorted({n for n in names if names.count(n) > 1})
    )
    defs = doc.get("$defs") or {}
    for tool in doc["tools"]:
        for side in ("input", "output"):
            out.extend(_ref_problems(tool[side], defs, f"{ref}:tools/{tool['name']}/{side}"))
    out.extend(_ref_problems({}, defs, ref))  # a cycle among the definitions
    return out


# -- comparison -------------------------------------------------------------------------------------------


class _Side:
    """A schema and the definitions its `$ref`s resolve against."""

    def __init__(self, schema: Mapping[str, Any], defs: Mapping[str, Any], label: str) -> None:
        self.schema, self.defs, self.label = schema, defs, label

    def resolved(self) -> tuple[Mapping[str, Any], str | None]:
        """The schema with a `$ref` followed (one step at a time, the subset allows no sibling but prose)."""
        schema, label = self.schema, None
        for _ in range(64):
            ref = schema.get("$ref")
            if not isinstance(ref, str):
                return schema, label
            match = _REF.match(ref)
            target = self.defs.get(match.group(1)) if match else None
            if not isinstance(target, dict):
                return {}, ref  # unresolvable: `_ref_problems` already said so
            schema, label = target, ref
        return {}, label

    def sub(self, schema: Any) -> _Side:
        return _Side(schema if isinstance(schema, dict) else {}, self.defs, self.label)


def _types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if declared is None:
        return set(_TYPES)
    return {declared} if isinstance(declared, str) else set(declared)


def _finite(schema: Mapping[str, Any]) -> list[Any] | None:
    if "const" in schema:
        return [schema["const"]]
    if "enum" in schema:
        return list(schema["enum"])
    return None


def _valid(value: Any, side: _Side) -> bool:
    return Draft202012Validator({"$defs": dict(side.defs), **dict(side.schema)}).is_valid(value)


def _bounds(a: _Side, b: _Side, here: str, keywords: tuple[tuple[str, bool], ...], out: list[str]) -> None:
    """A bound `b` sets is kept by `a` when `a` sets one at least as tight; `True` marks a lower bound."""
    for keyword, lower in keywords:
        if keyword not in b.schema:
            continue
        wanted, given = b.schema[keyword], a.schema.get(keyword)
        if given is None or (given < wanted if lower else given > wanted):
            out.append(f"{here}: `{keyword}` is {wanted} for {b.label} but {given} for {a.label}")


def _strings(a: _Side, b: _Side, here: str, out: list[str]) -> None:
    _bounds(a, b, here, (("minLength", True), ("maxLength", False)), out)
    for keyword in ("pattern", "format"):
        if keyword in b.schema and a.schema.get(keyword) != b.schema[keyword]:
            out.append(
                f"{here}: `{keyword}` is {b.schema[keyword]!r} for {b.label} "
                f"but {a.schema.get(keyword)!r} for {a.label}"
            )


def _finite_values(a: _Side, b: _Side, values: list[Any], here: str, out: list[str]) -> None:
    for value in values:
        if _valid(value, a) and not _valid(value, b):
            out.append(f"{here}: the value {value!r} is allowed by {a.label} and refused by {b.label}")


def accepted_by(
    a: _Side, b: _Side, path: str, out: list[str], _seen: set[tuple[int, int]] | None = None
) -> None:
    """Appends to `out` each reason why not every instance of `a` is shown to be an instance of `b`."""
    seen = _seen if _seen is not None else set()
    schema_a, schema_b = a.resolved()[0], b.resolved()[0]
    key = (id(schema_a), id(schema_b))
    if key in seen:  # a recursive definition: assumed to hold, the rest of the walk decides
        return
    seen.add(key)
    a, b = _Side(schema_a, a.defs, a.label), _Side(schema_b, b.defs, b.label)
    here = path or "/"

    values = _finite(schema_a)
    if values is not None:
        _finite_values(a, b, values, here, out)
        return
    if _finite(schema_b) is not None:
        out.append(f"{here}: {b.label} allows only fixed values, {a.label} allows more")
        return

    types_a, types_b = _types(schema_a), _types(schema_b)
    shared = {t for t in types_a if t in types_b or (t == "integer" and "number" in types_b)}
    for t in sorted(types_a - shared):
        out.append(f"{here}: type `{t}` is allowed by {a.label} and refused by {b.label}")
    if shared & {"integer", "number"}:
        _bounds(a, b, here, (("minimum", True), ("maximum", False)), out)
    if "string" in shared:
        _strings(a, b, here, out)
    if "object" in shared:
        _objects(a, b, here, path, out, seen)
    if "array" in shared and "items" in schema_b:
        accepted_by(a.sub(schema_a.get("items")), b.sub(schema_b["items"]), f"{path}/items", out, seen)


def _objects(a: _Side, b: _Side, here: str, path: str, out: list[str], seen: set[tuple[int, int]]) -> None:
    sa, sb = a.schema, b.schema
    props_a: Mapping[str, Any] = sa.get("properties") or {}
    props_b: Mapping[str, Any] = sb.get("properties") or {}
    extra_a: Any = sa.get("additionalProperties", True)
    extra_b: Any = sb.get("additionalProperties", True)
    for name in sorted(set(sb.get("required") or []) - set(sa.get("required") or [])):
        out.append(f"{here}: `{name}` is required by {b.label} and optional for {a.label}")
    for name in sorted(props_a.keys() | props_b.keys()):
        at = f"{path}/{name}"
        if name in props_a:
            mine: Any = props_a[name]
        elif extra_a is False:
            continue  # this side never has it
        else:
            mine = extra_a if isinstance(extra_a, dict) else {}
        if name in props_b:
            accepted_by(a.sub(mine), b.sub(props_b[name]), at, out, seen)
        elif extra_b is False:
            out.append(f"{here}: the property `{name}` is allowed by {a.label} and refused by {b.label}")
        elif isinstance(extra_b, dict):
            accepted_by(a.sub(mine), b.sub(extra_b), at, out, seen)
    if extra_a is False:  # nothing beyond the named properties
        return
    if extra_b is False:
        out.append(f"{here}: other properties are allowed by {a.label} and refused by {b.label}")
    elif isinstance(extra_b, dict):
        rest = extra_a if isinstance(extra_a, dict) else {}
        accepted_by(a.sub(rest), b.sub(extra_b), f"{path}/*", out, seen)


def _compare(
    server: Mapping[str, Any],
    profile: Mapping[str, Any],
    profile_defs: Mapping[str, Any],
    *,
    input_side: bool,
    hidden: set[str],
) -> list[str]:
    """Input: the profile's accepted by the server's. Output: the server's accepted by the profile's."""
    server_defs = server.get("$defs") or {}
    body = {k: v for k, v in server.items() if k != "$defs"}
    if input_side:
        body = _without(body, hidden)
    problems: list[str] = []
    mine = _Side(body, server_defs, "the server")
    theirs = _Side(profile, profile_defs, "the profile")
    if input_side:
        accepted_by(theirs, mine, "", problems)
    else:
        accepted_by(mine, theirs, "", problems)
    return problems


def _without(schema: dict[str, Any], names: set[str]) -> dict[str, Any]:
    """The server's input as an agent sees it: the arguments the platform fills are not the agent's."""
    out = dict(schema)
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: v for k, v in out["properties"].items() if k not in names}
    if isinstance(out.get("required"), list):
        out["required"] = [k for k in out["required"] if k not in names]
    return out


def conformance_problems(
    server: Mapping[str, Any],
    ref: str,
    *,
    plugin: str,
    requires: set[str],
    own: Mapping[str, Mapping[str, Any]],
    foreign: Mapping[str, Mapping[str, Any]] | None,
) -> list[str]:
    """Why `server` (the document at `ref`, of `plugin`) does not keep a profile it `implements`.

    `own` maps `<plugin>/<name>@<version>` to this plugin's profile documents. `foreign` maps the same to
    the profiles of the plugins it requires; `None` leaves another plugin's profiles unchecked (the loader
    has none to read: `validate --profiles-from` and the installer do)."""
    problems: list[str] = []
    operations = {str(op["name"]): op for op in server.get("operations") or []}
    for implemented in server.get("implements") or []:
        match = PROFILE_NAME.match(str(implemented))
        if match is None:
            continue  # `tracker`, which the loader checks on its own
        owner = match.group(1)
        if owner != plugin and owner not in requires:
            problems.append(
                f"{ref}: implements `{implemented}`, but `{owner}` is not in this plugin's `requires.plugins`"
            )
            continue
        if owner == plugin:
            profile = own.get(str(implemented))
        elif foreign is None:
            continue
        else:
            profile = foreign.get(str(implemented))
        if profile is None:
            problems.append(f"{ref}: implements `{implemented}`, which {owner} does not declare")
            continue
        problems.extend(_server_keeps(ref, str(implemented), operations, profile))
    return problems


def _server_keeps(
    ref: str, name: str, operations: Mapping[str, Mapping[str, Any]], profile: Mapping[str, Any]
) -> list[str]:
    out: list[str] = []
    defs = profile.get("$defs") or {}
    for tool in profile["tools"]:
        tool_name = str(tool["name"])
        where = f"{ref}: `{tool_name}` of `{name}`"
        op = operations.get(tool_name)
        if op is None:
            out.append(f"{where} is not declared")
            continue
        if op["access"] != tool["access"]:
            out.append(f"{where} is a {tool['access']} of the profile, declared `{op['access']}`")
        if tool.get("human_approval") and not op.get("human_approval"):
            out.append(f"{where} requires `human_approval: true` (a floor: it may be raised, never lowered)")
        missing = sorted(set(tool.get("platform_arguments") or []) - set(op.get("platform_arguments") or []))
        if missing:
            out.append(
                f"{where} must keep the platform arguments {missing} (a floor: added to, never removed)"
            )
        hidden = set(op.get("platform_arguments") or [])
        for side, key in (("input", "input_schema"), ("output", "output_schema")):
            schema = op.get(key)
            if schema is None:
                out.append(f"{where} declares no `{key}`: the {side} cannot be compared with the profile's")
                continue
            found = subset_problems(schema, f"{where}, `{key}`")
            if found:
                out.extend(found)
                continue
            reasons = _compare(schema, tool[side], defs, input_side=side == "input", hidden=hidden)
            direction = (
                "does not accept what the profile accepts"
                if side == "input"
                else "returns what the profile does not accept"
            )
            out.extend(f"{where}, {side} {direction}: {reason}" for reason in reasons)
    return out
