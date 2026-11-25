# SPDX-License-Identifier: Apache-2.0
"""The names the platform puts on the wire and in stored state, and their former twins (ADR 0224, R7).

ADR 0224 §4 gives every wire name a Tranibor twin. For a window of two minor versions the platform
**reads both** (accepts both on input, finds both in stored state) and **writes the new one**; it emits
both only where a reader of the former one exists outside the repository. This module is the one place
that spells both forms of each family, so no other module writes the former name by hand:

* headers: `X-Tranibor-<Name>`, formerly `X-Choregos-<Name>` (`header`, `former_header`, `read_header`,
  `both_headers`); a webhook signature is computed the same way under either name;
* manifests: `apiVersion: tranibor/v1` and `tranibor.dev/v1alpha1`, formerly `choregos/v1` and
  `choregos.dev/v1alpha1`; schema identifiers `tranibor.dev/schemas/*`;
* labels and annotations: `tranibor.dev/<key>` (and the bare `tranibor/<key>`), written beside the former
  family for the window, selected on under either;
* identity provider groups: `tranibor:<org>:<group>` beside `choregos:<org>:<group>`;
* cookies: `tranibor_<name>`, the former `choregos_<name>` read and then cleared;
* tracker markers: `<!-- tranibor:<kind> -->` (and `[tranibor:<kind>]`), the former found too;
* the MCP server's `name`, the A2A extension URI, the problem type URIs, the web fetch's tokens.

The former forms are removed in the minor that closes the window, in their own PR (ADR 0224 §1).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

__all__ = [
    "A2A_SKILL_FIELDS_EXTENSION",
    "A2A_SKILL_FIELDS_EXTENSIONS",
    "ACCESS_KEY",
    "ACCESS_KEYS",
    "ANY_MARKER",
    "API_VERSION",
    "API_VERSIONS",
    "FORMER_A2A_SKILL_FIELDS_EXTENSION",
    "FORMER_ACCESS_KEY",
    "FORMER_API_VERSION",
    "FORMER_KUBE_API_VERSION",
    "FORMER_MCP_SERVER_NAME",
    "FORMER_NAME",
    "FORMER_PROBLEM_BASE",
    "FORMER_ROBOTS_TOKENS",
    "GROUP_PREFIXES",
    "KUBE_API_VERSION",
    "KUBE_API_VERSIONS",
    "MCP_SERVER_NAME",
    "MCP_SERVER_NAMES",
    "NAME",
    "PLATFORM_KEY_PREFIXES",
    "PROBLEM_BASE",
    "ROBOTS_TOKENS",
    "SCHEMA_ID_BASE",
    "SCHEMA_ID_BASES",
    "both_headers",
    "cookie",
    "cookie_names",
    "former_cookie",
    "former_header",
    "former_label",
    "former_marker",
    "group",
    "group_org_and_name",
    "has_marker",
    "header",
    "header_names",
    "is_api_version",
    "is_kube_api_version",
    "label",
    "label_keys",
    "labels_with_former",
    "marker",
    "markers",
    "read_access",
    "read_cookie",
    "read_header",
    "read_label",
    "strip_group_prefix",
    "with_twin_headers",
]

NAME = "tranibor"
FORMER_NAME = "choregos"

# ─── headers ────────────────────────────────────────────────────────────────────────────────────────

_HEADER_PREFIX = "X-Tranibor-"
_FORMER_HEADER_PREFIX = "X-Choregos-"


def header(name: str) -> str:
    """`X-Tranibor-<name>`: the header the platform sends (`header("Secret")`)."""
    return f"{_HEADER_PREFIX}{name}"


def former_header(name: str) -> str:
    """`X-Choregos-<name>`: the former header, still accepted for the window."""
    return f"{_FORMER_HEADER_PREFIX}{name}"


def header_names(name: str) -> tuple[str, str]:
    """Both names of one header, the new one first."""
    return header(name), former_header(name)


def read_header(headers: Mapping[str, str], name: str, default: str = "") -> str:
    """The value of the header `name` under its new name, else under its former one, else `default`.

    `headers` may be case-insensitive (Starlette's, httpx's) or a plain mapping in any case: the lookup
    compares lowercased names. An empty value counts as absent, so a client that sends the new header
    empty and the former one filled is read by the former one.
    """
    wanted_new, wanted_former = (n.lower() for n in header_names(name))
    new = former = ""
    for key, value in headers.items():
        lowered = key.lower()
        if lowered == wanted_new and value and not new:
            new = value
        elif lowered == wanted_former and value and not former:
            former = value
    return new or former or default


def both_headers(name: str, value: str) -> dict[str, str]:
    """`{X-Tranibor-<name>: value, X-Choregos-<name>: value}`: for a receiver that may still read the
    former name (a delivery to a third party, a response an older client reads)."""
    return {header(name): value, former_header(name): value}


def with_twin_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """`headers`, each `X-Tranibor-*` header also under its `X-Choregos-*` twin and the reverse, when the
    twin is absent: for a set of headers relayed to a server outside the repository (a plugin's tracker
    server), which may read either name. A twin already present keeps its own value."""
    out = dict(headers)
    present = {key.lower() for key in headers}
    pairs = ((_HEADER_PREFIX, _FORMER_HEADER_PREFIX), (_FORMER_HEADER_PREFIX, _HEADER_PREFIX))
    for key, value in headers.items():
        for source, target in pairs:
            if key.lower().startswith(source.lower()):
                twin = target + key[len(source) :]
                if twin.lower() not in present:
                    out[twin] = value
                    present.add(twin.lower())
                break
    return out


#: The platform's own keys on an entry of a run's tool list (ADR 0122): `x-tranibor-access`, formerly
#: `x-choregos-access`. The API lists both for the window (a runner of the previous release reads the
#: former); an extension may declare either.
ACCESS_KEY = "x-tranibor-access"
FORMER_ACCESS_KEY = "x-choregos-access"
ACCESS_KEYS = (ACCESS_KEY, FORMER_ACCESS_KEY)
PLATFORM_KEY_PREFIXES = ("x-tranibor-", "x-choregos-")


def read_access(entry: Mapping[str, object]) -> object:
    """The access a tool entry declares, under the new key, else the former one (`None` when neither)."""
    value = entry.get(ACCESS_KEY)
    return value if value is not None else entry.get(FORMER_ACCESS_KEY)


# ─── manifests ──────────────────────────────────────────────────────────────────────────────────────

#: The `apiVersion` of the platform's own documents (a workflow, a project, a plugin, a procedure...).
API_VERSION = "tranibor/v1"
FORMER_API_VERSION = "choregos/v1"
API_VERSIONS = (API_VERSION, FORMER_API_VERSION)
#: The `apiVersion` of the Kubernetes-shaped documents (`kind: Plugin`, `kind: AgentProfile`...).
KUBE_API_VERSION = "tranibor.dev/v1alpha1"
FORMER_KUBE_API_VERSION = "choregos.dev/v1alpha1"
KUBE_API_VERSIONS = (KUBE_API_VERSION, FORMER_KUBE_API_VERSION)
#: The base of the JSON schemas' `$id`.
SCHEMA_ID_BASE = "https://tranibor.dev/schemas/"
SCHEMA_ID_BASES = (SCHEMA_ID_BASE, "https://choregos.dev/schemas/")


def is_api_version(value: object) -> bool:
    """Whether `value` is the platform's `apiVersion`, under either name."""
    return value in API_VERSIONS


