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
| `create_subscription` | `chat_ids` or `sources`; optional `sender_ids`, `kinds`, `direction`, `origins`, `label`, `expires_at` | Freeze an authorized source set, or follow every chat the key can see ([dynamic sources](#dynamic-sources-all-visible-chats-schema-58)), from the committed journal head |
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
journal range. Re-read the version after a `subscription_conflict`. For an explicit
`chat_ids` definition, any principal scope or credential-permission version change
invalidates the original grant, even if rights are later restored. A dynamic
definition is not invalidated by scope changes (see below). Inaccessible definitions expose only a masked
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
`owner_mention`, `reply_to_owner`, `reply_to_confirmed_send` and (dynamic sources only)
`reply_to_own_send`. Reasons come from
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
reset the attempt budget. When a self-service endpoint answers 429 or 503 with a
valid `Retry-After` (seconds or an HTTP date), its next attempt waits at least that
long, capped at one hour; operator receivers keep the own backoff. It checks
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
| `quiet_bypass` | ⊆ `incoming_message`, `owner_mention`, `reply_to_owner`, `reply_to_confirmed_send`, `silence` | reasons that skip quiet hours (default none); a dynamic-sources subscription refuses it (`invalid_delivery_policy`) |
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
the switch, the host policy and the limits; under self-registration (below) the
agent picks any public host, with no operator step per agent or per vendor.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `register_delivery_endpoint` | `request_id`, `url`, `signing_secret`; optional `label`, `headers` | Check the URL, store the endpoint, send one signed test and wait up to 10 s for it |
| `list_delivery_endpoints` | none | This key's endpoints that are not deleted, newest first, with `max_endpoints` |
| `update_delivery_endpoint` | `endpoint_id`, current `version`, `action`; `new_secret` for `rotate_secret`, `headers` for `set_headers` | Rotate the secret, replace headers (`{}` clears), `pause` or `resume` (a signed test first, below) |
| `delete_delivery_endpoint` | `endpoint_id`, current `version` | Delete; bound subscriptions become pull-only |
| `test_delivery_endpoint` | `endpoint_id`, `request_id` | One signed test delivery, at most one per minute per key |
| `list_delivery_attempts` | `endpoint_id`; optional `limit` (1–50, default 50), `before` | This key's latest attempts at the endpoint, newest first (schema 61, below) |

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
empty allowlist without `allow_any_public: true` or `self_registration: true` leave the policy unset:
registration answers `delivery_policy_unset` and the webhook transport shows
reason `delivery_policy_unset`. Set `allow_any_public: true` only after the
dispatcher's egress check passes (production.md, migration 050). The policy is read again before every attempt; a host that
leaves it cancels the hint (`endpoint_policy_refused`). After editing the file, run
`python -m messaging_service reload-delivery-policy`: it waits for every in-flight
POST and registration that read the old file, then cancels queued hints and tests of
hosts the new file refuses. The change counts as made when that command returns.
In Compose the dispatcher reaches receivers only through the `delivery-egress` proxy
(ANYM-581), which reads the same file per request: operator receiver hosts must be in
`allow_hosts` too (or covered by `allow_any_public`), else their hints retry and dead-letter
as unreachable. A host outside the file or with a non-public DNS answer gets no tunnel.
The stored URL and the TLS name and Host header use the same A-label host as the
policy check (`https://faß.example/` is kept as `https://xn--fa-hia.example/`).

**Self-registration (ANYM-585).** With `"self_registration": true` in the policy
file, an agent registers an endpoint on any public host that `deny_hosts` does
not match, with no `allow_hosts` entry; every other check here (address, port,
pinning, the proxy, write-only secrets, limits) is unchanged. It is the
feature's switch, off when absent. `allow_any_public` stays the operator's own
mode for hosts it vouches for (agents' and operator receivers' alike); set one,
not both: with `allow_any_public` on, every host counts as operator-allowed, so
the at-most-once rule and the kill switch below do not apply to it.
An endpoint admitted only by the switch is at most once whatever its URL: an
attempt that may have reached the receiver (a timeout after the request was
written, a lease that ran out mid-attempt, a 1xx answer) is final with
`outcome_unknown`, and so is every HTTP answer other than 2xx (`receiver_status`,
`redirect_rejected`), since an unknown receiver may act before it answers 500.
Only a request that never left (`receiver_unavailable`) is retried. A recognised
vendor endpoint (Vendor hosts) keeps its documented table, which also retries
the vendor's "no run started" statuses. A vendor routine whose URL nothing
recognises therefore never runs twice for one hint.
Every endpoint view (register, list, update) shows `at_most_once`, true for a
vendor endpoint or one admitted only by the switch.
`deedkit admin delivery-endpoints list` shows each endpoint's `admission`:
`allow_hosts` (listed or `allow_any_public`), `self_registration` (admitted only
by the switch) or null (refused now), and the top-level `self_registration`;
`reload-delivery-policy` prints it too. The kill switch is `"self_registration":
false` (or removing the key) and `reload-delivery-policy`: it revokes only the
switch's own admissions, so queued hints and tests of every endpoint whose
`admission` was `self_registration` are cancelled (`endpoint_policy_refused`),
no POST to such a host starts after the command returns, and endpoints on
`allow_hosts` hosts keep delivering. The endpoints stay listed and deliver again
when the switch is back on; wakes cancelled in between are not replayed, and the
agent fetches those events by pull. To stop one endpoint, use
`disable-delivery-endpoint` (States below).

**Address check.** The URL is https on port 443 without credentials, query or
fragment. Every resolved address must be public: multicast, NAT64 (`64:ff9b::/96`,
`64:ff9b:1::/48`), 6to4 (`2002::/16`), Teredo (`2001::/32`), IPv4-mapped and
CGNAT answers are refused. Resolution runs at registration and before each
attempt, the connection is pinned to the checked address, TLS verifies the
hostname, redirects fail and proxy variables are ignored. A refusal is
`unsafe_endpoint` with the field error "https on port 443, a public address, a
host the operator allows" (under self-registration every public host not in
`deny_hosts` is allowed); a tailnet or private host also gets the next step to
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

**States.** `enabled`, `agent_paused`, `auto_paused` (set by the dispatcher, below) and
`operator_disabled`. The agent may pause, and resume from `agent_paused` or
`auto_paused`; resume and test on `operator_disabled` answer `operator_disabled`.
Only the operator disables or enables (`deedkit admin delivery-endpoints list |
disable ID | enable ID`). Disable, delete, a policy change, key revocation and
loss of `delivery-write` cancel queued and in-flight hints and tests under the
same fence as receivers: no POST starts after the change commits.

**Limits.** At most `DELIVERY_MAX_ENDPOINTS_PER_KEY` endpoints per key (default 4,
1–32) that are not deleted (`endpoint_limit`); registration and test each at most
once per minute per key (`rate_limited` with `retry_after_s`). Endpoint hints
count toward `SUBSCRIPTION_MAX_HINTS_PER_HOUR`. Across all keys, at most
`DELIVERY_MAX_HINTS_PER_HOST_PER_HOUR` endpoint hints (default 100, 1–10000) are
admitted (their first attempt reserved) for one host in a rolling hour
(ANYM-585); past it a hint waits
for the next slot, spends no attempt and never dead-letters for the budget. Like
the per-key admission wait, the hint shows only as pending in `hint_counts`
(endpoint-level reasons: ANYM-583). Operator receivers are not counted; vendor
hosts share the same budget. The budget counts admissions, not POSTs: each
admitted hint makes at most 8 attempts over at most 7 days, and a self-registered
endpoint retries only a request that never left (Self-registration), so a lease
that ran out between admission and the POST settles with no POST at all. Endpoint tests are bounded
per key (one per minute), and keys are issued by the operator.

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
2026-10-28). Under self-registration a routine URL of the form
`https://api2.cursor.sh/automations/webhook/<routine-id>` is recognised as
`grok-bot-routine` by its shape, with no policy entry (ANYM-585; the vendor pages
publish no URL, the shape is Hookdeck's Grok Bot guide, 2026-10-28). The shape
only adds the Bearer check: a self-registered endpoint is at most once anyway. On
an `allow_hosts` host the operator names the vendor's documented host once (an
agent with operator access may; AGENTS.md: agents' decision, no owner step):

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
answer shows `vendor: null` for the routine's URL, neither the shape nor `vendors`
matched it. With `at_most_once: true` in the same answer (a self-registered
endpoint) keep it: make sure you sent the key as `Authorization: Bearer <key>`
yourself (the check did not run) and go on. With `at_most_once: false` that
endpoint would deliver at least once: do not bind it; delete it and report the URL's host (never the
key) to the operator, who adds it to `vendors`, then register again. Every
test (the registration's and `test_delivery_endpoint`) starts one run with body
`type: "test"`; the routine should end quietly on it. Bind with a
`delivery_policy` of `{"debounce_s": 60}` so a burst of events wakes one run, not
one per hint (a server-side fold of pending hints is not built).

A vendor host's endpoints deliver at most once after the request is written:

| Outcome | Hint | Endpoint test |
|---|---|---|
| 2xx | `delivered` | `delivered` |
| 3xx | dead letter `redirect_rejected` | `failed` `redirect_rejected` |
| 401, 403 | dead letter `vendor_refused`, no run started | `failed` `vendor_refused` |
| other status (no run started) | bounded retry, `receiver_status` | `failed` `receiver_status` |
| not sent (`egress.EgressNotSent`: refusal, connect, TLS, deadline before the first byte) | bounded retry, `receiver_unavailable` | `failed` `receiver_unavailable` |
| timeout, reset, a 1xx or a malformed response after the write | dead letter `outcome_unknown`, never re-sent | `failed` `outcome_unknown` |
| lease ran out during an attempt (dispatcher crash) | dead letter `outcome_unknown`, no POST, settled before DNS, quiet hours or the budget | `failed` `outcome_unknown`, no POST |

Before the dispatcher's transport raises `EgressNotSent`, no failure counts as
unsent, so a vendor hint is still never sent twice. After a rotated or revoked routine
key every hint dead-letters `vendor_refused` and starts no run; the endpoint stays
`enabled`. Every endpoint view (`register_delivery_endpoint`, `list_delivery_endpoints`,
`update_delivery_endpoint`) shows it as `last_error` (ANYM-583): null, or
`{code: "vendor_refused", at, http_status, next_step}`, and `health` reads `failing`
while it is set. The refusal is the endpoint's newest `vendor_refused` hint or test by
`finished_at`; it shows while it finished after the endpoint's newest delivered hint and
newest delivered test, so only a delivery clears it (a later `outcome_unknown`, redirect,
receiver or budget failure does not). `http_status` appears only when the refusal was a
test. `next_step` names the cause, the stored header names (never values) and the calls
for the endpoint's state: replace the headers with `set_headers` (it replaces every
header), then call `test_delivery_endpoint`, or for a paused endpoint `resume`, which
sends the signed test itself (ANYM-520); a delivered test confirms the new key and clears
the signal. Only the operator re-enables an `operator_disabled` endpoint. The signal
carries no header value, URL, response body or hint payload. It is per endpoint: rotate
every endpoint that shares a key. It is also `delivery_health.last_error` in
`get_subscription` and every wait and fetch response (below).
A 429 is an "other status": the bounded retry spends its attempts in minutes. A lost
wake loses no event: the
routine also keeps a schedule trigger that fetches and ACKs. Other endpoints and
operator receivers keep at-least-once delivery.

