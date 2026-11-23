# 0215 — Each connector has a page on the documentation site, the platform's and the plugins'

- **Status**: proposed, 2026-11-23 (story S84-06)
- **Concerns**: `tools/procedures/export_docs.py` (`--site`), `docs/site/connectors/`,
  `.github/workflows/docs-site.yml`, the console's setup panel (`apps/web/src/components/procedures/`,
  `lib/procedures.ts`) and Library → Plugins; builds on [ADR 0067](0067-a-procedure-per-connector-and-agent-type.md),
  [ADR 0093](0093-an-agent-ships-as-a-plugin.md), [ADR 0096](0096-a-plugin-s-code-runs-as-an-mcp-server-the-platform-runs.md)
  and [ADR 0185](0185-the-documentation-site-is-built-here-and-published-on-pages.md)

## Context

A setup procedure (ADR 0067) says what to do at the vendor and in Tranibor to connect one system. The
platform ships one per connector type; a plugin ships one per server it brings (ADR 0093), and the API
lists the installed plugins' procedures beside the platform's. Three gaps:

- **The console never showed a plugin's procedure where it is needed.** A plugin's server is an
  organisation connector of kind and type `mcp/mcp` (ADR 0096). Its settings asked
  `GET /procedures?for=connector:mcp/mcp` and showed the generic MCP server procedure; the plugin's own,
  which applies to `connector: mcp/<plugin>` or `mcp/<server>`, was never matched. Library → Plugins
  listed a plugin's procedures as ids in plain text.
- **The documentation site had no page per connector.** Its connectors page linked to
  `docs/procedures/*.md` in the repository, the platform's only.
- **A plugin's procedure appeared nowhere** outside an organisation that installed the plugin.

## Decision

1. **A plugin server's procedure is looked up by candidates, first match wins.** The setup panel takes
   one target or an ordered list. For the connector of a plugin's server (`server.plugin`, else the
   installer's `catalogue_entry` `plugin:<plugin>/<server>@<version>`) the console asks
   `connector:mcp/<plugin>`, then `connector:mcp/<server>`, then the generic `connector:mcp/mcp`; any other
   connector asks its own `connector:<kind>/<type>`. `GET /procedures?for=` answers an empty list when
   nothing matches, which moves the lookup to the next candidate; an error stops it and the panel shows
   nothing, as before. The convention for a plugin's author: **a server's procedure applies to
   `connector: mcp/<plugin>` (a plugin with one server) or `mcp/<server>`**. The API is unchanged.
2. **Library → Plugins opens an installed plugin's procedure by its id**, in the same panel and the same
   Markdown rendering as a connector's form; an id the API does not serve stays plain text.
3. **The documentation site has a section `connectors/`** (slug `connectors`, between "Use Tranibor"
   and "Build agents"). Its page `docs/site/connectors/index.md` is written by hand and committed; the
   pages below it are **generated at build time and never committed**: `export_docs.py --site <dir>`
   writes one page per procedure the platform ships, grouped by connector kind (then knowledge sources,
   MCP-door clients and external agents), each group with its `_category_.json`. With `--plugins <dir>`
   (the `plugins/` folder of a checkout of the plugins' repository) it adds one group per plugin, each
   page naming its plugin and version and linking its source file at `--plugins-ref`. A page has a
   Docusaurus `title` and `description`, no `slug` or `id`, the sections as the console renders them with
   the placeholders written `<api_url>`, `<public_url>` and `<org>` (as `docs/procedures/`), the fields as
   a table, the egress hosts and the vendor's documents as lists, and its prose escaped for MDX. A page
   never takes its group's index name (`index`, `readme`, the group's own name: `mcp/mcp` is written
   `mcp/mcp-setup.md`). Every procedure goes through the ADR 0067 parser. A platform procedure that does
   not parse fails the build, always. A plugin's fails it on a pull request of this repository; a publish
   (`--plugins-lenient`: a push to `main`, a release, the daily run) leaves that plugin out with a
   warning, so one plugin never stops the platform's pages from being published. The source links derive
   from the publishing identity (ADR 0156), never a literal.
4. **The site's workflow builds them.** After `import-docs.mjs` copies `docs/site/` (which it empties
   first), the workflow checks out the plugins' repository at `main`, runs the export into
   `website/docs/connectors/`, then builds. It runs on a daily schedule too, so a plugin's new or changed
   procedure reaches the site without a release of the core.

## Consequences

- A plugin's procedure shows on its server's connector, and on Library → Plugins, with nothing new in the
  API or the contracts. A plugin whose procedure names neither its own name nor its server's still gets
  the generic one, as before.
- `docs/procedures/` stays: it is the repository's reference, the platform's procedures only, and
  `make procedures-check` still refuses drift there. The site's pages are its published face.
- The plugin pages follow the plugins' repository's `main`, not the version an organisation installed:
  the site says which version it rendered. A deployment's own notes and procedures
  (`TRANIBOR_PROCEDURES_EXTRA`) never reach the public site.
- The daily build publishes whatever `main` of the core holds, like a push to `main` does.
- A plugin left out of a publish is visible only in the workflow's log, until a pull request here (which
  is strict) or the plugins' repository's own check (`tests/test_procedures.py` there) catches it.

## What this does not prove

- The Docusaurus build of the generated pages is proven by the workflow, not by `make ci`: the Python
  tests check each page's front matter, placeholders, attribution, field table and MDX safety, not the
  site's renderer. One local build (Docusaurus 3.8.1, the plugins' repository at `23854cd`, 77 generated
  pages) passed; its first attempt failed on a bare address holding a placeholder
  (`https://dev.azure.com/<organisation>` in a table: Markdown links the address up to the `<`, backslash
  included), which the export now writes as code.
- The candidates are checked by the console's tests against the mock API, not against a deployment
  with a plugin installed.
