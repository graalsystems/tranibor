# SPDX-License-Identifier: Apache-2.0
"""The documentation site's connector pages, generated from the platform's and the plugins' procedures
(ADR 0215, S84-06): one page per procedure, Docusaurus front matter, the placeholders of the docs export,
the plugin named and linked, the fields as a table, prose the site's MDX reader accepts."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys
import textwrap

import pytest
import yaml
from tranibor_core import procedures as core
from tranibor_core.publishing import PUBLISHING

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE_PLUGINS = ROOT / "tests/fixtures/plugins"
_SPEC = importlib.util.spec_from_file_location(
    "export_procedures_site", ROOT / "tools/procedures/export_docs.py"
)
assert _SPEC and _SPEC.loader
export_docs = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = export_docs
_SPEC.loader.exec_module(export_docs)

FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.S)
FENCE = re.compile(r"^\s*(```|~~~).*?^\s*\1[^\n]*$", re.M | re.S)
#: A code span, which may run over two lines of a paragraph.
INLINE_CODE = re.compile(r"``.+?``|`[^`]+`", re.S)


def _front(text: str) -> dict[str, str]:
    match = FRONT_MATTER.match(text)
    assert match, "a page starts with its front matter"
    data = yaml.safe_load(match.group(1))
    assert isinstance(data, dict)
    return data


def _prose(text: str) -> str:
    return INLINE_CODE.sub("", FENCE.sub("", FRONT_MATTER.sub("", text)))


@pytest.fixture(scope="module")
def site() -> dict[str, str]:
    return export_docs.render_site(FIXTURE_PLUGINS, plugins_ref="0123abc")


def _pages(site: dict[str, str]) -> dict[str, str]:
    return {name: text for name, text in site.items() if name.endswith(".md")}


def test_one_page_per_procedure_of_the_platform_and_of_the_plugin(site: dict[str, str]) -> None:
    shipped = core.load().procedures
    pages = _pages(site)
    assert len(pages) == len(shipped) + 1
    titles = sorted(_front(text)["title"] for text in pages.values())
    assert titles == sorted([p.front.title for p in shipped.values()] + ["Connect the hello server"])
    assert "plugin-hello/hello.md" in pages
    assert "tracker/jira.md" in pages
    assert "mcp-door-clients/cursor.md" in pages
    assert not any("." in pathlib.PurePosixPath(name).stem for name in pages), (
        "a dot in a URL reads as an extension"
    )


def test_without_plugins_only_the_platform_s_pages() -> None:
    pages = _pages(export_docs.render_site())
    assert len(pages) == len(core.load().procedures)
    assert not any(name.startswith("plugin-") for name in pages)


@pytest.mark.parametrize("name", ["tracker/jira.md", "plugin-hello/hello.md", "documents/sharepoint.md"])
def test_a_page_has_a_title_and_a_description_and_keeps_its_url(site: dict[str, str], name: str) -> None:
    data = _front(site[name])
    assert data["title"]
    assert len(data["description"]) > 40
    assert "slug" not in data and "id" not in data
    assert data["custom_edit_url"].startswith("https://")


def test_every_group_has_its_category_in_a_stable_order(site: dict[str, str]) -> None:
    folders = {name.split("/", 1)[0] for name in _pages(site)}
    categories = {f: json.loads(site[f"{f}/_category_.json"]) for f in folders}
    assert all(c["label"] and c["link"]["slug"] == f"/connectors/{f}" for f, c in categories.items())
    order = [f for f, _ in sorted(categories.items(), key=lambda item: item[1]["position"])]
    assert order[-1] == "plugin-hello", "the plugins come after the platform's groups"
    assert order[-4:-1] == ["knowledge-sources", "mcp-door-clients", "external-agents"]
    assert categories["tracker"]["label"] == "Work trackers"
    assert categories["plugin-hello"]["label"] == "Plugin hello"


def test_the_placeholders_are_shown_as_in_the_docs_export(site: dict[str, str]) -> None:
    jira = site["tracker/jira.md"]
    assert "<api_url>/api/v1/webhooks/jira" in jira
    assert "{{" not in jira
    assert "`<api_url>`, `<public_url>` and `<org>`" in jira


def test_a_plugin_page_names_its_plugin_and_links_its_source(site: dict[str, str]) -> None:
    page = site["plugin-hello/hello.md"]
    source = f"{PUBLISHING.plugin_repository}/blob/0123abc/plugins/hello/procedures/connector.mcp.hello.md"
    assert f"It ships with the plugin `hello` 1.0.0: [its source]({source})." in page
    assert "It applies to the connector type `mcp/hello`." in page
    assert _front(page)["custom_edit_url"] == (
        f"{PUBLISHING.plugin_repository}/edit/main/plugins/hello/procedures/connector.mcp.hello.md"
    )
    shipped = core.parse(
        (FIXTURE_PLUGINS / "hello/procedures/connector.mcp.hello.md").read_text(encoding="utf-8")
    )
    for section in shipped.sections:
        assert f"\n## {section.title}\n\n{section.markdown}\n" in page


def test_the_platform_s_page_names_its_own_source(site: dict[str, str]) -> None:
    edit = _front(site["tracker/jira.md"])["custom_edit_url"]
    assert edit == (
        f"{PUBLISHING.source_repository}/edit/main/packages/core/src/tranibor_core/procedures/connector.tracker.jira.md"
    )
    assert "It ships with the platform." in site["tracker/jira.md"]


def test_the_fields_are_a_table_and_the_vendor_documents_a_list(site: dict[str, str]) -> None:
    page = site["documents/sharepoint.md"]
    assert "## Fields\n\n| Field | What to put in it |\n| :-- | :-- |\n| `" in page
    assert re.search(r"^\| `client_secret` \| .+ \|$", page, re.M)
    assert re.search(r"^## Vendor documentation\n\n- \[[^\]]+\]\(https://", page, re.M)
    assert "- `graph.microsoft.com`" in page


@pytest.mark.parametrize("name", sorted(_pages(export_docs.render_site(FIXTURE_PLUGINS))))
def test_the_prose_is_safe_for_mdx(name: str) -> None:
    """A bare `<` opens a JSX tag and a bare `{` an expression: the site's build fails on them."""
    text = export_docs.render_site(FIXTURE_PLUGINS)[name]
    unsafe = [
        line
        for line in _prose(text).splitlines()
        if re.search(r"(?<!\\)<[A-Za-z/!]|(?<!\\)[{}]", line) and not line.startswith("| `")
    ]
    assert not unsafe, f"{name}: {unsafe}"


