# Durable subscriptions and agent delivery

Current delivery work and priority: [BACKLOG.md](BACKLOG.md), DK-08–DK-10.
This document is the API and delivery contract.

Status: since **schema 32** (current schema 40) DeedKit implements persistent structural subscriptions, passive
inspection, independent fetch/ACK, bounded long polling, native wake selectors,
operator-registered signed hints and private feedback. Schema 38 adds hint timing:
debounce or batch windows, quiet hours, expected-reply silence timers and a per-key
hourly hint budget. Installing the package
does not register a receiver, create a subscription, activate a provider or
verify a host turn. Unsolicited MCP resource notifications remain future work.

## Available API

The core operations are MCP tools, `/v1/<operation>` POST endpoints and
Python/npm client methods:

| Operation | Arguments | Behavior |
| --- | --- | --- |
| `create_subscription` | `chat_ids`; optional `sender_ids`, `kinds`, `direction`, `origins`, `label`, `expires_at` | Freeze an authorized source set at the committed journal head |
| `list_subscriptions` | Optional `limit` (1–100) | List this exact credential's definitions; `has_more` is explicit |
| `get_subscription` | `subscription_id` | Inspect lifecycle, match frontier and gap evidence |
| `update_subscription` | `subscription_id`, current `version`, `action`; optional `resume_policy` | Pause, resume or close with optimistic concurrency |
| `get_subscription_events` | `subscription_id`; optional `after`, `limit` (1–100) | Read immutable event references; return `next_after`, without ACK |

Creation and lifecycle changes require **both `read` and `subscription-write`**
in the principal policy and credential ceiling. Inspection requires `read`.
Existing credentials do not inherit the new permission. A definition belongs to
its original key as well as its principal; a different key cannot adopt it.

`chat_ids` are archive IDs obtained from the scoped chat catalog, with 1–64
explicit sources. Optional senders are verified against selected chats and remain
account-scoped. An equal sender string observed in two accounts is ambiguous;
create separate subscriptions instead of treating it as one person. Sources are
immutable in this release: close and recreate to change filters. A label is
routing context, never authority to select a host task.

Defaults are `kinds=['message.created']`, `direction='incoming'`,
`origins=['live']`. An empty direction selects both directions. Explicit origins
can include `backfill`, `reconcile` or synthetic `fixture`; historical matches
are marked. Expiry is optional, timezone-aware, in the future and at most 366
days ahead. No messages before the creation frontier are replayed.

Lifecycle is `active`, `paused`, `closed`, `expired` or `access_changed`.
Resume defaults to `catch_up`; explicit `skip` records the intentionally skipped
journal range. Re-read the version after a `subscription_conflict`. Any principal
scope or credential-permission version change invalidates the original grant,
even if rights are later restored. Inaccessible definitions expose only a masked
ID/state/version, and must be recreated after checking the current scope.

Pages contain event/message/account/chat/sender/provider references, exact
revision, observation/provider time, origin and optional processing evidence.
They do not copy message bodies, transcripts or file bytes into subscriber
queues; use existing context/file tools to retrieve authorized content.
`after` is a caller-maintained position, **not a server ACK**. Passive reads do
not mark messages read in Telegram/WhatsApp or advance archive consumers.

`matcher_caught_up` means caught up to the **committed archive journal**. It does
not prove a provider/chat is current or its history complete; check scoped sync
status separately. A journal hole returns `journal_gap`; a full reference budget
returns `match_budget` and stops before the unrecorded event. Neither becomes a
successful empty complete result. Missing legacy revision identity evidence is
counted rather than guessed from a later edited message.

## Running the matcher

One small worker scans PostgreSQL; it opens no messenger sessions, calls no model
and mounts no originals. Its default pass handles at most 16 subscriptions and
200 journal events each, then waits two seconds. Matching references and each
checkpoint commit atomically; uniqueness and row locks support restart/retry and
multiple workers. Fair ordering rotates sources beyond one batch.

Install `services/messaging/messaging_service/subscription_role.sql` after migration 32 (it grants on
`subscription_delivery` and `subscription_outbox`), then create
a separate restricted LOGIN inheriting `messaging_subscriptions`. Put its DSN in
`deploy/private/subscriptions.dsn` (0600, readable by UID 10001). Never use the
owner or gateway DSN for this worker. Apply updated gateway, indexer and enrichment
role grants too: processing workers now publish their own readiness events.

For an operator-managed Compose checkout, follow [SETUP.md](SETUP.md), build the
runtime image from the current source and add `compose.subscriptions.yaml`:

```sh
cd deploy
docker compose -f compose.yaml -f compose.subscriptions.yaml build subscriptions
docker compose -f compose.yaml -f compose.subscriptions.yaml up -d subscriptions
```

A native equivalent is `python -m messaging_service.subscription_worker` with
`DATABASE_URL_FILE` pointing to that private DSN. `--once` supports bounded
operator diagnostics. This worker needs no public port. The optional overlay
limits memory to 256 MiB and has a read-only filesystem and internal DB network.