### Delivery attempts, health and auto-pause (schema 61, ANYM-520)

**Attempt log.** The dispatcher writes one `delivery_attempts` row for each attempt at an
endpoint hint, and one for a hint it settles without a POST (an expired lease, an exhausted
budget, an expired module event, an auto-pause). `list_delivery_attempts(endpoint_id,
limit<=50, before?)` returns the key's own rows newest first: `attempt_id`, `at`, `hint_id`,
`subscription_id`, `attempt` (the hint's attempt number), `outcome` (`delivered`, `retry`,
`dead_letter`, `cancelled`, `paused`), `http_status` when the receiver answered,
`error_code` and `latency_ms`. Error codes name the failure, never its text: `timeout`,
`tls`, `connect`, `dns_unsafe`, `dns_unavailable`, `egress_unavailable`,
`invalid_response`, `redirect`, or the hint's own code (`vendor_refused`,
`outcome_unknown`, `receiver_status`, `delivery_budget_exhausted`, ...). No response body,
header value or URL is stored or returned. `has_more` and `next_before` page to older
rows. **Retention:** rows older than 7 days are never listed, and the dispatcher deletes
them, at most 1000 per pass. A failed attempt-log write never fails the hint (its own
savepoint, logged as `delivery_attempt_record_failed`).

