# 0229 — Surfaces may install and supervise a personal installation and its runner, as separate processes

- **Status**: proposed, 2026-11-26 (the product owner's decisions of 2026-11-26, stream S87; amends
  [ADR 0154](0154-the-terminal-is-a-surface-of-the-platform.md) §1 and §10 and
  [ADR 0155](0155-the-editor-is-a-surface-of-the-platform.md) §1, each quoted under "Context" and given its new
  reading in (1))
- **Concerns**: a new package `tranibor_cli.local` in `packages/cli`
  (`choregos local init|up|down|status|doctor|open|logs|backup|restore|relay|runner`, a bundled copy of
  `deploy/personal/compose.yaml`, ADR 0228's bundle); `tools/cli_binary/build.py` (`FORBIDDEN_MODULES`
  unchanged; the second target `choregos-outpost` is S87-26);
  `apps/api/src/tranibor_api/routers/cli_downloads.py` (the runner's files beside the CLI's); a new
  `packages/contracts/schemas/local-status.schema.json` (S87-08); a new application `apps/desktop` (Tauri 2:
  `src-tauri/src/main.rs`, `src-tauri/capabilities/`, `src-tauri/tauri.conf.json`, `e2e/`);
  `.github/workflows/ci.yml` and `release.yml`. Builds on [ADR 0010](0010-gates-are-mechanisms.md),
  [ADR 0101](0101-governance-the-platform-does-not-enforce-is-refused.md),
  [ADR 0147](0147-an-outpost-only-dials-out.md) §6 and §9,
  [ADR 0148](0148-the-outpost-door-is-a-third-door-with-its-own-contract.md) §13,
  [ADR 0151](0151-the-cli-ships-as-native-binaries.md),
  [ADR 0193](0193-the-phone-is-a-surface-of-the-platform.md),
  [ADR 0226](0226-a-person-s-own-machine-runs-that-person-s-runs.md) (the personal runner) and
  [ADR 0227](0227-a-person-s-own-subscription-is-used-only-by-the-vendor-cli-on-that-person-s-runner.md) (where
  the subscription token lives). Siblings of the same day: the personal installation and its owner ticket
  (ADR 0228), the relay (ADR 0230). Stories S87-26 to S87-30, S87-43, S87-48 to S87-50

## Context

The product owner wants one person to download Choregos and run it for themself, as the CLI, as a desktop app or
with Docker (decisions of 2026-11-26), with agents on their own machine and their own subscription. A personal
installation is a Compose stack on published images (ADR 0228) and the agents run in a personal runner, the
daemon `choregos-outpost` (ADR 0226). Someone has to create the data directory, start the stack, fetch the
runner, write its policy, enrol it and hand it the person's token. The person's own tools are the terminal and,
for someone who does not live in one, a desktop app.

Two records stand in the way as written, ADR 0154 (accepted) and ADR 0155 (proposed):

- ADR 0154 §1: the terminal "never runs a tool, a model or an agent on the person's machine, never edits the
  person's files on an agent's behalf, and holds no model key", and §10 lists "No local tool, model, agent or
  file edit (§1); no offline mode; no administration screens".
- ADR 0155 §1: the editor "runs no tool, model or agent, holds no model key, and never writes in the person's
  workspace on its own".

A surface that starts a daemon which runs agents on the same machine contradicts the first sentence. What those
records protect still holds: an agent runs where the platform placed it, under its governance, and never inside
a surface. This record moves the line from "the machine" to "the surface's own process", and says what each
surface may start.

## Decision

1. **A surface runs no tool, model or agent in its own process; it may start and stop, as separate processes, a
   personal installation and its personal runner.** ADR 0154 §1 now reads: `choregos` "never runs a tool, a
   model or an agent in its own process, never edits the person's files on an agent's behalf, holds no model key
   and keeps no subscription credential; it may start and stop, as separate processes, a personal installation
   and its personal runner". ADR 0155 §1 takes the same clause, and the desktop app (6) is under it. ADR 0154
   §10's "No local tool, model, agent or file edit (§1)" reads under that clause: no surface runs an agent or
   edits a file on an agent's behalf, and the processes it starts run what placement sent them. Its "no
   administration screens" stands: `choregos local` drives the person's own installation from outside it and
   changes none of the platform's members, assistants, needs or budgets. An agent runs where placement put it
   (ADR 0226 §7): a platform executor or the work owner's runner, never a surface. Held by
   `tests/architecture/test_cli_local_imports_no_server.py`: it imports `tranibor_cli.local` in a subprocess and
   fails if `sys.modules` holds an entry of `FORBIDDEN_MODULES` (read from `tools/cli_binary/build.py`, not
   copied), `tranibor_runner`, `tranibor_outpost` or the Docker SDK (`docker`). The binary's build still fails
   on a forbidden module (`forbidden_in`); the list does not change.
