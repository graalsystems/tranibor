# 0228 — The documentation is published on graal.systems, beside the other products of graal

- **Status**: accepted, 2026-11-27 (decided by the product owner on 2026-11-27: "the documentation must
  only be on the main site", for every product of graal); story S84-09
- **Concerns**: the publishing identity's `documentation`, `.github/workflows/docs-site.yml`,
  `docs/site/`, `website/`; supersedes the publication of [ADR 0185](0185-the-documentation-site-is-built-here-and-published-on-pages.md)
  §2 on GitHub Pages; amends [ADR 0215](0215-each-connector-has-a-page-on-the-documentation-site.md) §4

## Context

graal publishes two products, Tranibor and Lacydon, and will publish more. Their reader looks for the
documentation on graal.systems/docs, the site of graal. That site already rendered this product's pages
under `/docs/agentic-platform` (its repository imports `docs/site/` at each release tag, with the
connector pages); this repository published a second copy on GitHub Pages. Two copies drift: a reader
on one does not see what the other has, and the console's help link pointed at the copy graal.systems
does not show.

## Decision

1. **The documentation of this product is published only on graal.systems**, at
   `https://graal.systems/en/docs/agentic-platform` (the English pages: the product's documentation is
   written in English, ADR 0046). The publishing identity's `documentation` says so, and everything
   that reads it follows: the console's help links, the API's `docs_url`, the MCP door's documentation
   links (ADR 0150), the editor extension's home page.
2. **This repository stops publishing it.** `docs-site.yml` no longer deploys: it builds the pages on a
   pull request that touches them, and on a push to `main`, to prove they build, the connector pages
   included (ADR 0215); it holds no Pages permission, no schedule and no `github-pages` environment.
   The repository's GitHub Pages are switched off (a setting, outside the repository).
3. **`docs/site/` stays the source.** Its slugs stay fixed (the site serves the same path under its
   base), and `website/scripts/import-docs.mjs` stays the tool that copies it: the site's repository
   runs it at the release tag. A correction is made here and reaches graal.systems with the next
   release of the product.

## Consequences

- An address `https://graalsystems.github.io/tranibor/...` no longer answers once the Pages are off.
- An installation that set `TRANIBOR_DOCS_URL` keeps its value; the default is graal.systems.
- A page here is published when a release is cut, not on the next push to `main`: the daily rebuild
  of ADR 0215 §4 is gone. graal.systems shows the documentation of the released version, as it does
  for every product.

## What this does not prove

- The import on graal.systems: it is that site's repository's, outside this one. `tests/docs` proves
  the identity and the workflow, not the page served there.