**Health in every read.** `list_delivery_endpoints` (and `register` and `update`) carry
`delivery_health` for each endpoint, and so does `delivery` in `get_subscription` and in
every `fetch_subscription_events` and `wait_for_subscription_events` response for a
subscription bound to an endpoint: `{state, consecutive_failures, last_success,
last_error, next_step}`. `state` is the endpoint's `agent_paused`, `auto_paused` or
`operator_disabled`; an enabled endpoint is `failing` while it has a failed attempt since
its last success, a vendor refusal (`last_error`, ANYM-583), or a test that failed after
its last success, else `ok`. `consecutive_failures` counts the retried and dead-lettered
attempts since `last_success`, the newest delivered hint or test. `next_step` names the
exact call the state accepts: `test_delivery_endpoint` for a failing endpoint,
`update_delivery_endpoint(..., action="resume")` for a paused one, the operator for an
`operator_disabled` one, null when `ok`. It comes from the same function as
`last_error.next_step`, so the two never disagree.

**Pause rule.** After 3 dead-lettered hints since the last success, or once the first
failed attempt since the last success is 24 hours old (hints were due for a day without a
success), the dispatcher moves an `enabled` endpoint to `auto_paused` (version + 1) and
cancels its queued hints (`endpoint_auto_paused`, each logged as `paused`) and tests.
Events stay fetchable and nothing is ACKed. The rule runs after each pass with a failed
attempt, under the endpoint's lock, so it never races an agent's pause, resume or delete.
The dispatcher role gains only `UPDATE(state, version, updated_at)` on
`delivery_endpoints` and `SELECT, INSERT, DELETE` on `delivery_attempts`
(`delivery_role.sql`); the API role reads `delivery_attempts` (`gateway_role.sql`).

