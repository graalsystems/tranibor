"""S40-03b: every connector type of the registry has an icon decision.

The console's icon test (`apps/web/tests/connector-icon.test.ts`) reads a copy of the registry's
`(kind, type)` pairs. This test keeps that copy equal to the registry: a type added without a
fixture update, hence without an icon decision, turns it red.

The registry is the one the API serves: the core's types and those of the installed extensions
(`investment-research` registers the `marketdata` family, S83-08), loaded as `create_app` loads them.
Without that, the test read whatever an earlier test happened to load, and failed alone.

The fixture holds the first-party plugins' types too: the console decides their icon wherever they are
installed. Where a plugin is not installed (the core alone, ADR 0181 round D) its kinds are absent from the
registry: the fixture's entries of those kinds are compared where the plugin is installed (the suite with
the plugins), the rest is compared whole here, and nothing of those kinds may be registered without it.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib

from tranibor_adapters import connector_types, load_extensions

FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "apps"
    / "web"
    / "tests"
    / "fixtures"
    / "connector-types.json"
)

#: The connector kinds a first-party plugin registers (ADR 0181 round D, O4-4), by the plugin's package.
PLUGIN_KINDS = {
    "tranibor_software_delivery": {"scm", "ci", "cd"},
    "tranibor_investment_research": {"marketdata"},
}


def _absent_kinds() -> set[str]:
    """The kinds of the first-party plugins that are not installed in this environment."""
    return {
        kind
        for package, kinds in PLUGIN_KINDS.items()
        if importlib.util.find_spec(package) is None
        for kind in kinds
    }


def test_the_fixture_equals_the_registry() -> None:
    load_extensions()
    absent = _absent_kinds()
    expected = sorted((kind, type_name) for kind, type_name, _ in connector_types())
    assert not [pair for pair in expected if pair[0] in absent], "a plugin's kind registered without it"
    entries = json.loads(FIXTURE.read_text())
    assert {entry["kind"] for entry in entries} >= {kind for kinds in PLUGIN_KINDS.values() for kind in kinds}
    actual = sorted((entry["kind"], entry["type"]) for entry in entries if entry["kind"] not in absent)
    assert actual == expected, (
        "regenerate apps/web/tests/fixtures/connector-types.json and decide the icon of the new type"
    )