def test_mdx_safe_escapes_prose_and_leaves_code_alone() -> None:
    text = (
        'Go to <api_url>/x, set {a} and `<kept>` and ``a ` <b>`` then\n\n```json\n{"k": "<v>"}\n```\nend <c>'
    )
    assert export_docs.mdx_safe(text) == (
        "Go to \\<api_url>/x, set \\{a\\} and `<kept>` and ``a ` <b>`` then\n\n"
        '```json\n{"k": "<v>"}\n```\nend \\<c>'
    )


def test_an_address_holding_a_placeholder_becomes_code() -> None:
    """Markdown links a bare address up to the `<`, backslash included: escaping cannot work there."""
    text = (
        "Paste https://dev.azure.com/<organisation>, or (https://<server>/<path>). See https://example.com."
    )
    assert export_docs.mdx_safe(text) == (
        "Paste `https://dev.azure.com/<organisation>`, or (`https://<server>/<path>`). See https://example.com."
    )


def _plugin(tmp_path: pathlib.Path, body: str, fields: str = "") -> pathlib.Path:
    root = tmp_path / "plugins" / "acme-desk"
    (root / "procedures").mkdir(parents=True)
    (root / "plugin.yaml").write_text(
        "kind: Plugin\nmetadata: {name: acme-desk, version: 0.4.0}\n"
        'contents:\n  procedures: ["procedures/*.md"]\n',
        encoding="utf-8",
    )
    front = textwrap.dedent(
        """\
        ---
        id: connector.mcp.acme-desk
        title: Connect the desk
        applies_to: {connector: mcp/acme-desk}
        audience: org_admin
        version: 1
        reviewed: 2026-11-20
        egress: []
        """
    )
    (root / "procedures" / "connector.mcp.acme-desk.md").write_text(
        front + fields + "---\n\n" + body, encoding="utf-8"
    )
    return tmp_path / "plugins"