**Resume path.** `update_delivery_endpoint(action="resume")` from `agent_paused` or
`auto_paused` stages one signed test (`{type: "test"}`, the per-key test rate applies)
while the endpoint stays paused, and waits up to 10 s for it. Delivered: the endpoint is
`enabled` again (version + 1), and every bound subscription of the key with events above
its ACK position gets one `events` hint at once (`woken_subscriptions`); quiet hours, the
budget and the scope recheck still apply at dispatch. Failed: `endpoint_test_failed` with
the test's `test_error` and `http_status`, and the endpoint stays paused. Still pending
after 10 s: the endpoint stays paused and `test.next_step` names the same resume call,
which reads that test back and finishes the resume once it is delivered (within 5
minutes). Resume never clears `operator_disabled`.

**Budget and operator view.** `messages_capabilities` shows the webhook transport's
`hint_budget`: `per_hour` (`SUBSCRIPTION_MAX_HINTS_PER_HOUR`) and `remaining_this_hour`,
the fewer of the hints the key may still commit and still admit in the rolling hour (null
when that optional read fails). `deedkit admin delivery-endpoints list` shows each
endpoint's `delivery_health` and `last_error`, the owning `principal`, `key_id` and the
principal's `key_label` from the private admin labels file, and `endpoints_by_state` counts
the listed endpoints by health state.

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

## Failed sends and account health (schema 55)

Two opt-in kinds (ANYM-535) wake an agent without polling send status or sync status. They are
matched only when listed in `kinds`, never by the default kinds; `wake_rules` select incoming
messages only, so a subscription with wake rules never matches them. `direction` and
`sender_ids` do not filter them.

| Kind | Journaled when | Fetched fields | Hint reference |
| --- | --- | --- | --- |
| `send.failed` | A send reaches `failed` or `unknown` (uncertain; also a send interrupted by a collector restart) | `send_id`, `account`, `chat`, `status`, `error_code` | `send_id`, `status`, `account_id`, `chat_id` |
| `source.health` | An account's health changes to or from `disconnected`, `auth_required` or `stale` | `account`, `status` | `status`, `account_id` |

`send.failed` matches only subscriptions of the key that made the send, in one of their chats;
another key of the same principal sees nothing. No text leaves: neither the body nor the
provider's error text, only the fixed `error_code`. The collector writes the row in the same
transaction as the send status, in its own savepoint: a journal error is logged
(`subscription_journal_failed`) and counted, and the status commits anyway; a failing log write
cannot undo it either. A collector login bound to one account (`collector_account_roles`) may
journal only a failed or unknown send of its own account, under that send's own principal, key and
chat.

`source.health` matches subscriptions whose key sees the whole account; a chat-restricted key
reads no collector health (as in `get_sync_status`). Its `status` is `auth_required` (the
connection is blocked until the owner relinks), `disconnected` (no live collector lease, a
stopped collector or a connection that is not connected), `stale` (connected, but no provider
event, meaning a committed live capture such as a message, receipt, read state or deletion, since
the later of the connect and the last event for longer than `SOURCE_HEALTH_STALE_MINUTES`,
default 30, 1–1440, set on the matcher) or `connected` (a recovery). An account quiet for longer
than the threshold is therefore stale; raise the threshold for quiet accounts. The matcher derives
the status from `collector_health` and the lease every 30 seconds through the definer function
`journal_source_health`, which remembers the last status, connect time and lease epoch per account
in `source_health_journal`. The status is sampled every 30 seconds: a disconnect and reconnect, or a
collector restart, between two samples is journaled as `disconnected` followed by the current
status, but other changes reverted within one interval (for example a connect that drops again
before the next sample) may collapse into no event. The first observation of an account
is a baseline and journals nothing. The function locks no collector row; its transaction is
bounded to 2 seconds. A failure is logged and counted and never stops matching.