2. **`choregos local` drives Compose by subprocess, at the CLI's own version.** `init` writes the data directory
   (per OS), `secrets.env` and the owner key at mode 0600 (ADR 0228); `up` runs the bundled compose file with
   the images of the CLI's own version, waits for health and starts the runner (4); `down`, `logs`, `backup` and
   `restore` (S87-30), `relay` (ADR 0230, S87-43) and `runner` complete it. Every call to Docker is a subprocess
   of Compose v2, against the Docker Engine or Podman's Docker-compatible socket; `doctor` says which it found
   and refuses Compose v1 and a missing engine with a message. The bundled compose file is byte-identical to
   `deploy/personal/compose.yaml`. `status`, `doctor`, `open --print-url` and `relay enable` take `--json` and
   answer `local-status.schema.json`, the only shape the desktop app reads. Held by
   `packages/cli/tests/test_local_commands.py` (a fake `docker` on `PATH` records each argv; Podman detection;
   every `--json` output validated against the schema; the byte equality).
3. **The runner comes from the stack, verified, at the stack's version.** `up` fetches `choregos-outpost` for
   this OS and processor from the stack it started, at `/downloads/outpost/`, served as `/downloads/cli/` is
   (`routers/cli_downloads.py`): the API image carries the files and a manifest of names and sha256 digests
   (S87-26). The CLI refuses a manifest whose version is not the stack's, checks the digest before the file is
   made executable, and replaces the binary atomically when the stack's version changes. Replacing it is the
   person's gesture on their own machine, not the door's: no verb of the door makes a daemon download or run
   anything (ADR 0147 §9 holds). The desktop app passes its bundled sidecar instead (`up --runner-binary`),
   checked the same way. Windows has no native runner (ADR 0226 §10): `choregos local` starts the bundle's
   `runner` profile, whose container is the only one that mounts the Docker socket (S87-27). Held by
   `packages/cli/tests/test_local_runner.py` (`::test_a_binary_whose_digest_differs_is_refused`,
   `::test_a_runner_of_another_version_is_refused`).
