# 0194 — A person's devices are told by Web Push: the server side, and a service worker that only notifies

- **Status**: proposed, 2026-11-26 (the product owner's decisions of 2026-11-26, stream S87; amends
  [ADR 0193](0193-the-phone-is-a-surface-of-the-platform.md)'s rejected alternative "A progressive web app", for
  notifications only, and [ADR 0130](0130-a-browser-session-is-recorded-and-its-owner-ends-it.md) §4; the number
  was reserved by ADR 0193 and by stories S81-02 and S81-03)
- **Concerns**: a new `routers/push_subscriptions.py` (`GET /push/vapid-public-key`, `/me/push-subscriptions`), a new
  `services/web_push.py` (`web_push_loop`, `payload_of`, `TITLES`) and the tables `push_subscriptions` and
  `push_deliveries`; a new sender `tranibor_adapters.notify.web_push` (`WebPushSender`, `HttpWebPushSender`,
  `FakeWebPushSender`); `services/my_pending.py` (`pending_for`, read and unchanged); `browser_sessions.py`
  (`end_session`, `end_other_sessions`, `revoke_user_sessions`); the settings `TRANIBOR_WEB_PUSH_VAPID_PRIVATE_KEY`,
  `TRANIBOR_WEB_PUSH_SUBJECT`, `TRANIBOR_WEB_PUSH_INTERVAL_S` and `TRANIBOR_WEB_PUSH_CONFIRM_DAYS` (`config.py`); a
  new `charts/choregos/templates/web-push.yaml`; `choregos local init` and `secrets.env`
  ([ADR 0228](0228-a-personal-installation-is-one-person-s-production.md) §7); `packages/contracts/openapi.yaml` and a
  new `packages/contracts/schemas/web-push-payload.schema.json` (S81-02); in `apps/web`, a new `public/sw.js` and
  `public/manifest.webmanifest`, the CSP of `src/proxy.ts` and a Notifications card on My account. Builds on ADR 0193
  §4, [ADR 0005](0005-adapters-and-fakes.md), [ADR 0010](0010-gates-are-mechanisms.md),
  [ADR 0101](0101-governance-the-platform-does-not-enforce-is-refused.md),
  [ADR 0114](0114-what-a-run-writes-after-reading-a-restricted-base-stays-restricted.md) §6,
  [ADR 0154](0154-the-terminal-is-a-surface-of-the-platform.md) §4 and §7 and
  [ADR 0170](0170-sign-in-security-moves-into-the-core.md). Siblings of the same day: the personal runner
  ([ADR 0226](0226-a-person-s-own-machine-runs-that-person-s-runs.md)), the personal installation (ADR 0228), the
  surfaces that install it
  ([ADR 0229](0229-surfaces-may-install-and-supervise-a-personal-installation-and-its-runner.md)), the relay
  ([ADR 0230](0230-a-hosted-relay-reaches-a-personal-installation-by-tls-passthrough.md)), personal notices (ADR 0232)

## Context

ADR 0193 §4 decided that what waits for a person reaches their phone by Web Push (RFC 8030, the payload encrypted
per RFC 8291, VAPID per RFC 8292), through UnifiedPush in the Android app, and left the server side to this number.
Stories S81-02 and S81-03 cite it. No file was written and nothing of it is built: no route, no key, no sender
(`docs/plan/PLAN-2026-11-26-personal-use.md` §1).

The product owner's first situation (2026-11-26) makes it needed now. One person runs Choregos on their own
computer and uses it from their phone through a relay (ADR 0230). Their agents run on their runner and stop at a
decision: a human step, a governed action, a write or a launch an assistant proposes. `GET /me/pending`
(`services/my_pending.py`) lists it, and nobody is told: the console is a browser tab the phone has closed, and the
Android app (S81-04 to S81-07) is not built. ADR 0193 does not cover iPhones at all.

Three facts bound the answer.

- A browser receives Web Push only through a service worker that a page registers on its own origin. Safari on iOS
  (16.4 and later) delivers it only to a web app added to the Home Screen, which needs a web manifest whose
  `display` is `standalone`. ADR 0193 rejected "**A progressive web app.** No foreground service for a long voice
  session, no reliable audio route, and Web Push in a mobile browser depends on the browser's vendor."