Voice transcripts need no new kind: `file.transcript.ready` already wakes a subscriber when an
audio or voice message's transcript is ready.

Rollout: migration 55, then reapply `subscription_role.sql` (EXECUTE on
`journal_source_health(integer)`), then restart the matcher. Before the migration the matcher
skips health journaling. Undo: stop selecting the kinds; the rows stay in the journal.

## Module events (schema 56)

Calendar and calls events (ANYM-562; design: docs/plans/cross-module-subscriptions.md, option a2)
have a journal of their own, `module_events`, with its own gapless counter
(`module_event_counter`). It is never part of the message journal: a module publish never takes
the `feed_counter` row every message writer locks, and the message matcher, `get_updates`,
message fetch and ACK, message retention and older runtimes never read module rows.

Contract (`messaging_service/module_events.py`, `validate_event`). Each event uses the CloudEvents
attribute names, not the CloudEvents HTTP binding:

| Attribute | Value |
| --- | --- |
| `specversion` | `1.0` |
| `id` | `<module>:<seq>`, `calendar:` or `calls:` plus the module's own journal number; also the idempotency key (`event_key`, one identifier) |
| `source` | `deedkit:calendar` or `deedkit:calls` |
| `type` | one of the types below |
| `subject` | the primary reference (`event_ref` or `call_id`), equal to that data key |
| `time` | when it happened (RFC 3339) |
| `data` | references only: the type's listed keys, each within its type, enumeration or length bound |
| `urgency` | `now` (passes quiet hours; never batched) or `batch` |
| `expires_at` | RFC 3339 or null; an event already expired when matched is never matched |

| Type | `data` keys |
| --- | --- |
| `calendar.event.created` | `calendar_ref`, `event_ref`, `start_at`, `version` |
| `calendar.event.changed` | as created, plus `changed` (subset of `start`, `end`, `details`) |
| `calendar.event.cancelled` | `calendar_ref`, `event_ref`, `start_at` |
| `calendar.event.starting` | `calendar_ref`, `event_ref`, `start_at`, `lead_minutes` (1–1440); `expires_at` must equal `start_at` |
| `call.finished` | `call_id`, `status` (`finished`), `end_reason`, `transcript` (`final`); optional `duration_s` (0–86400), `outcome` |
| `call.failed` | `call_id`, `status`, `end_reason` |
| `call.not_answered` | `call_id`, `end_reason` (`no_answer`, `busy`, `canceled`) |
| `call.interrupted` | `call_id`, `end_reason` (`service_restart`) |
| `call.owner_request` | `call_id`, `decision_id`, `state` (`pending`); `urgency=now` and `expires_at` = the decision's stored deadline |

References are `[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`; `end_reason` and `outcome` are lowercase
tokens of at most 48 characters. No titles, places, descriptions, guest names, destinations,
transcripts or owner answers. `data` is at most 1 KiB (as PostgreSQL prints jsonb); the module
seq in `id` is at most 2^53-1.

Producers' read endpoint (`GET /v1/events?after=<seq>&limit=<n>`, ANYM-563 and ANYM-565; the router,
ANYM-575, applies `read_page`). A page is `{events, next_after, has_more, retention_floor,
journal_id}`. `after` is exclusive; events are ordered by the module's gapless `seq`; at most
`limit` events and the router's byte bound per page; `next_after` is at least the last event's seq
and always moves past rows the producer filtered (a page with `has_more` never repeats its cursor).
`retention_floor` is the highest pruned seq: a cursor below it gets an explicit `retention_gap`
from `after` to the floor (the ANYM-446 shape), never an empty complete page. A `journal_id`
other than the cursor's, or a `next_after` below the cursor, is a source reset: it is reported and
never read as new events.

The shared producer contract (ANYM-575; ANYM-563 and ANYM-565 build against this paragraph, and
`tests/test_event_router.py` runs a reference producer of it over HTTP):
- Each event in `events` is the envelope above plus one top-level `scope_ref`
  (`[A-Za-z0-9][A-Za-z0-9._:-]{0,255}`), the module-side owner of the event (calls: the call's
  `owner_principal`, ANYM-564). The router removes it before `validate_event` and publishes it as
  the event's scope; messaging never takes a scope from router configuration. No other top-level
  key and no key outside the type's `data` keys (a rehearsal flag or owner scope included): such an
  event is quarantined.
- `GET /v1/events/head` returns `{"journal_id": str, "head": int}` (the highest seq ever
  assigned). The router reads it once, for a source with no stored cursor.
