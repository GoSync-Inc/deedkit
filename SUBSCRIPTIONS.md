# Durable subscriptions and agent delivery

Current delivery work and priority: [BACKLOG.md](BACKLOG.md), DK-08–DK-10.
This document is the API and delivery contract.

Status: since **schema 32** (current schema 35) DeedKit implements persistent structural subscriptions, passive
inspection, independent fetch/ACK, bounded long polling, native wake selectors,
operator-registered signed hints and private feedback. Installing the package
does not register a receiver, create a subscription, activate a provider or
verify a host turn. Batching, quiet hours, silence timers and unsolicited MCP
resource notifications remain future work.

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
polling. This slice does not delete match rows or apply automatic journal
retention. Listing has no continuation cursor yet; keep the default definition
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

`wait_for_subscription_events(subscription_id, timeout=25, limit=50)` polls those
pages for at most 25 seconds. Every read reauthorizes and releases its connection
before the short sleep. There are at most 32 admitted waits per API process;
client cancellation or a response deadline retains admission until an in-flight
DB read finishes. `timeout=0` performs one read with a separate five-second
response deadline. A deadline before any authorized page returns
`subscription_wait_timeout`; a normal empty authorized page returns
`wait_status=timeout`. No connection is retained while waiting for new events.

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

A webhook receiver is an **operator registration**, not an agent-supplied URL.
The operator installs `delivery_role.sql` and supplies a separate restricted
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
and private receiver secret mounts. `--once` is a bounded operator pass. Keep the
matcher on its existing internal-only network; public HTTPS egress belongs to a
separate dispatcher deployment. No dispatcher is started by installing the
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

Schema-32 backup manifests include consumption, hashed receipts, receiver
registrations, bindings, outbox and local feedback. Quarantine disables receivers,
cancels pending/leased hints and removes receipts, in addition to closing
subscriptions and revoking credentials. Receiver secrets stay in private operator
configuration and must be backed up separately under the recovery-state policy.

`submit_feedback(request_id, category, description, operation?, refs?)` records
private feedback locally with explicit `feedback-write`. Categories are `bug`,
`feature`, `quality` and `other`; descriptions are limited to 4000 characters and
references to 32 IDs per type. Message/chat/subscription references are validated
against the current credential. Each key has a 1000-record bound; request-ID
retries are idempotent and changed payloads conflict. `list_feedback(limit=50)`
returns only this exact key's records; source reference IDs are omitted to avoid
revealing a previously authorized scope after a policy change. Feedback does not
send messages, create external issues or forward reports to a third party.

## Remaining extensions and verification boundaries

Quiet hours, silence timers, debounce/batch windows, coalescing across overlapping
subscriptions, stream resource notifications, automatic retention and text/model
relevance predicates are not implemented. Current hints are eligible after each
bounded matcher pass; retry/delivery progress remains separate from event ACK.
There is no arbitrary agent URL registration, provider polling on demand, remote
task execution or automatic MCP host scheduling. A host adapter must verify its
own receipt handling, deduplication and actual turn creation with explicit live
session authority before anyone claims end-to-end delivery.