def is_kube_api_version(value: object) -> bool:
    """Whether `value` is the Kubernetes-shaped `apiVersion`, under either name."""
    return value in KUBE_API_VERSIONS


# ─── labels and annotations ─────────────────────────────────────────────────────────────────────────


def label(key: str, *, domain: bool = True) -> str:
    """`tranibor.dev/<key>` (or `tranibor/<key>` with `domain=False`, the bare family of the runner's
    jobs: `tranibor/work-item`, `tranibor/run-id`, `tranibor/project`)."""
    return f"{NAME}.dev/{key}" if domain else f"{NAME}/{key}"


def former_label(key: str, *, domain: bool = True) -> str:
    """The former key of `label(key)`, written beside it for the window."""
    return f"{FORMER_NAME}.dev/{key}" if domain else f"{FORMER_NAME}/{key}"


def label_keys(key: str, *, domain: bool = True) -> tuple[str, str]:
    """Both keys of one label, the new one first (a selector matches either)."""
    return label(key, domain=domain), former_label(key, domain=domain)


_FAMILIES = {f"{NAME}.dev/": f"{FORMER_NAME}.dev/", f"{NAME}/": f"{FORMER_NAME}/"}


def labels_with_former(labels: Mapping[str, str]) -> dict[str, str]:
    """`labels`, each `tranibor.dev/*` and `tranibor/*` key also written under its former key.

    The window writes both, so a selector or a policy written for the former family still matches an
    object created after the upgrade. A key the caller set under the former name is kept as it is.
    """
    out = dict(labels)
    for key, value in labels.items():
        for new, former in _FAMILIES.items():
            if key.startswith(new):
                out.setdefault(former + key.removeprefix(new), value)
                break
    return out


def read_label(labels: Mapping[str, str] | None, key: str, *, domain: bool = True) -> str | None:
    """The value of a label under its new key, else under its former one."""
    if not labels:
        return None
    new, former = label_keys(key, domain=domain)
    value = labels.get(new)
    return value if value is not None else labels.get(former)