- The relay is operated by a third party and carries connections into the installation by SNI, without terminating
  TLS (ADR 0230). A push service is a third party too: RFC 8291 hides the payload from it, not the endpoint, the
  time, the size or the sender.
- A notification leaves the platform's rights. It sits on a lock screen, outlives a sign-out and passes a push
  service. ADR 0193 §4 already said what it may carry: "a kind, a title, one line and references, never content the
  person would need the platform's rights to read".

The product owner decided on 2026-11-26: Web Push in the browser, with a notification-only service worker.

## Decision

1. **A person registers their devices; the installation publishes one public key (S81-02).**
   `GET /push/vapid-public-key` answers a signed-in person with the installation's VAPID public key (base64url, the
   uncompressed P-256 point the browser's `applicationServerKey` takes). `POST /me/push-subscriptions` takes the
   browser's `PushSubscription` (`endpoint`, `keys.p256dh`, `keys.auth`), a `device_name` and the `kinds` the device
   wants (all by default); it creates the row, or confirms it when the same endpoint is posted again (6).
   `GET /me/push-subscriptions` lists the caller's: id, device name, the push service's host, kinds, created,
   confirmed, last success, last failure. `DELETE /me/push-subscriptions/{id}` ends one;
   `POST /me/push-subscriptions/{id}/test` sends a message of kind `test`, once a minute at most (429 beyond).
   - *Who.* The routes need the person's own session with a `user_sessions` row: `principal.credential` is `cookie`
     or `surface` and `principal.session_id` is set. An API token, a machine account, a run token, an outpost and an
     embedded frame get 403.
   - *What.* The endpoint is `https`, on port 443, without user-info, named by a host that is not an IP literal and
     whose addresses are all public (`tranibor_adapters.documents.web.is_private_address`); `p256dh` is a point of
     P-256 and `auth` is 16 octets. Otherwise 422, before anything is stored. One endpoint belongs to one person:
     posted by another person, it moves to them and the earlier row is deleted. Ten subscriptions per person at
     most (409 beyond).
   - *Where.* `push_subscriptions` sits under forced row-level security with a restrictive owner-only policy, as
     `connected_accounts` does (migration `20261110_1600_connected_accounts.py`). No route returns an endpoint's path
     or a key after creation; a log line names the host only. Creation, deletion and a move are audited
     (`push.subscription.create`, `push.subscription.delete`, `push.subscription.move`).
2. **One VAPID key pair per installation.** `TRANIBOR_WEB_PUSH_VAPID_PRIVATE_KEY` (PEM, P-256) is checked when the
   settings load, as `TRANIBOR_OUTPOSTS_DOOR_SIGNING_KEY` is (`config.py`): another curve or a broken key refuses the
   start. `TRANIBOR_WEB_PUSH_SUBJECT` is the RFC 8292 contact, a `mailto:` or `https:` URI, the public URL by
   default. A personal installation gets its pair from `choregos local init`, in `secrets.env` at mode 0600 (ADR
   0228 §7, ADR 0229). The chart reads a Secret the operator names, or draws a P-256 key at the first installation
   (`genPrivateKey "ecdsa"`, as `templates/embedded-secrets.yaml` draws the run-token key) into a Secret it keeps,
   marks immutable and reads back with `lookup`, as `templates/outposts-door.yaml` does for the door's key. Two
   installations never share a pair, and the relay never holds one. A new pair orphans every subscription (a push
   service refuses a token signed by another key); the console subscribes again at its next load, and
   `docs/deployment.md` says so. Without a key, Web Push is off: the key route answers 404 and a subscription 409,
   both `web_push_off`. Under `TRANIBOR_FAKES=1`, `FakeWebPushSender` records what it would send (ADR 0005).