Operator bounds are `SUBSCRIPTION_MAX_DEFINITIONS` (default 64 per key, including
closed definitions; 1–1000) and `SUBSCRIPTION_MAX_MATCHES` (default 10,000 references
per subscription; 1–1,000,000). Set the match bound on the worker and the definition
bound on the API. Raising a match budget resumes retained events without provider
polling. Old ACKed references are pruned (see [Event retention](#event-retention));
a passive-only subscription at its match bound stops with `match_budget` and needs
the operator. Outbox rows are not deleted.
Listing has no continuation cursor yet; keep the default definition
bound within one 100-item page or manage known IDs individually.

Schema-26 backups include definitions, source sets and matched references.
Quarantine restore closes definitions and advances versions, revokes credentials
and disables collectors, preserving source/match evidence without restarting
subscriptions against restored credentials.

## Product boundary

AnyMsg collects messenger data, selects relevant committed changes and delivers
recoverable event references. The consuming agent decides what those changes
mean: a news digest, a project update, a suggested task or a reply draft. Source
messages and generated descriptions remain evidence, not executable instructions.

A reply watch is a subscription preset. Sending is not a prerequisite for
subscribing to a chat, channel, set of chats or person's messages. A project name
can label a source set; this introduces no project/task management dependency.

| Use case | Selection | Delivery policy |
| --- | --- | --- |
| Follow a dialogue | One account/chat, incoming messages | Immediate hint with bounded debounce |
| Wait for a reply | Confirmed send reference plus explicit reply policy | Immediate hint, expiring watch |
| Follow news | Explicit channel/chat set | Batch hint after a time window or count threshold |
| Follow a work stream | Named set of explicit sources, optional senders | Batched or immediate |
| Follow a person | Verified account-scoped sender references in selected chats | Chosen by the subscriber |
| Follow file processing | Originals or derived artifacts linked to selected messages | Separate readiness events |

Immediate delivery means prompt dispatch eligibility, not a latency guarantee or
an unconditional model turn. Batch delivery groups references; it does not run a
summarizer in the core. A batch must flush at its maximum wait even under a
continuous message stream.

## Schema 32: explicit consumption and registered hints

Schema 32 adds independent subscription consumption. `fetch_subscription_events`
returns a bounded page after this subscription's server ACK position. A key with
explicit `subscription-ack` also receives a random five-minute page receipt;
only its SHA-256 is stored. `ack_subscription_events(subscription_id, receipt,
version)` atomically advances that position. Exact receipt retries return the
original result; competing pages, configuration changes, expiry and changed
permissions cannot advance it. At most 128 live receipts are retained for one
subscription. Passive `get_subscription_events` remains independent. Neither ACK
changes the provider's read state or archive consumer positions.

**What "read-only" means for these tools.** `fetch_subscription_events` and
`wait_for_subscription_events` carry `readOnlyHint=true`, as do
`get_subscription_events` and the other subscription views. Here the term means:
no change to consumption (`acked_through`, ACK version), subscription version or
lifecycle for an active subscription whose expiry is not reached and whose scope is
unchanged, provider state, or another key's data. A fetch still writes issuance bookkeeping, like a pagination
cursor: it creates the initial consumption row at `start_position` if missing and,
for a key with `subscription-ack` and a non-empty page, prunes expired receipts,
evicts the oldest receipts beyond 128 and stores one new receipt hash with a
five-minute expiry. The receipt is short-lived and safe to retry; a retry returns
the same events from the same position (plus any matched in between), and the
eviction means an old receipt then fails closed. Expiry
and access-change are checked on every subscription read (even `get_subscription`
and `list_subscriptions`), so a read that finds the definition expired or
its grant changed moves it to `expired` or `access_changed` and bumps `version`
(`get_subscription`, `list_subscriptions` and `get_subscription_events` keep it;
a `fetch`/`wait` that fails on the first check rolls it back, one that returns keeps it);
the next `update_subscription` then gets `subscription_conflict`. Clients should
treat the hint as retry safety, not as proof that nothing is written. The
gateway turns a backend failure of a read-only tool into `backend_unavailable`;
for a tool without the hint it would become `mutation_outcome_unknown`, which an
agent must never retry. That is why these tools stay read-only.

The same reasoning, decided 2026-10-19, keeps `readOnlyHint=true` on reads that
write bookkeeping: `calendar_agenda` and `calendar_slots` (a `sync_jobs` row and a
provider read) and `messages_get_attachment` (the original's idle clock and a 120 s
download grant). Tests pin the hint for these tools and for fetch and wait in the
backend, the gateway `catalog.json` and `packages/deedkit/lib/tools.json`.

`wait_for_subscription_events(subscription_id, timeout=25, limit=50)` polls those
pages for at most 25 seconds. Every read reauthorizes and releases its connection
before the short sleep. There are at most 32 admitted waits per API process;
client cancellation or a response deadline retains admission until an in-flight
DB read finishes. `timeout=0` performs one read with a separate five-second
response deadline. A deadline before any authorized page returns
`subscription_wait_timeout`; a normal empty authorized page returns
`wait_status=timeout`. No connection is retained while waiting for new events.

Through the gateway, the `messaging-subscriber` profile exposes the three consumption
tools (ANYM-518). The recommended loop is wait, fetch, handle, ACK; the gateway's wait
pool holds 2 active and 4 waiting requests and, when full, returns
`wait_status=busy` with `retry_after_s`. `get_subscription` returns `acked_through`.
Capabilities list `subscriptions.transports`: `pull` (fetch and ACK), `long_poll`
(wait, at most 25 s) and `webhook`, each with `authorized`, `configured`,
`operational` and the first failing `reason`. A webhook works while the dispatcher
heartbeat is under 120 s old and an enabled receiver or endpoint is bound. It says
`registration: "self_service"` when the operator's delivery switch is on and the host
policy is set, otherwise `"operator_only"`; its `self_service` block names the five
endpoint tools and `max_endpoints` ([Self-service webhook endpoints](#self-service-webhook-endpoints-schema-49)).

Creation accepts optional `wake_rules`, an OR selection of `incoming_message`,
`owner_mention`, `reply_to_owner` and `reply_to_confirmed_send`. Reasons come from
immutable event snapshots of validated native metadata on incoming
`message.created` events. Edits, receipts/status changes and processing readiness
never count as a new human reply. `reply_to_send` freezes
a sent reference owned by this exact key, including its account, chat and exact
provider receipt; unknown/failed sends and another key's send cannot become an
anchor. `reply_to_confirmed_send` requires that anchor and an exact native reply
reference in the same account/chat. Names, body text and nearby timestamps never
establish a match. Telegram owner replies freeze the exact outgoing archive
target and its current revision when the incoming event is captured. Missing or
other-chat targets stay unknown, even if imported later. A WhatsApp confirmed
watch can freeze its native stanza from the exact outgoing archive row when the
watch is created: only the known chat prefix and exact known participant suffix
are removed, preserving underscores and the original provider ID. Missing target
or ambiguous components leave the stanza projection unknown. Matching and page
reads never resolve these proofs against a later message revision, infer PN/LID
aliases or rewrite provider identifiers. Only a complete native participant JID
can be removed as a participant suffix; a bare legacy sender value cannot shorten
the stanza. An explicit native `reply_to_owner=false` excludes a projected stanza
match because it identifies a quote to another participant. Historical events have a separate `historical_event` reason
and do not satisfy live wake rules. Existing selectors without these rules keep
their behavior. There are no quiet-hours or silence timers in this version.

A webhook receiver is either an **operator registration** (this section) or an
endpoint the key registers itself inside the operator's host policy
([Self-service webhook endpoints](#self-service-webhook-endpoints-schema-49), ANYM-519).
For an operator receiver, the operator installs `delivery_role.sql` and supplies a separate restricted
LOGIN inheriting `messaging_event_delivery`. A private JSON registration contains
`receiver_id` (fresh UUID), `principal`, `key_id`, `endpoint` (HTTPS, port 443,
no credentials/query/fragment), and `secret_file` (absolute private regular file,
32–4096 secret bytes). With an owner DSN, run:

```sh
DATABASE_URL_FILE=/private/operator.dsn python -m messaging_service.delivery_worker \
  --register-receiver /private/receiver.json
```

The agent sees only the opaque receiver ID. The receiver is immutable; rotation
registers another ID, then `configure_subscription_delivery(subscription_id,
version, receiver_id)` explicitly binds it and advances the subscription version.
Passing `receiver_id=null` disables delivery. `test_subscription(subscription_id,
request_id)` enqueues a synthetic hint without creating an archive event. Its
request ID is idempotent, with at most one new test per subscription per minute.
A receiver HTTP success is not a verified host turn; all hints and probes report
`host_turn_verified=false`. Host adapters must separately prove that integration.

Run `python -m messaging_service.delivery_worker` using only the dispatcher DSN
and private receiver secret mounts; in production `deploy/compose.delivery.yaml`
runs it (docs/operations/production.md, migration 050). `--once` is one serial
bounded operator pass. Otherwise it runs separate lanes (ANYM-534): one for operator
receivers, with its own DNS lookup slots, and three for self-service endpoints. An
endpoint lane leases at most one attempt per key at a time, counting the key's
endpoint tests, and serves the least recently served key first. A key holds at
most 32 queued endpoint hints across its subscriptions and hint kinds: past it a
due hint stays held (`held_reason` `budget`) and fires on a later tick, and
`test_subscription` answers `rate_limited` (events stay fetchable). So endpoints that hang for the full timeout never delay operator
receivers, and a failing endpoint never stops a lane. The dispatcher heartbeat is
written every 30 s and only while every lane finished a pass within 60 s. The
dispatcher reaches PostgreSQL only over an internal network that holds nothing
else, and the internet over its own bridge, which host rules restrict to DNS on
the one configured resolver and public port 443; host-bound packets are dropped on
both of its bridges (`scripts/delivery_egress_check.py`). Keep the matcher on its
existing internal-only network. No dispatcher is started by installing the
package. Operator `--disable-receiver UUID` uses the same authorization fence as
in-flight delivery and cancels queued hints. No internal/private IP exceptions
are enabled.

The matcher commits opaque outbox hints with exact match references and its
checkpoint. Hints contain only a stable ID, subscription ID/version, journal
boundary, kind and the unverified-host flag. A separate dispatcher uses leased
at-least-once delivery, retries transient failures with bounded backoff, and moves
exhausted hints to `dead_letter` after eight durably reserved attempts or seven
days. The reservation commits before external I/O, so a crash after POST cannot
reset the attempt budget. It checks
credential, scope, lifecycle and exact receiver binding before each attempt. A
successful hint never ACKs events. Receivers deduplicate `X-DeedKit-Hint-ID` and
verify `X-DeedKit-Signature` as `sha256=HMAC(secret, timestamp + '.' + exact body)`;
check timestamp age before accepting a request. TLS pins a validated public DNS
address for the connection; redirects, private addresses and mixed public/private
DNS responses fail closed. Response reads and TLS exchange are bounded. Hint
counts are visible through `get_subscription`.

**Revocation fence and its latency.** No hint is sent unless authorization is
rechecked under a lock that keeps a revocation from committing until the send ends,
because a sent hint cannot be recalled. Each attempt takes, in one transaction, the
shared principal advisory lock and a `FOR UPDATE` lock on the subscription row
(`event_delivery.py` `dispatch_hints`). DNS validation (at most 2 s), the secret
read and the body build run before the fence; under it the dispatcher rechecks
eligibility, the exact receiver binding and its lease, then sends to the
pre-validated pinned address. Because DNS runs first, a hint whose key was revoked,
whose lease was lost or whose age expired can still resolve the receiver's registered
hostname once before it is cancelled; no POST follows. The hold is bounded by the 5 s
TLS deadline plus database time, so:

- an operator ACL change or revocation for that principal waits up to about 5 s per
  in-flight attempt;
- an agent `update_subscription`, `configure_subscription_delivery` or ACK on that
  subscription waits likewise, and the matcher skips the locked row;
- attempts in one dispatcher pass are serial (up to 16), so one slow receiver can
  delay other subscriptions' hints by up to about 16 × 7 s per pass (DNS plus TLS;
  only the TLS part holds the fence);
- reads of that subscription row wait as well (`get_subscription`,
  `get_subscription_events`, `fetch`, `wait`, `list_subscriptions`), and while a
  revocation for the principal is queued behind an attempt, PostgreSQL queues every
  new request of that principal behind it, so all of that principal's API calls can
  stall for up to the 5 s statement timeout and then fail with `query_timeout`
  (`Store.identity_digest` takes the shared lock on each
  call; unmeasured).

Each pass reports every attempt's fence hold in milliseconds as the list
`fence_hold_ms` in the JSON line the delivery worker prints: from the moment the
principal lock is held to commit, so time spent waiting for that lock is not counted. Running more than one
dispatcher process lowers the head-of-line delay (`FOR UPDATE SKIP LOCKED`). No
latency incident exists. Decided (2026-10-17, ANYM-257): the fence stays; an
unfenced send with post-send checks is rejected because a hint would still reach a
receiver after revocation.

Schema-32 backup manifests include consumption, hashed receipts, receiver
registrations, bindings, outbox and local feedback. Quarantine disables receivers,
cancels pending/leased hints and removes receipts, in addition to closing
subscriptions and revoking credentials. Receiver secrets stay in private operator
configuration and must be backed up separately under the recovery-state policy.

`submit_feedback(request_id, category, description, operation?, refs?)` records
private feedback locally with explicit `feedback-write`. Categories are `bug`,
`feature`, `quality` and `other`; descriptions are limited to 4000 characters and
references to 32 IDs per type. `operation`, when given, is the tool name as `tools/list`
shows it, for any module: a messaging operation bare (`search`) or with the gateway prefix
(`messages_search`), or a tool of another module under its gateway name (`calendar_slots`,
`calls_get_call_status`, `platform_capabilities`). It is stored in one canonical form: messaging
operations bare (existing rows keep their meaning), other modules' tools under the full gateway name,
and `list_feedback` returns the stored value. The idempotency hash uses the stored form, so replaying a
`request_id` with the other spelling of the same name returns the same feedback ID. The common gateway
checks the name against its reviewed catalog before relaying, whichever modules are deployed or visible
to the key, so a report about an unavailable tool is still accepted; an unknown name is rejected there
with `invalid_arguments` and a `field_errors` entry for `operation`; the bare `download`, valid before
the check although it has no gateway tool, stays accepted so a recorded request can be replayed. The
`operation` schema allows up to 129 characters (`calendar_` or `platform_` plus 120). The messaging REST API, called
directly, checks the shape only: a messaging operation, the same with the `messages_` prefix, or
`calendar_`, `calls_` or `platform_` followed by 1–120 lowercase letters, digits or `_`. The name list is
not returned in the error. Message/chat/subscription references are validated
against the current credential. Each key has a 1000-record bound; request-ID
retries are idempotent and changed payloads conflict. `list_feedback(limit=50)`
returns only this exact key's records; source reference IDs are omitted to avoid
revealing a previously authorized scope after a policy change. Feedback does not
send messages, create external issues or forward reports to a third party. A submission
transaction ends within 60 seconds or is aborted and not stored (its failure is audited as
`query_timeout`). The maintainer reads every principal's feedback for triage only through the
operator command `export-feedback` (ACCESS.md); it is not an MCP tool, REST route or gateway
action. One exception to "not forwarded" (owner decision D1, ANYM-264): an owner-run triage
session on the owner's workstation reads the exported text, so that text reaches that session's
model provider. Only that session, or the autoloop's tool-less reader on the same workstation,
reads raw text; nothing derived from it goes to Tiden, GitHub or another sink
(docs/operations/feedback-triage.md). Exception (owner 10-26, ANYM-568): for feedback from a
principal in the autoloop config, a card written in the card worker's own words, with no quote,
name, phone number or message content, goes to Tiden and GitHub (docs/operations/feedback-autoloop.md).

A rejected `submit_feedback` names what to fix. Over REST and MCP the schema check
returns `invalid_parameters` with the existing `fields` list plus `field_errors`; the
backend checks return `invalid_feedback` with the same `field_errors`. Each entry is
`{field, reason, expected?, allowed?}`: `field` is a path such as `category`,
`description` or `refs/message_ids/0`; `reason` is one of `required`, `unknown_field`,
`type`, `enum`, `min_length`, `max_length`, `pattern`, `blank`, `max_items`, `size`
(and the other reasons of the shared vocabulary); `expected` is a short reviewed hint
and `allowed` lists the permitted values, for example the four categories. There is
one entry per field, in parameter order, at most 20, and no submitted value is ever
returned: an unknown key is named only when it is a short identifier. A reference that
is valid but not found or not permitted stays `not_found`. Through the common gateway,
the gateway's own schema check returns `invalid_arguments` with `field_errors`, and it relays the
backend's `invalid_feedback` `field_errors` after rechecking the shape and vocabulary (a malformed list is
dropped, never partly relayed).

### Feedback status (schema 39)

Each feedback item carries the maintainer's disposition, so an agent can see what happened to
a report instead of filing it again. `list_feedback` returns per item `status`, `status_note`
(at most 280 characters), `fixed_in`, `spec_ref` (`ANYM-<n>`, the card that removes the
reported failure), `duplicate_of` (a feedback ID of the same key) and `status_changed_at`
(database time). `list_feedback(status=...)` returns only items with that status; another
value is rejected (`invalid_parameters`, or `invalid_feedback` from the backend, with
`field_errors`). Ordering stays `created_at DESC, id DESC`; references stay omitted.

| Status | Meaning |
|---|---|
| `received` | Default for every new and existing item: not triaged yet |
| `accepted` | Planned: `spec_ref` names the card |
| `declined` | Will not be done; `status_note` says why |
| `duplicate` | Same report as `duplicate_of`, or already covered by `spec_ref` |
| `fixed` | Merged to main: `fixed_in` = `deedkit@<40-hex merge SHA>`. Not yet live |
| `verify_requested` | Deployed: `fixed_in` = the deployed `deedkit@<sha>`. Please re-check and report |
| `verified`, `reopened` | Reserved for the reporting agent's verdict (ANYM-344); the operator cannot set them |

Merged source is not live behavior, so `fixed` and `verify_requested` are separate steps. When
one report needs several cards, the item names the primary card and stays `accepted` (with a
note) until every card is merged (`fixed`) or deployed (`verify_requested`). Only the
maintainer sets a status, with the operator command `set-feedback-status` (ACCESS.md); the
agent-facing database role can insert only the submission fields and cannot update feedback.

## Schema 38: hint timing policy

`configure_subscription_delivery(subscription_id, version, receiver_id, delivery_policy)`
sets the whole delivery configuration and bumps the version (`subscription-write`,
`subscription_conflict` on a stale version, `invalid_delivery_policy` for a bad policy). Omitting
`delivery_policy` means none; a policy needs a receiver. A policy changes only **when hints are
emitted**: never which events match, ACK positions or what `fetch_subscription_events` and the
passive reads return. `capabilities.subscriptions.delivery_policy` lists the fields and bounds.

| Field | Bounds | Meaning |
|---|---|---|
| `debounce_s` | 0–600 | one hint after this many seconds without a new match |
| `max_wait_s` | 1–3600, ≥ debounce | latest hint after the first unhinted match; default max(300, `debounce_s`) |
| `batch_window_s` | 0–86400 | one hint this long after the first unhinted match |
| `batch_count` | 1–1000 | one hint at this many matches (also flushes at `max_wait_s`) |
| `quiet_hours` | IANA `timezone`, 1–4 `windows` | `{days: [mon..sun], start: "HH:MM", end: "HH:MM"}`; `days` names the day a window starts, `end` ≤ `start` crosses midnight, adjacent windows chain |
| `quiet_bypass` | ⊆ `incoming_message`, `owner_mention`, `reply_to_owner`, `reply_to_confirmed_send`, `silence` | reasons that skip quiet hours (default none) |
| `expect_reply_within_s` | 60 s–14 d | silence timer; only for a `reply_to_send` subscription |

Debounce and batch are alternatives (both set: `invalid_delivery_policy`). Gate order: the window
(debounce or batch) decides when a hint is due; quiet hours hold a due hint to the window end, even
past `max_wait_s` (held, never dropped); the per-key budget holds it to the next slot. A bypass
reason skips only the quiet-hours step; the held range keeps the wake reasons of its matches and
the current policy's `quiet_bypass` decides at each tick. A hint carries the batch `through`. The
dispatcher rechecks quiet hours before each POST of an `events` or `silence` hint without a bypass,
so a hint moved by the budget or a retry into a quiet window waits for its end without using an
attempt.

**Silence timer.** The deadline is the configuration commit time plus `expect_reply_within_s`. If
no `reply_to_confirmed_send` match commits by then, the worker records one silence reference and
emits one hint with `kind: "silence"`. The reference is the timer record in `get_subscription`
(`delivery.silence`: anchor `send_id`, `deadline`, `state` `pending|replied|fired|hinted|cancelled`,
`fired_at`), not an archive event, so fetch results do not change. A reply before the deadline
cancels the timer; a later reply is a normal match. Any later version change cancels it: configure
again to re-arm.

**Per-key budget.** `SUBSCRIPTION_MAX_HINTS_PER_HOUR` (default 20, bounds 1–1000, set on the
matcher and dispatcher) counts every hint of a key across its subscriptions (`events`, `silence`,
`test`), with or without a policy: this deliberately changes schema-32 hint timing for
subscriptions that already have a receiver. The matcher holds a new hint over budget
(`held_reason: "budget"`) so held matches coalesce into one hint at the next slot. The dispatcher
also admits at most that many distinct hint IDs per rolling hour before their first POST, so a
backlog after downtime cannot burst; retries of an admitted hint are not new admissions. Deferral
never dead-letters and loses no event reference: the seven-day retry age starts at admission. A `test_subscription` probe over budget is refused
with `rate_limited`.

`get_subscription` shows `delivery.policy`, `held_reason` (`debounce|batch|quiet_hours|budget`)
and `next_hint_at`. Rules, all enforced by the matcher worker on durable rows:

- **Clock:** every deadline uses the database clock (`clock_timestamp()`), stored as UTC.
- **DST:** local windows use PostgreSQL time zone rules; a non-existent local time snaps forward,
  an ambiguous one uses its first occurrence.
- **Restart:** held ranges and timers are rows; after a worker restart an overdue hint fires once,
  at the first moment quiet hours and the budget allow.
- **Revocation:** an access change, pause, close, receiver change or removal, or a revoked key
  cancels held hints and timers under the existing ACL fence; nothing is emitted after the
  revocation commits. A same-receiver reconfiguration carries the held range to the new version and
  re-plans it under the new policy.
- **Idempotency:** one hint per (subscription, version, kind, window ID), enforced by a unique
  index, so retries and parallel workers cannot double-emit.
- **Failure isolation:** the hint step runs in a savepoint after the matches. A failure rolls back
  only that step (telemetry code `subscription_hint_failed`, no content); matches and the frontier
  commit, and the next tick replays the committed matches above the held range (reasons, count,
match times, an on-time reply) before it settles the range or a silence timer.

Automatic wake scheduling (ANYM-228) depends on these rules and the per-key budget: they are the
server-side gate before a relay fires an agent run.

## Self-service webhook endpoints (schema 49)

A key with `read` and the explicit `delivery-write` permission (ANYM-519,
[ACCESS.md](ACCESS.md)) registers where its own hints go. The operator only sets
the switch, the host policy and the limits.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `register_delivery_endpoint` | `request_id`, `url`, `signing_secret`; optional `label`, `headers` | Check the URL, store the endpoint, send one signed test and wait up to 10 s for it |
| `list_delivery_endpoints` | none | This key's endpoints that are not deleted, newest first, with `max_endpoints` |
| `update_delivery_endpoint` | `endpoint_id`, current `version`, `action`; `new_secret` for `rotate_secret`, `headers` for `set_headers` | Rotate the secret, replace headers (`{}` clears), `pause` or `resume` |
| `delete_delivery_endpoint` | `endpoint_id`, current `version` | Delete; bound subscriptions become pull-only |
| `test_delivery_endpoint` | `endpoint_id`, `request_id` | One signed test delivery, at most one per minute per key |

`create_subscription` accepts `delivery: {endpoint_id, delivery_policy?}` and
`configure_subscription_delivery` accepts `endpoint_id` beside `receiver_id` (not
both), with the same version check and fence as a receiver.

**Switch.** Every endpoint tool, the `delivery` field and endpoint staging answer
`delivery_disabled` until the operator enables the feature with
`python -m messaging_service set-delivery-features --enable` (ANYM-534). That refuses
until the API, the matcher and the dispatcher report this version and schema
through fresh heartbeats (`component_heartbeats`, under 120 s); `--disable` is the
rollback. With the switch
off no endpoint hint is staged, and turning it off cancels queued endpoint hints
and tests. The webhook transport in capabilities then shows `operational: false`
with reason `delivery_disabled`.

**Host policy.** `DELIVERY_POLICY_FILE` names a private JSON file (absolute path,
regular file, no symlink, mode 0600 or tighter, at most 64 KiB):

```json
{"allow_hosts": ["hooks.example.com"], "deny_hosts": ["internal.example.com"], "allow_any_public": false}
```

Hosts are IDNA-canonicalized (UTS 46, lowercase, one trailing dot removed). A
suffix matches on a DNS label boundary (`example.com` matches `a.example.com`, not
`badexample.com`) and deny wins. A missing or invalid file, unknown keys or an
empty allowlist without `allow_any_public: true` leave the policy unset:
registration answers `delivery_policy_unset` and the webhook transport shows
reason `delivery_policy_unset`. Set `allow_any_public: true` only after the
dispatcher's egress check passes (production.md, migration 050). The policy is read again before every attempt; a host that
leaves it cancels the hint (`endpoint_policy_refused`). After editing the file, run
`python -m messaging_service reload-delivery-policy`: it waits for every in-flight
POST and registration that read the old file, then cancels queued hints and tests of
hosts the new file refuses. The change counts as made when that command returns.
The stored URL and the TLS name and Host header use the same A-label host as the
policy check (`https://faß.example/` is kept as `https://xn--fa-hia.example/`).

**Address check.** The URL is https on port 443 without credentials, query or
fragment. Every resolved address must be public: multicast, NAT64 (`64:ff9b::/96`,
`64:ff9b:1::/48`), 6to4 (`2002::/16`), Teredo (`2001::/32`), IPv4-mapped and
CGNAT answers are refused. Resolution runs at registration and before each
attempt, the connection is pinned to the checked address, TLS verifies the
hostname, redirects fail and proxy variables are ignored. A refusal is
`unsafe_endpoint` with the field error "https on port 443, a public address, a
host the operator allows"; a tailnet or private host also gets the next step to
ask the operator for the wake relay (ANYM-228).

**Secret and headers.** The caller supplies `signing_secret` as
`whsec_<base64>` (24–64 decoded bytes); DeedKit never generates or returns one.
At most 4 `headers` are sent with every delivery: names are RFC 9110 tokens,
values 1–1024 visible ASCII characters. `Host`, `Content-Type`,
`Content-Length`, `Transfer-Encoding`, `Connection`, `Upgrade`, `TE`, `Trailer`,
`Cookie` and any `webhook-*`, `x-deedkit-*` or `mcp-*` name are
`reserved_header`. Secrets and header values are write-only: no tool, REST
route, log, audit row, telemetry or error returns them. Views show
`header_names`, `headers_fingerprint` and `secret_fingerprint` (the first 4 bytes
of SHA-256, 8 hex). The API role may insert and update those columns but not
select them; only the dispatcher role reads them. This is privilege separation,
not encryption: database owners and backup readers can read the values, and
backups carry them inside the existing encrypted bundle. Quarantine restore
disables every self-service endpoint.

**Retries.** The same `request_id` with the same request (compared by a stored
hash, with the secret and header values hashed) returns the stored endpoint, so a
lost response is recovered by repeating the call; a changed request is
`idempotency_conflict`.

**Signing (Standard Webhooks).** Each delivery carries `webhook-id` (the hint
ID, stable across retries; the test ID for a test), `webhook-timestamp` (the
attempt time in seconds) and `webhook-signature: v1,<base64 HMAC-SHA256>` over
`id.timestamp.body` with the decoded secret. `rotate_secret` stores `new_secret`
as pending; for 24 hours deliveries carry both signatures separated by a space,
then the dispatcher promotes the new secret. A second rotation inside that window
is `rotation_in_progress` with `retry_after_s`. Operator receivers keep the
`X-DeedKit-*` format.

**Payload.** No message text, names or file content, and no `host_turn_verified`.
An `events` hint is `{type, hint_id, subscription_id, version, label, references,
references_omitted, next_call}` within 4 KiB: at most 20 references, each
`{event_id, kind, occurred_at, wake_reason, account_id, chat_id, message_id,
sender_id, reply_to_send_id?}`, the rest counted in `references_omitted`;
`next_call` is `messages_fetch_subscription_events`. References are rechecked at
dispatch under the revocation fence. A `silence` hint carries the anchor `send_id`
and `next_call` `messages_get_subscription`. A test is `{type: "test", test_id,
endpoint_id}`. The receiver host learns these IDs, the delivery timing and the
agent's own headers.

**Onboarding test.** Registration stages a signed test; the dispatcher sends it
and records `http_status` and `latency_ms`. Past 10 s the result says
`test_status: pending` with `next_step` naming `test_delivery_endpoint` and the
same `request_id`. A failed test still creates the endpoint (state `enabled`,
health `failing`), so a retry needs no new request ID.

**States.** `enabled`, `agent_paused`, `auto_paused` (set by S3) and
`operator_disabled`. The agent may pause, and resume from `agent_paused` or
`auto_paused`; resume and test on `operator_disabled` answer `operator_disabled`.
Only the operator disables or enables (`deedkit admin delivery-endpoints list |
disable ID | enable ID`). Disable, delete, a policy change, key revocation and
loss of `delivery-write` cancel queued and in-flight hints and tests under the
same fence as receivers: no POST starts after the change commits.

**Limits.** At most `DELIVERY_MAX_ENDPOINTS_PER_KEY` endpoints per key (default 4,
1–32) that are not deleted (`endpoint_limit`); registration and test each at most
once per minute per key (`rate_limited` with `retry_after_s`). Endpoint hints
count toward `SUBSCRIPTION_MAX_HINTS_PER_HOUR`.

**Decided edge cases.** Deleting a bound endpoint leaves the subscription
pull-only; `get_subscription` shows `delivery.target: "none"` with reason
`endpoint_deleted`. The same URL under another request ID is a separate endpoint
and counts toward the cap. Two keys may share a receiver URL, each signing with
its own secret. Binding a paused or disabled endpoint answers its state
(`endpoint_paused`, `endpoint_auto_paused`, `operator_disabled`) and a next step.
Endpoints and subscriptions do not carry over to a reissued key (ANYM-495): the
new key registers its own.

### Vendor hosts (Grok Bot routines, ANYM-582)

An agent whose assistant has its own webhook trigger registers that trigger
itself, with no host relay. A Grok Bot routine's Webhook section gives a "POST to"
URL and a key; the vendor documents `Authorization: Bearer <key>`, a `200` when a
run started and every other status as "no run started", and no deduplication
(docs.x.ai/grok-bot/skills-routines-and-automations, cursor.com/help/grok-bot/routines,
2026-10-28). The operator names the vendor's host in the policy file once:

```json
{"allow_hosts": ["<vendor host>"], "vendors": {"grok-bot-routine": ["<vendor host>"]}}
```

`vendors` takes only known names (`grok-bot-routine`), at most 1000 suffixes each,
canonicalized and matched like `allow_hosts`. It never widens the policy: the host
must also pass `allow_hosts` and `deny_hosts`, and a malformed value or an unknown
name leaves the policy unset. Take the host from the routine's own URL; the vendor
publishes none. `reload-delivery-policy` prints the `vendors` map it loaded, so a
host left out of `vendors` shows at the edit.

The agent then calls `register_delivery_endpoint` with the URL,
`headers: {"Authorization": "Bearer <key>"}` (the routine's key) and a
`signing_secret`: any valid `whsec_` of its own; it is still required, and the
vendor never checks the `webhook-*` headers. Without that header, registration and
`set_headers` on a vendor host answer `invalid_header` with `header:
"Authorization"`: send the routine key as `Bearer <key>`. Endpoint views show
`vendor` (`grok-bot-routine` or null) under the current policy. If the register
answer shows `vendor: null` for the routine's URL, the host is not in `vendors`:
stop and ask the operator, since that endpoint would deliver at least once. Every
test (the registration's and `test_delivery_endpoint`) starts one run with body
`type: "test"`; the routine should end quietly on it. Bind with a
`delivery_policy` of `{"debounce_s": 60}` so a burst of events wakes one run, not
one per hint (a server-side fold of pending hints is not built).

A vendor host's endpoints deliver at most once after the request is written:

| Outcome | Hint | Endpoint test |
|---|---|---|
| 2xx | `delivered` | `delivered` |
| 3xx | dead letter `redirect_rejected` | `failed` `redirect_rejected` |
| 401, 403 | dead letter `vendor_refused`; the endpoint view turns `health: failing` with `last_hint` | `failed` `vendor_refused` |
| other status (no run started) | bounded retry, `receiver_status` | `failed` `receiver_status` |
| not sent (`egress.EgressNotSent`: refusal, connect, TLS, deadline before the first byte) | bounded retry, `receiver_unavailable` | `failed` `receiver_unavailable` |
| timeout, reset, a 1xx or a malformed response after the write | dead letter `outcome_unknown`, never re-sent | `failed` `outcome_unknown` |
| lease ran out during an attempt (dispatcher crash) | dead letter `outcome_unknown`, no POST, settled before DNS, quiet hours or the budget | `failed` `outcome_unknown`, no POST |

Before the dispatcher's transport raises `EgressNotSent`, no failure counts as
unsent, so a vendor hint is still never sent twice. A rotated or revoked routine
key shows as `health: failing` with `last_hint.error_code: vendor_refused` (the
endpoint stays `enabled`; later hints are refused too and start no run): replace
the headers with `set_headers`, then `test_delivery_endpoint`; a delivered test
makes it `ok` again.
A 429 is an "other status": the bounded retry spends its attempts in minutes. A lost
wake loses no event: the
routine also keeps a schedule trigger that fetches and ACKs. Other endpoints and
operator receivers keep at-least-once delivery.

## Host receivers (wake relay)

`packages/wake-relay/` (Node 24, `node:crypto` and `node:sqlite` only) is a host
receiver: it turns one signed hint into at most one run of the owner's assistant.
It runs on the owner's agent host behind a public HTTPS name on 443 (Caddy or a
tunnel), never on the DeedKit production host: it holds vendor credentials that
DeedKit does not hold. Registration stays an operator action (`--register-receiver`
with the relay's `https://<name>/hints/<receiver-id>` URL); there is no agent
onboarding-link API. Configuration is in [the package README](packages/wake-relay/README.md).

**Wire format.** The relay verifies `X-DeedKit-Signature` with a constant-time
compare over the exact raw body, rejects timestamps older than 300 s or more than
60 s ahead and bodies over 64 KiB. The signature covers timestamp and body only, so
`X-DeedKit-Hint-ID` must equal the signed body `hint_id` (otherwise `401`), and the
relay deduplicates on the signed `hint_id`, never on the header. Only the six
content-free body fields are kept and forwarded; any other field is dropped.
`tests/test_event_delivery.py` keeps the relay's signed-hint vector in the live
dispatcher's format.

**State machine.** One SQLite row per signed `hint_id`; each transition commits
before the action that follows it.

| State | Meaning |
|---|---|
| `pending` | Committed before the dispatcher gets its `2xx`. Nothing was written downstream. |
| `sending` | Committed after DNS, TCP and TLS succeed, just before the first request byte. |
| `accepted` | The vendor returned 2xx: Claude with its documented session ID (stored), Grok `202` (no run ID exists). A Claude 2xx without a session ID is `unknown`. |
| `unknown` | Any outcome after `sending` that the vendor does not document as a refusal: timeout, reset, crash, 500, 503, unlisted status, a response over 64 KiB. Never re-fired. |
| `failed` | A documented refusal (Anthropic 400/404, Grok 400/413), or a local error before `sending` such as a malformed credential. |
| `revoked` | Credential rejected (Anthropic 401/403, Grok 401), or the adapter disabled or its credential file removed. |
| `expired` | `max_hold_s` (default 120 s, at most 600 s after the signed timestamp) ran out before a send. |
| `folded` | Merged into the subscription's open fire; kept only for deduplication. |

Pre-send failures retry with backoff inside the hold. A `429` returns the row to
`pending` after `Retry-After`, or `expired` if that passes the hold. On restart,
`pending` rows fire and `sending` rows become `unknown`. A redelivered known
`hint_id` gets `2xx` with no new fire. "Exactly one run" is claimed only for `accepted`.

**Coalescing and budgets.** At most one `pending` or `sending` fire per receiver and
subscription; later hints fold into it and the highest `through` wins. A new fire
needs a slot in a sliding hour, per vendor target (default 20; one adapter per
routine or webhook, the same destination however its URL is spelled) and per vendor
account (default 60). The slot is reserved at acknowledgement and re-dated to each
send, so every sliding hour holds at most the budget of fires. A request the vendor
refuses with `429` starts no run: its retry is the same fire and reuses the row's
slot, so HTTP requests can exceed the budget only by such refusals. Without a slot the relay answers
`503` with `Retry-After`, stores no row and counts `budget_refused` per
subscription; the hint stays in the DeedKit outbox under the server fence and
dead-letters after the dispatcher's eight attempts (about four minutes) if no slot
opens. A budget-refused hint is not guaranteed a wake.

**Lost wakes and revocation.** No event is lost: a woken run fetches from the ACK
position and ACKs what it handled. A wake can be lost (`budget_refused`, `expired`,
`unknown`, dead letter), so the routine or automation also carries a schedule
trigger at the adapter's shortest cadence (Claude routine: hourly; Grok: per its
documented schedules) that fetches and ACKs outstanding events. The server fence
stops new hints but cannot reach work the relay already acknowledged: that work
fires within `max_hold_s` at most. `--disable-receiver` stops hints only; the key
can still read. Revoking the key also makes the woken run's fetch and ACK fail.
Removing an adapter's credential file stops its pending fires, also one whose
connection is being set up; `"enabled": false` takes effect at the next restart,
which revokes its pending rows.

**Vendor limits** (vendor documentation, 2026-10-19). Claude routine `/fire`: 30
fires per hour per routine, 100 API fires per hour per account, `Retry-After` on
`429`, no idempotency key, `text` up to 65,536 characters wrapped as untrusted
`<routine-fire-payload>`. Grok Automations webhook (the relay's `grok-automation`
adapter, as researched 2026-10-19): Standard Webhooks signature, timestamps within
five minutes, body up to 1 MiB, no published rate limit or `webhook-id`
deduplication. Grok Bot routine webhooks as documented on 2026-10-28 take a Bearer
key instead and need no relay ([Vendor hosts](#vendor-hosts-grok-bot-routines-anym-582)).

`host_turn_verified` stays `false` on every hint: the dispatcher cannot see a host
turn. A relay `accepted` row plus the run's own `fetch_subscription_events` and
`ack_subscription_events` (ACK position advanced in `get_subscription`) is the
host-turn evidence the live acceptance of ANYM-228 records.

## Event retention

Status: acknowledged-event retention is implemented since **schema 40** (ANYM-446, the
first half of the ANYM-257 contract decided 2026-10-17 by the orchestrator under the
owner's delegation). Terminal outbox rows and closed definitions are still never
deleted, and closed definitions still count toward `SUBSCRIPTION_MAX_DEFINITIONS`
(sibling story, not built). Retention may lose events only with an explicit gap.

- **Explicit-ACK subscriptions** that are not closed or expired: match references at or below `acked_through` whose
  `matched_at` is older than `SUBSCRIPTION_ACKED_RETENTION_DAYS` (default 7, 1–366; set
  it on the subscription worker, where a value outside the bounds fails startup) are
  deleted and stop counting toward `SUBSCRIPTION_MAX_MATCHES`, so a `match_budget`
  stop resumes on the next pass. A reference above `acked_through` is never deleted,
  at any age. `matched_event_count` counts retained references only.
- **Passive-only subscriptions** (never fetched with `subscription-ack`): nothing is
  deleted. The `match_budget` stop stays operator-visible, because deleting would
  silently break the `after` positions these subscribers keep themselves.
- **Retained floor:** each subscription keeps the highest pruned position (0 when
  nothing was pruned). `get_subscription_events` with an `after` below it returns no
  items, `has_more: true`, `next_after` = floor and, in `subscription.gap`,
  `{reason: 'retention_gap', after, through: floor, recoverable: false, floor}`;
  resume with `after` = floor. An `after` at or above the floor reads as before.
  `fetch_subscription_events` and `wait_for_subscription_events` read from
  `acked_through`, which is never below the floor.
- **Mechanics:** before each matching pass the subscription worker prunes at most 16
  subscriptions, one transaction of at most 500 references each, oldest first. A batch
  takes the shared principal lock and the subscription row with `SKIP LOCKED` (the
  matcher's order; ACK and fetch hold the same row), runs with `lock_timeout` 100 ms,
  `statement_timeout` 1 s and `transaction_timeout` 4 s (client pauses included), deletes a contiguous prefix above the floor and moves
  the floor in the same transaction. A failed batch rolls back alone and is logged as
  `subscription_retention_failed` with its error class only; matching and hints
  continue (also in opt-in telemetry, code only). `--once` diagnostics never prune.
- **Closed and expired definitions** keep their references: they match nothing new, so
  their budget does not matter, and every quarantine closes all definitions. Deleting
  them is the sibling story's. A definition whose `expires_at` has passed, or is less than
  a minute away, counts as expired before the matcher stores the state: selection and
  the batch (under the row lock) both skip it.
- **Quarantine hold:** quarantine restore sets `retention_hold` on every definition it
  closes. Held subscriptions are skipped by every pruning path; the hold is a table
  column, so backups carry it. Restored matches and consumption stay exactly as
  restored until the operator releases the hold:
  `python -m messaging_service release-retention-hold --subscription-ids <uuid>...`
  (or `--all-subscriptions`). A schema-38 or 39 restore is quarantined without a hold
  column and migrates to `retention_hold=false`; its definitions are closed, so this
  story never prunes them. The sibling story must not delete such closed definitions
  without the operator's release either: a closed definition left by a quarantine is
  indistinguishable from an ordinary one after migration 40, so before it ships the
  operator reruns `quarantine-restore` on any legacy restore kept beyond the migration.
- **Activation:** nothing is pruned until the operator enables retention, after every
  serving messaging API runs a floor-aware reader (one that returns `retention_gap`):
  `python -m messaging_service set-subscription-retention --enable` (or `--disable`;
  stored as `probe_metadata` `subscription_retention`, refused below schema 40). Each batch rereads the activation under a
  shared advisory lock that the switch takes exclusively, so `--disable` waits for
  in-flight batches and, once it returns, no batch deletes or moves a floor; floors
  already moved stay. An
  older reader would silently skip the pruned prefix for a passive `after` below the
  floor.
- **Roles:** the worker role gains `SELECT` on `probe_metadata` and `subscription_consumption`,
  `UPDATE(retention_floor)` on `subscriptions` and `DELETE` on `subscription_matches`.
  Migration 40 grants them to an existing `messaging_subscriptions`; reapply
  `subscription_role.sql` for a new install.
- **Rollout:** a verified backup, then migration 40 (docs/operations/production.md
  backup gate), then the release with the floor-aware API and worker, then
  `set-subscription-retention --enable` once that release is healthy. A worker on an
  older schema or without the activation prunes nothing.
- **Rollback:** once pruning has run, the messaging API must stay at a floor-aware
  version. Before any rollback of messaging below this release, run
  `set-subscription-retention --disable`; the pre-retention reader still does not report
  gaps for subscriptions already pruned, so roll back only with that known loss of the
  gap signal or restore the pre-migration backup instead.

## Deleted messages (schema 45)

The purge (ANYM-511) keeps event IDs, kinds and positions: a consumer behind it receives
the `message.created`/`message.edited` events and the `message.deleted` event of a purged
message as content-free snapshots marked `deleted: true, purged: true`, never the old body.
Every other event of the message (`message.status`, `attachment.ready`, file processing
kinds) gets the same snapshot and keeps its content-free state: `delivery_receipt` and
`metadata.processing` (attachment ID, stage, state, artifact version); its body, file names
and other metadata go. ACK positions and versions are unchanged, and hints stay
content-free references.

## Remaining extensions and verification boundaries

Coalescing across subscriptions of different keys, stream resource notifications,
retention of outbox rows and closed definitions, and text/model relevance predicates
are not implemented. Without a
policy a hint is eligible after each bounded matcher pass, within the per-key budget;
retry/delivery progress remains separate from event ACK.
An agent registers a URL only as a self-service endpoint inside the operator's host
policy. There is no provider polling on demand, remote
task execution or automatic MCP host scheduling. A host adapter must verify its
own receipt handling, deduplication and actual turn creation with explicit live
session authority before anyone claims end-to-end delivery.
