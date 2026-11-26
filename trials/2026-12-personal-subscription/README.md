# Trial 2026-12: Claude Code on a person's own subscription (S87-07)

Story S87-07 measures what the runner needs to run Claude Code on its owner's subscription (ADR 0226, ADR 0227):
which variables the CLI reads, what it answers when it is signed out or at a usage limit, whether a conversation
reloads, and which hosts it reaches. Half of it is read from the pinned packages' source, here, without a token; the
other half needs the subscription's owner and their own token, on their own machine.

## Read from the source (2026-11-26)

`packages/runner/tests/fixtures/subscription/from_source.json`, with the file and line of each fact, from
`@agentclientprotocol/claude-agent-acp` 0.85.1 (the version `versions.lock` pins), `@anthropic-ai/claude-agent-sdk`
0.3.286 and `@agentclientprotocol/sdk` 1.6.0:

- the CLI pays with a subscription token from `CLAUDE_CODE_OAUTH_TOKEN`, and reads its configuration from
  `CLAUDE_CONFIG_DIR` (else `~/.claude`);
- `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and an `apiKeyHelper` outrank a subscription: the runner removes them
  from the CLI's environment (S87-21);
- `initialize` announces `loadSession`; `authMethods` are offered only to a client that declares terminal
  authentication, which the runner never does: it never offers a sign-in;
- signed out, a turn whose result says "Please run /login" fails with the ACP error `-32000` "Authentication
  required";
- a usage limit is a synthetic assistant message starting with one of twelve prefixes of the SDK
  (`USAGE_LIMIT_ERROR_PREFIXES`), which the adapter classifies `quota_exhausted`.

## Measured by the owner

```bash
claude setup-token                                   # prints your token
read -rs CLAUDE_CODE_OAUTH_TOKEN && export CLAUDE_CODE_OAUTH_TOKEN
trials/2026-12-personal-subscription/run.sh
```

`run.sh` installs the pinned adapter in a directory of its own and runs `probe.py`, which uses the runner's own ACP
client in a per-run home and records: a prompt answered on the subscription, the error of the same prompt without the
token, the precedence of an invalid `ANTHROPIC_API_KEY` set beside it, `session/load` in a new process, and every host
the CLI tunnelled to. It writes `packages/runner/tests/fixtures/subscription/measured.json` and refuses to write it if
the token appears anywhere in it. Commit that file: the tests that read the fixtures then hold the measured values too.

A usage limit cannot be reached on purpose; its texts stay those of the source.