3. **What is pushed is what already waits: read, not hooked.** `web_push_loop`, a task of the API's lifespan beside
   `task_loop` (`main.py`), passes every `TRANIBOR_WEB_PUSH_INTERVAL_S` (15 s by default). For each person who holds
   a live subscription it calls `pending_for`, the very function `GET /me/pending` answers with: a device is told
   only what its person may decide, by the rule of the route that decides it, under their rights at that pass.
   - An item that appeared after a subscription was created and is not yet recorded for it becomes one row of
     `push_deliveries`, unique on (subscription, kind, reference) and inserted with `ON CONFLICT DO NOTHING`: a
     second replica, or a pass replayed after a crash, sends nothing twice (ADR 0008). More than three new items for
     one person in one pass become one message of kind `several`.
   - Two other sources write rows: runs that wait for the person's offline runner, one message per runner per hour
     (S87-55, `services/outpost_runs.py`, kind `runner_waiting`), and in phase B the personal notices of ADR 0232,
     whose Web Push channel is this sender. Event proposals (S87-67) are listed by `/me/pending` and come through
     the read with no source of their own.
   - Nothing else is pushed: no inbox delivery, agent notification, run end or conversation message. A new kind is a
     contract change of the payload's `kind`, never free text. A row whose item has left `/me/pending` before it is
     sent is dropped as `moot`.
