# The purchasing server against a real Odoo, every night

## What it is for

The Odoo backend of the purchasing server (`purchasing-mcp`, in the `tranibor-agents` repository) is proved there on
hand-written exchanges: they show the code does what its author read in Odoo's documentation, and nothing about
what Odoo does. The nightly job `purchasing-live` (`.github/workflows/nightly.yml`) is that confrontation. It
runs the **published image** of the server against **Odoo 19 and Odoo 20** (a matrix, one run each) and plays all
eleven operations of the profile over MCP, writes and compensations included, then checks that every operation
was played. Odoo and its database are containers the job starts, so nothing is skipped: a service that does not
come up fails the night (finding TST-3).

## How to know it is this

The `nightly failed` issue links a run whose failing job is `purchasing server against Odoo 19.0` or `... 20.0`.
The failing step says which part broke:

| Step | A failure means |
| :-- | :-- |
| `wait for Odoo` | the Odoo image did not answer `/web/health` in five minutes; the step prints Odoo's last 80 lines |
| `initialise the database` | a module did not install (`contacts`, `purchase`, `purchase_requisition`), or a group the integration user needs does not exist in this Odoo (`odoo_keys.py`), or no key was minted |
| `TLS in front of Odoo` | the nginx terminator did not start; the server refuses an `http` base URL, so it cannot be left out |
| `run the purchasing server image` | the image is not published, or refuses its configuration (read its log: `refused_to_start`), or cannot reach Odoo |
| `every operation of the profile` | **a real finding**: see below |

## What runs where

All on the runner, nothing outside it, no secret: Postgres and Odoo as job services (images pinned by digest),
`odoo_keys.py` run by `odoo shell` creates the dedicated user `purchasing-bot` (a Purchase user and a Contacts
manager, not an administrator) and API keys for it and for the administrator, nginx (`nginx.conf`, pinned) gives
Odoo a certificate for `localhost` that only the server trusts (`SSL_CERT_FILE`), and the server image runs on the
host network against `https://localhost:8443`. `tests/live/purchasing/` seeds the database through Odoo's
JSON-2 API with the administrator's key (two suppliers, a product, one open-ended framework agreement and one
contract ending in thirty days: in Odoo the agreement and the contract are the same record, a confirmed blanket
order), then drives the server on `/mcp` with the platform's own MCP client.

Odoo 20 has no registration number on a partner, so there `registration_number` must answer `not_supported`
and the test asserts exactly that; the category filter of the agreements answers `not_supported` on both.

## When the last step is red

1. Open the failing test. The message names the operation and the answer; the three container logs are printed
   at the end of the job (`logs of the three containers`).
2. Decide whose defect it is. If the server asked something Odoo does not accept, or read an answer wrongly, it
   is the server's: open a `finding` issue with `origin: S85-14`, the request, Odoo's answer and the version,
   and fix it in `tranibor-agents` with a **patch version** (then bump `PURCHASING_IMAGE`). **Never relax the
   assertion**: the profile is the contract, a test bent to the backend proves nothing.
3. If Odoo changed (a new digest of the series), the finding is the mapping in the server's `PROFILE.md`.
4. A flaky Odoo start is not a finding; re-run the job once, and open a `finding` if it happens again.

## Updating the pins

- **Odoo and Postgres** (matrix `image`, service `db`): resolve the digest of the new tag
  (`docker buildx imagetools inspect odoo:19.0 --format '{{json .Manifest.Digest}}'`) and replace the whole
  reference; `tests/ci/test_purchasing_live_job.py` refuses a floating image.
- **The purchasing image** (`PURCHASING_IMAGE`): until the agents repository has published `0.2.0` the digest is a
  placeholder of zeros, never pullable, so the job fails at `docker run` on purpose. Once published, write the real
  one (`oras resolve ghcr.io/graalsystems/tranibor-agents/purchasing-mcp:0.2.0` gives it). Each patch release of the
  server is a new tag and a new digest to write here.

## Running it by hand

Without Docker the module fails on its first test (it never skips); the steps of the job are the procedure: start
the two services, `odoo -i base,contacts,purchase,purchase_requisition`, `odoo shell < tests/live/purchasing/odoo_keys.py`,
a TLS terminator, the server image, then set the `PURCHASING_LIVE_*` variables that `tests/live/purchasing/support.py`
lists and run `uv run pytest tests/live/purchasing -m purchasing_live -rs`.

## How to check it is fixed

Run the job again (`workflow_dispatch` on `nightly`); both matrix entries green, and the last test of the module
(`test_every_operation_of_the_profile_was_played`) green, which says all eleven operations were played.