BODY = "".join(
    f"## {title}\n\nText for {title}.\n\n"
    for title in ("Before you start", "At Acme", "Permissions", "In Tranibor", "Test it", "When it fails")
)


def test_a_field_with_a_pipe_or_a_placeholder_stays_in_its_cell(tmp_path: pathlib.Path) -> None:
    plugins = _plugin(
        tmp_path, BODY, 'fields:\n  base_url: "https://<tenant>.acme.example | or the <EU> host"\n'
    )
    page = export_docs.render_site(plugins)["plugin-acme-desk/acme-desk.md"]
    assert "| `base_url` | `https://<tenant>.acme.example` \\| or the \\<EU> host |" in page


def test_a_plugin_procedure_that_does_not_parse_fails_the_export(tmp_path: pathlib.Path) -> None:
    plugins = _plugin(tmp_path, "## Before you start\n\nOnly one section.\n")
    with pytest.raises(core.ProcedureError):
        export_docs.render_site(plugins)
    assert export_docs.main(["--site", str(tmp_path / "out"), "--plugins", str(plugins)]) == 1


def test_the_command_writes_beside_the_hand_written_index(tmp_path: pathlib.Path) -> None:
    out = tmp_path / "connectors"
    out.mkdir()
    (out / "index.md").write_text("hand-written\n", encoding="utf-8")
    assert (
        export_docs.main(["--site", str(out), "--plugins", str(FIXTURE_PLUGINS), "--plugins-ref", "v1.2.3"])
        == 0
    )
    assert (out / "index.md").read_text(encoding="utf-8") == "hand-written\n"
    assert "/blob/v1.2.3/plugins/hello/" in (out / "plugin-hello/hello.md").read_text(encoding="utf-8")
    assert (out / "tracker/_category_.json").is_file()
    assert len(list(out.rglob("*.md"))) == len(core.load().procedures) + 2


def test_the_command_refuses_a_bad_ref_and_plugins_without_site(tmp_path: pathlib.Path) -> None:
    assert export_docs.main(["--site", str(tmp_path), "--plugins-ref", "bad ref"]) == 1
    with pytest.raises(SystemExit):
        export_docs.main(["--plugins", str(FIXTURE_PLUGINS)])


def test_a_plugin_that_does_not_parse_is_left_out_of_a_lenient_export(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """ADR 0215: a publish leaves a plugin out, with a warning, rather than stop the platform's pages."""
    plugins = _plugin(tmp_path, "## Before you start\n\nOnly one section.\n")
    good = tmp_path / "plugins" / "hello"
    good.mkdir()
    for path in (FIXTURE_PLUGINS / "hello").rglob("*"):
        if path.is_file():
            target = good / path.relative_to(FIXTURE_PLUGINS / "hello")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
    pages = export_docs.render_site(plugins, lenient=True)
    assert "plugin-hello/hello.md" in pages, "the other plugins are still published"
    assert not any(name.startswith("plugin-acme-desk/") for name in pages)
    assert "the plugin acme-desk is left out of the site" in capsys.readouterr().err
    out = tmp_path / "out"
    assert export_docs.main(["--site", str(out), "--plugins", str(plugins), "--plugins-lenient"]) == 0
    assert (out / "plugin-hello" / "hello.md").is_file()


def test_no_page_takes_its_group_s_index(site: dict[str, str]) -> None:
    """Docusaurus makes `<folder>/<folder>.md`, `index.md` or `readme.md` the group's own page, which
    clashes with the generated index: the generic MCP procedure was unreachable (`mcp/mcp.md`)."""
    for name in _pages(site):
        folder, _, file = name.rpartition("/")
        stem = file.removesuffix(".md").lower()
        assert stem not in {"index", "readme", folder.rpartition("/")[2].lower()}, name
    assert "mcp/mcp-setup.md" in site


def test_a_page_name_has_neither_dot_nor_underscore(site: dict[str, str]) -> None:
    for name in _pages(site):
        assert "." not in name.removesuffix(".md") and "_" not in name, name
    assert "runtime/agent-sandbox.md" in site


def test_a_plugin_page_names_the_console_screen_that_lists_it(site: dict[str, str]) -> None:
    assert "and on **Library › Plugins**." in site["plugin-hello/hello.md"]
