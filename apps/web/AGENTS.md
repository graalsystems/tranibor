# Console (apps/web) — notes for coding agents

This is Next.js 16: APIs, conventions and file structure differ from older versions. Read the
relevant guide in `node_modules/next/dist/docs/` (this directory's `next` package) before writing
code, and heed deprecation notices.

This file is maintained by hand. `next.config.mjs` sets `agentRules: false`, so `next dev` does
not write its own block here and a development run leaves the tree clean. Keep Next's
managed-block markers out of this file: with the option off, Next deletes a block it finds.