- Every request carries `Authorization: Bearer <router read token>`; reads send `after` and
  `limit` (100). The router refuses a response over 256 KiB or over `limit` events, follows no
  redirect, reads plain HTTP on the internal integration network only (the config refuses another
  scheme) and gives each request one total deadline (`timeout_s`) over the name lookup, connect,
  headers and body.
- A re-fetch of one quarantined seq reads `?after=<seq-1>&limit=1`.

Publishing: only `publish_module_event(...)`, a definer function with a fixed search path and no
PUBLIC execute; it applies the whole contract again through `module_event_valid` (every key, rule,
bound, the subject, the id's module, the urgency and expiry rules and the data size), so a caller
that skips `validate_event` still cannot store free text, and inserts one row. A retry with the same `id` and fields returns the stored seq; the same `id` with other
fields fails with `module_event_conflict`. The router's role (`module_router_role.sql`,
`messaging_module_router`) can execute it and `report_module_reachability` and nothing else: no
table privilege, so it can write no message event, change no subscription source or link and read
no message content.

The module stream. A subscription gets module sources `(source, scope_ref, types)` and a module
stream (`subscription_module_streams`) through `module_events.attach_sources`; the link check
that lets a key name a scope is ANYM-564, so no public API creates module sources yet. Each
matcher pass, after the message pass and in a savepoint of its own (a failure is logged as
`subscription_module_match_failed` and never undoes or stops the message matches), matches
module events by `(source, scope_ref)` and type into `subscription_module_matches` with its own
`matched_through`, match budget (`SUBSCRIPTION_MAX_MATCHES`) and gap (`stream: "modules"`), and
stages one outbox hint of kind `module_events` whose `through_position` is a module position
(endpoint bodies list `type`, `source` and `subject` per event, ANYM-575).
A hint is staged only while a dispatcher whose heartbeat names schema 56 or later beats
(`module_hints_ready()`): an older dispatcher would read the module position as a message
position. Until then the hint waits (the stream's `hinted_through` stays) and pull reads work. A
failed hint step (`subscription_module_hint_failed`) keeps the module matches and is retried on
the next pass. The dispatcher rechecks expiry before every attempt: a hint whose events have all
expired (or were ACKed) is cancelled with `module_events_expired` and never sent.

Reads: `get_subscription` adds `module_stream` (state, positions, sources, health) only for a
subscription that has one; message-only responses are unchanged. `get_subscription_events`,
`fetch_subscription_events` and `wait_for_subscription_events` add `module: {items, has_more,
next_after, gap}`; each item is the envelope above plus `module_event_id` and `expired`. The
passive `get_subscription_events` reads the module stream from the consumer's ACK position, so a
pruned range is never served again; `Store.get_subscription_events(module_after=)` pages it
explicitly (the MCP and gateway parameter comes with the module catalog, ANYM-566). One fetch receipt
carries both streams' positions (`module_after_position`, `module_through_position`); the ACK
moves each position by compare-and-swap and returns `module_acked_through`. Retention prunes
ACKed module references after the same window and with the same switch as message references,
with its own floor; a cursor below it gets a `retention_gap` of the module stream only.

Health: `report_module_reachability(source, reachable)` keeps the time of the first failure and
clears on the next success. A subscription shows each unreachable module once in
`module_stream.health` as `{source, state: "module_unreachable", since}`; `blocked_reason` stays
the matcher's, and message sources keep matching.

Rollback fence: module-aware code sets `messaging.module_aware` for the transaction that makes a
subscription active. Any other change to `active` (an older runtime's resume of a paused
subscription) sets `rollback_hold` through the trigger `subscription_module_fence`: the module
stream stays held (`module_stream.state: "held_after_rollback"`, `next_action: "resume"`) while
the message stream runs, until the key calls `update_subscription(action="resume")` again.

Backup: the manifest covers `module_event_counter`, `module_events`, `subscription_module_sources`,
`subscription_module_streams` and `subscription_module_matches` from schema 56;
`module_source_health` is derived router state. A restore closes every subscription and cancels
pending hints, module ones included. `pg_restore --no-acl` leaves new functions executable by
PUBLIC; `migrate()` revokes PUBLIC from the five schema-56 functions again (as for the older
definer functions), and `subscription_role.sql` and `module_router_role.sql` revoke it too.

Rollout: under docs/operations/production.md "Migration releases and the backup gate". Production
runs schema 53 (0107b218), whose backup reads schemas up to 53 only: the prepared release's backup
and restore runtimes (schema 56) are installed first, then the gate takes and verifies the backup
of the current database with them, then the one `migrate` (054 through 056), then reapply
`subscription_role.sql`, `gateway_role.sql` and `delivery_role.sql` and install
`module_router_role.sql`; the router (ANYM-575) comes later. Those backup and restore runtimes stay
pinned through any rollback of the service images: a backup taken after the migration needs a
schema-56 runtime to verify or restore.
Rollback of the service images to a runtime before schema 56: stop the matcher (the only module hint
writer) first, then cancel pending module hints
(`UPDATE subscription_outbox SET state='cancelled',error_code='module_rollback',finished_at=clock_timestamp()
WHERE kind='module_events' AND state IN ('pending','leased')`), because an older dispatcher would
send them as message hints, then deploy. The module tables stay; an older matcher stages no module
hint, and the fence holds every module stream an older runtime resumes.

### Module event router (ANYM-575, schema 58)

`messaging_service/event_router.py` is a worker (overlay `deploy/compose.event-router.yaml`) that
pulls each configured module (`deploy/event-router.example.json`) and publishes into the module
journal. Per page: the HTTP read, the page checks and each event's contract check run with no
database transaction open; then one database-only transaction, bounded on the server
(`lock_timeout` 1 s, `statement_timeout` 2 s, `transaction_timeout` 5 s,
`idle_in_transaction_session_timeout` 5 s), publishes the events through `publish_module_event`
and advances the stored cursor (`module_router_cursors`: `source`, `after_seq`, `journal_id`)
in the same commit, compare-and-set against the cursor read before the network step. A crash
before the commit publishes nothing; a crash after it reads from the stored cursor; a replay
returns the stored seq for each `event_key`, so no event is journaled twice.

- First contact: the cursor is the module's head, unless the operator set one before
  (`python -m messaging_service set-module-cursor --source <s> --after <n>`); an earlier `after`
  replays idempotently.
- A malformed event (contract refused, wrong source, missing `scope_ref`, or a definer-side
  refusal or `module_event_conflict`) is quarantined in `module_router_gaps` with its seq and the
  rule's code, never its content; the page continues past it. A refused event that names no
  usable seq is held as the range up to the next valid event. A pruned range (`retention_floor`
  above the cursor) is a `retention_gap` row; a new `journal_id` or a cursor going back is a
  `source_reset` row and the router stops reading that source until the operator sets its
  cursor again. Ordering breaks (`seq_not_increasing`, `next_after_behind_events`) refuse the page.
- While a source has an open gap, `module_stream.sources[].coverage` is `incomplete` and
  `module_stream.health` lists `{source, state: "module_gap", since}` (the oldest open gap). Each
  pass re-fetches up to four quarantined ranges; one whose events all pass is published and
  resolved `refetched`. The owner abandons one explicitly with `python -m messaging_service
  abandon-module-gap --source <s> --gap-kind quarantine|retention_gap --after <after_seq>`
  (recorded as `abandoned` with its time); `module-router-status` lists cursors and open gaps.
- A module that does not answer, answers slowly (past `timeout_s`), with a non-200 status or with a
  broken page is reported through `report_module_reachability` (`module_unreachable`) and
  cleared by the next good answer. The router never writes a message row or takes the
  `feed_counter` lock, so message writes and hints never wait for it.
- Role: `messaging_module_router` executes the two definer functions and reads and writes only
  its own cursor and gap rows (`module_router_role.sql`). The gateway reads open gaps
  (`gateway_role.sql`); before that grant, `get_subscription` shows no `module_gap`.

What leaves the process. To a module: the bearer token, `after` and `limit`. Into the database:
the envelope and its `scope_ref` (references only, `validate_event`). Logs: one JSON line per
failure, `{event: "module_router", source, code}` with a fixed code (`module_timeout`,
`module_http_status`, `module_auth_refused`, `module_unreachable`, a contract rule, ...); never
a URL, token, response body or event field. Telemetry (opt-in): `module_router_failed` with
the exception type and frames only. A delivered module hint carries no event body:

- a self-service endpoint body is `{type: "module_events", hint_id, subscription_id, version,
  label, references, references_omitted, next_call}`, and each reference is `{type, source,
  subject}`: the kind, the subject reference and the module name; positions, urgency, expiry and
  `data` are read only through the fetch (`next_call`);
- an operator receiver body is `{hint_id, subscription_id, subscription_version, through, kind:
  "module_events", host_turn_verified}`, with no reference at all.

Delivery honours the contract (ANYM-562 paths): an event whose `expires_at` passed is never
matched, and a hint whose events all expired is cancelled at every attempt; a hint holding an
`urgency=now` event bypasses quiet hours and is never batched, and still takes a slot of the
key's budget; `batch` events are hinted after each matcher pass and wait for quiet hours.

## Dynamic sources: all visible chats (schema 60)

ANYM-522. Instead of `chat_ids`, `create_subscription` accepts
`sources={all_visible_chats: true, account_ids?, include_channels?, include_bots?}`: every chat
the key can see, optionally limited to some of its accounts (1–64 account IDs inside the key's
scope, else `not_found`). Exactly one of `chat_ids` and `sources` is required. The definition
stores no chat list (`get_subscription` shows `sources` instead of `chat_ids`), so a principal
scope or credential-permission version change neither invalidates it nor needs a new one. Losing
`read` or `subscription-write` (on the principal or the key), a disabled principal, a revoked or
expired key still moves it to `access_changed` (fail closed); the operator's change does so at once,
so restoring the right later does not revive it. `sender_ids` and `reply_to_send` belong to explicit chats and are
refused with dynamic sources.

Channels (Telegram broadcast channels have `chat_type` `channel`) and messages from bot senders
(`sender_is_bot` native metadata) are left out unless `include_channels` or `include_bots` is
`true`. WhatsApp status and broadcast lists are not collected at all. Kinds, direction, origins
and the existing wake rules apply unchanged; `reply_to_confirmed_send` still needs its explicit
anchor.

**Authorization at every disclosure point.** The matcher reads one scope snapshot per event batch
(one `chats` query, under the shared principal lock it holds for the batch) and never matches an
event in a chat outside the key's scope at that moment. Each reference records its account and
chat. Every read applies the key's scope at that moment: passive `get_subscription_events` pages,
`fetch_subscription_events` and `wait_for_subscription_events`, `get_subscription` and
`list_subscriptions` (`matched_event_count`), hint staging (reasons, counts and match times of a
held range) and hint dispatch. When the operator narrows a scope (`set-access`), the principal's
dynamic references to chats or accounts that left it are marked hidden in the same transaction,
which waits for every read, matcher batch and hint POST in flight; a chat added again shows only
events matched after the re-add. The same transaction rebuilds a held hint range from the visible
references (count, reasons and match times) and cancels a pending hint that covers no visible
reference (`scope_changed`, left out of `hint_counts`); any other queued hint keeps its range and
position. The dispatcher rechecks visibility under its fence before each POST. Quiet hours hold a
dynamic subscription's hints with no bypass: its delivery policy refuses `quiet_bypass`, so a
reason in a chat hidden later never skips them. A self-service endpoint body lists only visible
references. A page whose remaining
references are all hidden still moves `next_after` past them, and `fetch_subscription_events`
issues a receipt for it, so the ACK cursor passes filtered positions; a receipt issued before a
scope change is still `stale_ack`.

**Waking on replies without a per-send subscription.** The wake rule `reply_to_own_send` matches an
incoming live `message.created` that natively replies, in the same account and chat, to any
confirmed send of this key, by the anchor rules of `reply_to_confirmed_send`: its native reply
reference equals the send's exact provider message ID; or, on WhatsApp, the native stanza projected
from the exact outgoing archive row (known chat prefix, and in a group the exact participant suffix),
unless the provider marks it as no reply to the owner; or its exact reply evidence (frozen at
ingestion) names that archived outgoing message. Another
key's send, an unknown or failed send and the same ID in another chat never match. No anchor is
needed at creation. Recommended for a dynamic subscription, so it does not spend the hourly hint
budget on every message: `wake_rules=[reply_to_owner, owner_mention, reply_to_own_send]`. Interim
await-reply recipe: one `all_visible_chats` subscription with `wake_rules=[reply_to_own_send]`
catches every reply to the key's confirmed sends; only a silence timer (`expect_reply_within_s`)
still needs a per-send `reply_to_send` subscription.

**Activation.** Dynamic sources return `dynamic_sources_disabled` (409) until the operator runs
`python -m messaging_service set-dynamic-subscriptions --enable`, which refuses (and changes
nothing) unless the API, matcher and dispatcher heartbeats report this version on the current
schema: an older matcher would scan a dynamic definition, match nothing and still advance its
checkpoint. Rollout: migration 60 (expand: four columns on `subscription_matches`), reapply
`subscription_role.sql` (SELECT on chat IDs and types, on the key's sends' IDs and status, and on
the outgoing archive rows' IDs and the account platform for the stanza projection)
and `delivery_role.sql` (SELECT on the reference columns), deploy, then enable. Rollback, before
any older code is deployed: `--disable` moves every dynamic definition to `access_changed`, which
older code already masks in get, list and passive reads and refuses to resume or fetch, cancels
its queued hints (`dynamic_sources_disabled`) under the fence and prints the definition and hint
IDs. Agents recreate their definitions after a new `--enable`.

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