4. **The CLI writes the daemon's policy, enrols the runner as its owner, then starts it.** The policy
   (`outpost-policy.schema.json`) names the loopback door as `control_plane` and, as `door_keys`, the public
   half of the door signing key that `init` generated into `secrets.env`: computed by the CLI, never learned
   from an answer of the door (ADR 0148 §13). It lists no endpoint, since a personal outpost serves runs, not
   tools (ADR 0226 §1), and it takes docker mode only; native is the person's explicit choice (ADR 0226 §10).
   `up` signs an owner ticket on the loopback door (ADR 0228), creates the enrolment through `/me/runners`
   (ADR 0226 §2, the one API the CLI and the app share), hands the one-time token to
   `choregos-outpost enrol --token-file` through a 0600 file it removes afterwards, and approves the instance
   only when the fingerprint the door lists equals the one the daemon reports from its own identity. Then it
   starts `choregos-outpost run` as a detached process of its own, with a pid file and a log in the data
   directory. `down` stops the runner (SIGTERM, the daemon's grace) before the stack; `status` reads its pid and
   its presence in `/me/runners`. Held by `packages/cli/tests/test_local_runner.py` (a fake runner binary and a
   fake loopback API: `::test_up_enrols_approves_then_starts`, `::test_a_fingerprint_mismatch_is_not_approved`,
   `::test_the_policy_pins_the_key_init_generated`, `::test_down_stops_the_runner_first`).
5. **`choregos local runner login claude` passes standard input through and reads nothing.** It starts
   `choregos-outpost personal login claude` (ADR 0227 §4) with the CLI's own standard input handed to the child
   as it is: the CLI never reads it, writes it or logs it, never puts it in an argument or an environment
   variable, and has no `--token` option. On Windows the same command runs in the runner's container through
   `docker compose exec -T`, standard input attached. Held by
   `packages/cli/tests/test_local_runner.py::test_login_passes_stdin_through`: a fake runner records its argv,
   its environment and what it read; a canary token appears only in what it read, and not in the CLI's output,
   error stream or log.
6. **The desktop app is a fifth surface, and a supervisor.** After the console, the terminal, the editor and the
   phone (ADR 0193), `apps/desktop` is a Tauri 2 application whose sidecars are `choregos` and, on macOS and
   Linux, `choregos-outpost`, both at the version of the app's own release. It knows the installation only
   through its sidecars' `--json`: `choregos local … --json`, which folds in the runner's own
   `choregos-outpost doctor --json`, parsed against `local-status.schema.json`. It holds no session and calls no
   route of the platform's API itself. First launch runs `doctor` (it explains a missing Docker or Podman), then
   `init` and `up`, then opens the console. A tray shows the stack, the runner and the relay; when `status`
   reports the runner down, the app runs `up` again. On quit the stack is stopped or left running, by a setting.
   A relay screen runs `choregos local relay enable --json` and shows the pairing code (ADR 0230). Held by
   `apps/desktop/src-tauri/tests/supervisor.rs`: a fake CLI sidecar replays fixtures that validate against the
   schema (first launch, a failure, a retry, a runner restarted).
7. **The console is a WebView of the loopback door, and it gets no IPC.** The console window loads the URL that
   `choregos local open --print-url --json` returns (an owner ticket, ADR 0228) and stays on the loopback
   origin; any other origin opens in the system browser. The console is code the platform serves, not code the
   app ships, so no capability file names the console window, none carries `remote` URLs, and no window is
   granted the shell, file-system, HTTP or process plugins; sidecars are started from Rust only. Held by
   `apps/desktop/src-tauri/tests/capabilities.rs` (it reads every file of `src-tauri/capabilities/` and fails on
   a `remote` entry, the console window's label, a wildcard window or a permission outside the closed list of
   the app's own commands, and tests the one function that decides where the console window may navigate), and
   by `apps/desktop/e2e/first-launch.spec.ts`, which calls `invoke` from the console window and expects a
   refusal.
8. **"Connect Claude" is a native window with bundled assets, and the token goes to the runner's standard
   input.** The window shows a page the app bundles (content security policy `default-src 'self'`, nothing
   remote), never a page of the console, so no platform-served code can read the field. It tells the person to
   run `claude setup-token` themselves, Anthropic's own sign-in, and to paste what it prints; the app never runs
   the vendor's sign-in and never offers a Claude.ai login (ADR 0227 §5). Its one command spawns
   `choregos-outpost personal login claude` (on Windows, `choregos local runner login claude`) with a pipe as
   standard input, writes the token, closes the pipe, zeroises the Rust buffer and closes the window. The token
   is never in an argument, the environment, a file, a log line or the app's settings. Held by
   `apps/desktop/src-tauri/tests/connect_claude.rs` (a fake runner sidecar records argv, environment and
   standard input; the canary reaches standard input only; the app's log and data directory hold none of it).
9. **Targets, signing and updates.** macOS arm64 and x86_64 (a universal disk image), Windows x86_64 (WebView2,
   an installer), Linux x86_64 (AppImage and deb). Developer ID signing with notarization, and Authenticode, are
   blocked by BLOCKERS S62-07: until the certificates exist, `release.yml` skips those steps as it does for the
   CLI, the files are published with `SHA256SUMS` and a keyless cosign signature (ADR 0151 §1, §5), and the
   release notes say that Gatekeeper refuses them, SmartScreen warns and a managed Windows blocks them. The
   updater is Tauri's, with an Ed25519 key of its own held apart from the operating systems' certificates; it
   refuses a manifest whose signature does not verify, and installs only on the person's gesture. An app update
   changes the CLI, so the stack's version: `choregos local up` takes a backup (S87-30) before it pulls images
   of another version and stops if the backup fails; the data directory records the version that last migrated
   it, and an older CLI refuses `up` (`local_downgrade_refused`) and names a restore as the way back. Held by
   `apps/desktop/src-tauri/tests/updater.rs` (a wrong signature, a tampered manifest) and
   `packages/cli/tests/test_local_commands.py` (`::test_a_version_change_backs_up_first`,
   `::test_up_refuses_a_data_directory_migrated_by_a_newer_version`).
10. **CI tests what each operating system can run.** Rust tests with the fake CLI run on Linux, macOS and
    Windows. The tauri-driver end-to-end test runs on Linux (a virtual display, WebKitGTK, the real stack under
    Docker: first launch to a signed-in console and a work item) and on Windows (WebView2 and the fake CLI: the
    interface only). The real stack runs on Linux only: hosted macOS and Windows runners run no Linux container.
    macOS gets the build and the Rust tests, since WKWebView has no WebDriver; the status row of S87-49 says so.

## What is refused, and what is only recorded

| Concern | Refused, by a test | Only recorded |
|:--|:--|:--|
| The surface's process | a server, runner, outpost or Docker SDK module under `tranibor_cli.local`; a forbidden module in the binary | the CLI's own memory, which never holds the token |
| The runner binary | a digest that differs from the manifest; a version other than the stack's | that the binary is as trustworthy as the image that serves it; the cosign signature is checked only where `cosign` is installed |
| Enrolment | an approval whose fingerprint differs from the daemon's own; door keys learned from the door | who enrolled and approved (ADR 0226 §2, audit `outpost.approve`) |
| The token | an argument, an environment variable, a log line, a file or a setting of the CLI or the app | the field's value in the bundled window's JavaScript heap until the window closes: a JavaScript string cannot be zeroised |
| The console window | any IPC; navigation to another origin; the shell, file-system, HTTP and process plugins | that the console runs the platform's code with the person's session, as a browser does |
| Updates | an update manifest whose signature fails; a stack upgrade without a backup; a downgrade past a migrated schema | unsigned installers until S62-07, said in the release notes |

The machine is the person's. Whatever runs with their rights can read the runner's keychain entry or its 0600
file, and the surfaces do not pretend otherwise (ADR 0227, "Recorded, and not prevented").

## Unchanged, on purpose

- **ADR 0151 §3**: the binary holds what the CLI needs and nothing else; `FORBIDDEN_MODULES` does not change.
  `cryptography` joins the CLI for the owner key and its tickets, and is not a server module.
- **ADR 0154 §6 and §7**: a decision is taken in the console's own session with a fresh authentication; the
  desktop app adds no gesture and no route.
- **ADR 0155's rejected "Driving the `choregos` CLI as a child process"** stands for the editor, which builds
  none of this record.
- **ADR 0147 §9**: no verb of the door upgrades a daemon; the CLI replaces the runner on the person's gesture.
- **ADR 0168**: `local-status.schema.json` is not one of the five frozen surfaces. The app always calls the CLI
  sidecar of its own release, never a `choregos` found on `PATH`, so the two read the same version of it.

## Alternatives rejected, and when each wins

| Alternative | Wins when | Loses when | Verdict |
|:--|:--|:--|:--|
| The runner inside the CLI's or the app's process | one binary to ship | a model loop and the token in a surface; the binary carries the runner and the build's forbidden list stops meaning what it says | **Refused** |
| "Connect Claude" as a field of the console | one screen | the token passes through code the platform serves, over the loopback door or the relay: the platform intermediates it (ADR 0227) | **Refused** |
| Tauri IPC granted to the console origin (`remote` URLs) | the console drives the app | any script the platform serves, or one injected into it, reaches the credential command and the sidecars | **Refused** |
| The app as a REST client of its own | a native view of the work | a second console to keep, and a session in the app | **Refused**; the console in a WebView |
| The Docker SDK, or the socket, from the CLI | no `docker` binary needed | the binary grows, the daemon's API version leaks in, and Podman's support differs | **Refused**; Compose by subprocess |
| The runner downloaded from the public release | no file in the API image | needs the internet and a second source of truth for the version | **Refused**; the stack serves it |
| Electron | a web team's tooling | a bundled Chromium per app, a larger footprint; the product owner chose Tauri | **Refused** |
| Updates installed silently | no gesture | a stack upgrade migrates the schema and cannot be undone | **Refused**; offered, then installed |

## Consequences

- The API image carries the runner's files for four targets beside the CLI's five, and `cli_downloads.py` serves
  them under the same rate limit.
- A person who uses only the CLI restarts a stopped runner (a crash, a reboot) with `choregos local up`;
  starting at login is the desktop app's setting.
- The desktop app does not reach people who cannot override Gatekeeper or SmartScreen until S62-07 is resolved;
  that row of BLOCKERS names the app when S87-50 lands.
- The repository gains a Rust application with its own lockfile, a CI job (`desktop`) and a release artefact per
  target; its tests run on fakes, like every client (ADR 0005).

## Not proved

This record is the decision; it proves nothing. That Tauri 2 refuses IPC to a window no capability names is read
from its documentation: the end-to-end test proves it on WebKitGTK and WebView2, and WKWebView is never driven.
How WebKit treats the loopback door's cookie in a WebView is untried. Podman is detected and tested with a fake;
no real Podman run is in CI. No installer has been signed or notarized.