# ─── identity provider groups ───────────────────────────────────────────────────────────────────────

#: An identity provider's group namespaced for an organisation: `tranibor:<org>:<group>`, formerly
#: `choregos:<org>:<group>`. Both are read; the platform writes the new one.
GROUP_PREFIXES = (f"{NAME}:", f"{FORMER_NAME}:")


def group(org: str, name: str) -> str:
    """`tranibor:<org>:<name>`."""
    return f"{NAME}:{org}:{name}"


def strip_group_prefix(value: str) -> str | None:
    """`<org>:<group>` for a namespaced group under either prefix, `None` for any other group."""
    for prefix in GROUP_PREFIXES:
        if value.startswith(prefix):
            return value.removeprefix(prefix)
    return None


def group_org_and_name(value: str) -> tuple[str, str] | None:
    """`(org, group)` of a namespaced group (`tranibor:acme:architects`), `None` otherwise."""
    rest = strip_group_prefix(value)
    if rest is None:
        return None
    org, sep, name = rest.partition(":")
    if not sep or not org or not name:
        return None
    return org, name


# ─── cookies ────────────────────────────────────────────────────────────────────────────────────────


def cookie(name: str) -> str:
    """`tranibor_<name>`: the cookie the platform sets (`cookie("session")`)."""
    return f"{NAME}_{name}"


def former_cookie(name: str) -> str:
    """`choregos_<name>`: read for the window, then cleared."""
    return f"{FORMER_NAME}_{name}"


def cookie_names(name: str) -> tuple[str, str]:
    """Both names of one cookie, the new one first."""
    return cookie(name), former_cookie(name)


def read_cookie(cookies: Mapping[str, str], name: str) -> tuple[str | None, bool]:
    """`(value, from_former)`: the cookie under its new name, else its former one (`from_former` then
    True: the caller sets it again under the new name and clears the former one)."""
    value = cookies.get(cookie(name))
    if value:
        return value, False
    value = cookies.get(former_cookie(name))
    return (value, True) if value else (None, False)


# ─── tracker markers ────────────────────────────────────────────────────────────────────────────────


def marker(kind: str, *, html: bool = True) -> str:
    """`<!-- tranibor:<kind> -->` (or `[tranibor:<kind>]` with `html=False`, for a tracker whose
    comments are not Markdown)."""
    return f"<!-- {NAME}:{kind} -->" if html else f"[{NAME}:{kind}]"


def former_marker(kind: str, *, html: bool = True) -> str:
    """The former form of `marker(kind)`, still found in comments written before the upgrade."""
    return f"<!-- {FORMER_NAME}:{kind} -->" if html else f"[{FORMER_NAME}:{kind}]"


def markers(kind: str, *, html: bool = True) -> tuple[str, str]:
    """Both forms of one marker, the new one first."""
    return marker(kind, html=html), former_marker(kind, html=html)


def has_marker(text: str | None, kind: str, *, html: bool = True) -> bool:
    """Whether `text` carries the marker `kind` under either name."""
    return bool(text) and any(m in (text or "") for m in markers(kind, html=html))


#: `<!-- tranibor:<anything> -->` or the former: the platform's own comment, whatever its kind.
ANY_MARKER = re.compile(rf"<!-- (?:{NAME}|{FORMER_NAME}):[^>]*-->")

# ─── MCP, A2A, problems, the web fetch ──────────────────────────────────────────────────────────────

#: The MCP server's `name` in `initialize` (its `title` is the visible name).
MCP_SERVER_NAME = NAME
FORMER_MCP_SERVER_NAME = FORMER_NAME
MCP_SERVER_NAMES = (MCP_SERVER_NAME, FORMER_MCP_SERVER_NAME)
#: The A2A extension that carries a skill's fields (ADR 0156); a client may activate either.
A2A_SKILL_FIELDS_EXTENSION = "https://tranibor.dev/a2a/extensions/skill-fields/v1"
FORMER_A2A_SKILL_FIELDS_EXTENSION = "https://choregos.dev/a2a/extensions/skill-fields/v1"
A2A_SKILL_FIELDS_EXTENSIONS = (A2A_SKILL_FIELDS_EXTENSION, FORMER_A2A_SKILL_FIELDS_EXTENSION)
#: The base of the problem type URIs (RFC 9457) the API answers with.
PROBLEM_BASE = "https://tranibor.dev/problems/"
FORMER_PROBLEM_BASE = "https://choregos.dev/problems/"
#: The robots.txt tokens of the web fetch: the new ones first. A site keyed its rules on the former
#: ones, so both are honoured for the window (the most specific group that names any of them wins).
ROBOTS_TOKENS = ("TraniborKnowledge", "Tranibor")
FORMER_ROBOTS_TOKENS = ("ChoregosKnowledge", "Choregos")
