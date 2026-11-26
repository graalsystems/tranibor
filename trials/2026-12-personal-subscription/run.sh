#!/usr/bin/env bash
# S87-07: run by the owner of a Claude subscription, on their own machine, from a checkout of this repository.
#   1. `claude setup-token` (Claude Code installed on this machine), copy the token it prints;
#   2. `read -rs CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN` (typed, never in the shell history);
#   3. `trials/2026-12-personal-subscription/run.sh`.
# It installs the adapter the runner pins in a directory of its own, runs probe.py and writes
# packages/runner/tests/fixtures/subscription/measured.json, which holds no token (the probe checks).
set -euo pipefail
: "${CLAUDE_CODE_OAUTH_TOKEN:?export CLAUDE_CODE_OAUTH_TOKEN first (see the header of this script)}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="$(sed -n 's/^claude-agent-acp=//p' "$ROOT/packages/runner/src/tranibor_runner/backends/versions.lock")"
PREFIX="$(mktemp -d)"
trap 'rm -rf "$PREFIX"' EXIT
npm install --silent --prefix "$PREFIX" "@agentclientprotocol/claude-agent-acp@${VERSION}"
export PATH="$PREFIX/node_modules/.bin:$PATH"
cd "$ROOT"
uv run python trials/2026-12-personal-subscription/probe.py