4. **The payload: a kind, a title, one line, a reference.** `web-push-payload.schema.json`
   (`additionalProperties: false`) holds `v`, `kind`, `title`, `line` (the `body` of S81-02's note), `org`, `ref`,
   `url` and `at`. `kind` is one of `human_step`, `governed_action`, `conversation_action` and `launch` (the values
   of `PendingKind`), `several`, `runner_waiting` and `test`. One function builds it, `web_push.payload_of`:
   - the title comes from a closed table per kind (`TITLES`);
   - the line names where the thing waits, by references only: the project's slug and the work item's key, the
     assistant's slug for a proposal, the runner's name and a count for waiting runs. It never holds
     `PendingItem.what` (a step's question or summary, an action's title, a proposal's summary), an argument, a
     conversation's text, a run's output or a cost. A marked work item (ADR 0114 §6) gets `WITHHELD`
     (`tranibor_core.restricted`) as its line;
   - `ref` is the item's kind and id; `url` is the item's path in the console.

   The payload is encrypted per RFC 8291 (content coding `aes128gcm`, RFC 8188) to the subscription's keys and
   padded to one length (`PADDED_TO`, 1,024 octets), so its size tells the push service nothing of its kind or line.
   RFC 8291 and RFC 8292 are implemented on `cryptography` and PyJWT, which `tranibor_adapters` already depends on:
   no push library, no vendor's SDK.
5. **Delivery goes from the installation to the push service, never through the relay.** `HttpWebPushSender` POSTs
   to the endpoint (RFC 8030 §5) with `TTL` (a day; a minute for `test`), `Urgency` (`high` for a decision, `normal`
   otherwise), `Topic` (a digest of kind and reference, so a push service replaces a copy it has not delivered),
   `Content-Encoding: aes128gcm` and `Authorization: vapid t=…, k=…` (RFC 8292: `aud` the endpoint's origin, `exp`
   12 hours at most, `sub` the subject). The host is resolved and checked again at each send and the connection is
   pinned to the address checked, as `services/web_fetch.py` does; no redirect is followed.
   - 201: `last_success_at`. 404 or 410: the subscription is deleted (S81-03). 429, 5xx or a network failure:
     retried with a growing delay, five attempts, then dropped and counted. Any other answer: not retried, recorded
     as the subscription's `last_failure` and shown on My account.
   - The loop reads committed rows only: a push service that is down or slow never blocks, delays or fails the
     decision, the step or the run that made something wait.
   - The relay of ADR 0230 is not on this path. Its agent dials the relay's host only and splices each stream it is
     handed to the edge's relay listener alone (ADR 0230 §3): it carries connections into the installation, never
     out of it. The relay never receives an endpoint, a payload or the VAPID key, and its operator does not learn
     that a push was sent (ADR 0230 §11 says the same from its side).
   - On the chart, the API's pods reach push services only where the operator's egress opens them
     (`networkPolicies.platformEgress`); with egress closed, deliveries fail and are counted, and nothing else
     changes. A log line names the push service's host, the kind and the outcome, never the endpoint's path, a key
     or the payload.
6. **A subscription lives with the session that confirmed it.** Each subscription records the session it was
   registered or last confirmed from (`session_id`, `confirmed_at`). The console posts its subscription again at
   every load, the Android app at every start. Ending that session deletes the subscription in the same transaction,
   which amends ADR 0130 §4 ("`DELETE /me/sessions/{id}` ends one; `POST /me/sessions/end-others` ends all but the
   one the request came with"): signing out (`POST /auth/logout`), either of those routes, an administrator's
   revocation, a passkey revoked (ADR 0228 §4); `browser_sessions.end_session`, `end_other_sessions` and
   `revoke_user_sessions` do it. A subscription not confirmed for
   `TRANIBOR_WEB_PUSH_CONFIRM_DAYS` (30) is stale: the loop sends it nothing and deletes it. A browser session's eight
   hours do not end it, so the person need not keep a tab open; a device on which nobody has opened the console for
   a month is told nothing. A member removed from an organisation needs nothing more: `pending_for` stops listing
   its items at the next pass.
7. **The console ships a service worker that only notifies, and a web manifest** (the product owner's decision of
   2026-11-26).
   - `apps/web/public/sw.js` is registered at scope `/` only after the person asks for notifications on the
     Notifications card of My account; `Notification.requestPermission()` runs on that button, never on load.
   - It listens to `push` and `notificationclick` and to nothing else. It has no `fetch` listener, so every request
     goes to the network as if it were absent; it opens no cache (`caches`), imports no script (`importScripts`) and
     makes no request of its own.
   - On `push` it shows the title and the line with the console's icon and no `actions`: a notification carries no
     decision, as a tracker comment carries none (ADR 0102) and a Slack notice carries a link only (S87-69). On
     `notificationclick` it focuses or opens `url` resolved against its own origin; an address on another origin
     opens the console's home. The decision is taken there, in the person's session, with the fresh authentication
     ADR 0170 asks for (a passkey on the relay's door, ADR 0228 §4).
   - `apps/web/public/manifest.webmanifest` gives a name, icons, `start_url` and `scope` `/`, and
     `display: standalone`, which iOS needs to deliver Web Push to a Home Screen web app. The CSP of `src/proxy.ts`
     names `worker-src 'self'` and `manifest-src 'self'`, since its `script-src` carries `'strict-dynamic'`.
   - The console offers no install: no `beforeinstallprompt` handler, no banner. On an iPhone the card says to add
     the page to the Home Screen first, which is Safari's own gesture. It is not a progressive web app: no offline
     shell, no page served offline, no background or periodic sync.
8. **The Android app uses the same server side.** Under ADR 0193 §4 and S81-06 it registers its UnifiedPush endpoint
   and the keys it generated on the same route, from its device-flow session (a `user_sessions` row of its own
   kind), decrypts per RFC 8291 itself and maps `ref` to its own screens. The server names no push vendor and no
   distributor.

Each guarantee is held by a test written with its story (ADR 0010):

- `apps/api/tests/test_push_subscriptions.py`: the routes and each refusal above; the owner-only policy on
  PostgreSQL; a canary written in a step's question and in an action's title, absent from every plaintext payload;
  `WITHHELD` for a marked work item; one send for two passes and two loops; `moot` after a decision; 404 and 410
  delete; 5xx retried, then dropped; an ended session and a stale subscription get nothing; the sender connects to
  the endpoint's checked address and to nothing else.
- `packages/adapters/tests/test_web_push.py`: RFC 8291 Appendix A as a known answer; the VAPID header; the padding.
- `tests/charts/test_web_push_key.py`: the drawn Secret is kept, immutable and read back; a named Secret is used.
- `apps/web/e2e/push.spec.ts`: Chromium, with the API's fake sender and the DevTools protocol's
  `ServiceWorker.deliverPushMessage` standing in for the push service. Subscribe from My account; a waiting step is
  shown with its title and line and no action; the click opens the item; the console offline gives the browser's
  own error page; `caches.keys()` is empty; `sw.js` registers `push` and `notificationclick` only.

## What is refused, and what is only recorded

| Case | Answer | Where |
|:--|:--|:--|
| A subscription from an API token, a machine account, a run token, an outpost or an embedded frame | 403 | `routers/push_subscriptions.py` |
| An endpoint not `https`, off port 443, with user-info, an IP literal or a private address; a malformed key | 422 at registration; not sent at delivery | `routers/push_subscriptions.py`, `HttpWebPushSender` |
| An eleventh subscription; any subscription without a VAPID key | 409 (`web_push_off` for the second) | `routers/push_subscriptions.py` |
| A VAPID key of another curve, or unreadable | the API refuses to start | `config.py` |
| A push for an item the person may not decide | never queued | `pending_for` |
| An item's content in a payload | no path: `payload_of` reads references only; `WITHHELD` for a marked work item | `services/web_push.py` |
| A push to a subscription whose session was ended, or not confirmed for 30 days | not sent; the subscription deleted | `web_push_loop` |
| A push service answering 404 or 410 | the subscription deleted | `HttpWebPushSender` |
| A push through the relay | no path: the relay carries connections in only | ADR 0230 §3 and §11 |
| A notification that approves or rejects | none: no `actions`; the console decides, freshly authenticated | `sw.js` |
| A page served or cached by the service worker | none: no `fetch` listener, no cache | `sw.js` |
| A subscription created, deleted or moved to another person | recorded: the audit log | `routers/push_subscriptions.py` |
| A delivery that failed | recorded: `last_failure`, counted | `push_deliveries`, `push_subscriptions` |

Recorded, and not prevented:

- **A push service learns the metadata.** Which device the installation pushes to, when, how often, the padded size,
  the VAPID public key and the subject (the installation's public URL by default); for a personal installation, the
  address of the person's own connection. RFC 8291 hides the payload, not these.
- **A lock screen is readable by whoever holds the phone.** The line names a project and a work item's key; a
  person who wants nothing shown hides notification content in the phone's settings.
- **The vendor decides delivery.** An iPhone receives push only in a web app added to the Home Screen, and a
  browser's vendor may drop or delay a message (ADR 0193's reason, still true).
- **A notification shown stays shown** after its item is decided or its session ended; nothing withdraws it.

## Alternatives rejected

| Alternative | Wins when | Loses when | Verdict |
|:--|:--|:--|:--|
| A vendor's SDK (Firebase Cloud Messaging, APNs) | one vendor's devices, richer features | it names a vendor (ADR 0193 §4) and the server holds the vendor's credential | **Refused** |
| Pushes sent or carried by the relay | the installation has no route out | the relay's operator would see every person's endpoints and timing; the relay carries connections in only (ADR 0230) | **Refused** |
| A progressive web app (offline shell, cached pages) | a phone without a network | cached pages keep content on the device after a sign-out or a change of rights; ADR 0193's reasons hold | **Refused**, except for notifications |
| A hook at each place an item starts to wait | no periodic read | each writer repeats who may decide (`pending_for`'s rules); a hook missed tells the wrong person, or nobody | **Refused** for `/me/pending`; kept for runs waiting, which no list holds |
| The item's label (`what`) as the line | more useful on a lock screen | content leaves the platform's rights for a device and a push service | **Refused** |
| A subscription that ends when its session expires | one rule | a browser session lasts eight hours: the phone is told nothing the same evening | **Refused**: ended with an ended session, stale after 30 days |
| Approve and reject buttons in the notification | one tap | a decision without the session and the fresh authentication | **Refused** |
| A push library (`pywebpush`) | less code | a dependency for two RFCs that `cryptography` and PyJWT cover, and a known answer tests ours | Not taken |

## Consequences

- Two tables under row-level security, a loop, five routes (one contract change, S81-02, under ADR 0001; a client in
  `apps/web/src/lib/api.ts`, as `tests/web/test_api_routes_have_a_client.py` asks), a sender, a service worker and a
  manifest. The migrations are security paths owed a human review.
- The read costs one `pending_for` per subscribed person per pass: nothing on a personal installation. An
  organisation with many subscribed members raises the interval; the outbox of ADR 0232, written with the act, is
  the way to retire the read if it ever weighs.
- ADR 0193 keeps the Android app, and its "iOS is not covered" now holds for the app only.
- Stories: S87-06 writes this record; S87-45 delivers S81-02 and S81-03 for every installation; S87-55 adds runs
  waiting; S81-06 registers the Android app; S87-46 and S87-47 use it in the phone's flow. The record proves
  nothing; each guarantee above names the test that will hold it.

## Not proved

No push has been sent by Choregos. Untried: Safari's delivery to a Home Screen web app opened on the relay's
hostname; how long push services keep an endpoint valid without a visit; whether Chrome on Android offers its own
install for a manifest whose service worker has no `fetch` listener; `Topic` replacement across push services;
whether `ServiceWorker.deliverPushMessage` exercises the path a real push takes.
