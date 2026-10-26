# Cross-module subscriptions: how calendar and call events join the push model

Decision note for ANYM-521 (research; feedback efeea8f5 item 3, caa43774 items 2-3). As-of SHA 47e1ff6, schema 44.
Context: push epic ANYM-517, story S4. The child cards live in Tiden (ANYM-517 lineage), not in this file.

## 0. Answer

Recommendation: **option (a), staged: every module keeps a small append-only event journal, and one router in the
messaging side *pulls* from the modules over HTTP and publishes content-free references into messaging's journal.**
Build calls first (cheap, high value: the owner request inbox is pull-only today), calendar second (needs new change
detection that does not exist), and decline invite responses and "source disconnected" for now. Options (b) and (c) are
rejected with evidence below. Seven child cards are recommended (section 7); the journal, links and router need an Astra review and migrations.

Three facts decide it:

1. **The calendar service cannot tell what changed.** It has no sync token, watch channel, change log or background
   polling; a window is read only when an agent asks and is then overwritten (`store.py:511-553`, `api.py:230-256`).
   "Created, changed, cancelled" does not exist as data. "Starting soon" has no trigger. Invite responses have no data
   (Google sets `guests: bool(attendees)` only; invitations are unsupported in CONTRACTS.md).
2. **The calls service already has a journal.** `voice_events` is append-only with an autoincrement `seq` and `call_id`
   (`core.py:382`, `_event` at `core.py:690`), and a single terminal writer, `stop` (`core.py:598`). Owner requests are
   first-class (`decision_requested`, `core.py:877`) but explicitly not notified: `notification_channel: "not_connected"`
   (`agent_api.py:~554`), `OWNER_NOTICE` (`agent_api.py:241-244`).
3. **Messaging's journal is one gapless sequence that every writer shares a lock on.** `append_event` takes
   `UPDATE feed_counter ... RETURNING` inside the writer's transaction (`store.py:1424`); the matcher requires
   contiguous ids and otherwise blocks the subscription with `journal_gap`, `requires_operator`
   (`subscriptions.py:362-366, :404-408`). A writer that holds that row open stalls every message hint.

## 1. What exists

| | Messaging | Calendar | Calls |
|---|---|---|---|
| Store | PostgreSQL 18, `events(id, account NOT NULL, message NOT NULL, kind, revision, origin, snapshot jsonb, event_key)` (schema.sql:41, migrations/002:31-36) | own PostgreSQL 18 (`fcs` schema), own volume; `resources.etag` (latest only), `occurrences`, `coverage`, `sync_jobs`, `operations` journal (svc/schema*.sql) | SQLite `voice.sqlite3`, `journal_mode=DELETE`, one process, one `RLock` on the event loop (`core.py:376-379`) |
| Event journal | yes, gapless `feed_counter` | **none** (operation journal is writes only) | `voice_events` (seq, call_id, kind) |
| Subscriptions | definitions, sources `(account, chat)` with FK to chats (026:47-54), matches, outbox, dispatcher | none | none |
| Identity of the caller | principal: accounts and chat IDs | calendar principals and `agent_calendars` | principals with scopes, channels, destinations |
| References | message_id, chat_id, account_id | `calendar_ref` (operator text), `event_ref = "event-"+hash(resource_ref, occurrence id)`, `series_ref` (providers.py:52-63) | `call_id` = uuid4 (`core.py:524`); `brief_id` unique per call |
| Time triggers | none needed | `Worker.run` loop `tick()` + sleep 0.5 s; nothing time-driven (`worker.py:411-445`) | `LearningWorker` only; skipped while a call is active (`learning.py:409`) |
| Network | `backend` internal + `frontend` alias `anymsg-api` (compose.yaml:69-72) | `calendar-backend` internal, `provider-egress` (worker), `integration` (api, alias `calendar-api`) | `integration`, `calls-egress`; `127.0.0.1:13842` |
| Calls to other services | none to modules | none to messaging (grep `anymsg\|messaging\|subscription` empty) | read-only REST to messaging at prepare time only (`anymsg.py:13-19`, 20 s timeout) |

Three separate identity systems: no mapping exists from a messaging key to a calendar principal or a calls principal.
This is the access problem every option has to solve (section 3).

Current states relevant to calls: statuses `waiting_for_browser, dialing, ringing, connecting, active` are active
(`core.py:20`); terminal `finished`, `failed` (model error), `interrupted` (service restart, `core.py:392-394`). There is no
`missed`/`no_answer` status: those arrive as `end_reason` (`provider_no-answer`, `provider_busy`, `provider_canceled`,
`provider_failed`, `provider_completed`; `outbound.py:312-324`). There is no "transcript ready" event: the transcript is final
when `stop` writes the terminal status; the later analysis (`voice_reports`, Gemini review, `learning.py:402-440`) is a
separate asynchronous step.

## 2. How others solve it

| Source | What they do | Take |
|---|---|---|
| Google Calendar push channels (`events.watch`) | A channel per resource with an expiration (the more restrictive of the request and Google's limit; shown in `X-Goog-Channel-Expiration`), a first `sync` message, then body-less notifications (`X-Goog-Resource-State`, message number). The receiver calls `events.list` with a sync token to learn what changed | **Adapt the shape, not the transport.** The idea "a notification only says *something changed*; a cursor read says what" is exactly DeedKit's content-free hint plus fetch. The transport needs a public HTTPS endpoint into `calendar-api`, which listens on loopback only; reject for now. The diff-against-a-cursor part is what the calendar lacks |
| iCloud / CalDAV | No push; clients poll with ctag/etag | Poll. The existing windowed read already does this on demand |
| Twilio status callbacks | Per-call callback per requested event (`initiated, ringing, answered, completed`); `CallStatus` completed, busy, failed, no-answer, canceled; `CallDuration` only on completed | **Adopt the vocabulary** for `end_reason` mapping: `completed`, `busy`, `no_answer`, `canceled`, `failed`. DeedKit already stores them as `provider_*` end reasons |
| CloudEvents 1.0 | Required attributes `id`, `source`, `specversion`, `type`; optional `subject`, `time`, `datacontenttype`, `dataschema`; `source`+`id` unique, duplicates share both | **Adopt attribute names** for the module event envelope (not the HTTP binding): `id` (module + seq), `source` (`deedkit:calendar`, `deedkit:calls`), `type` (`calendar.event.cancelled`), `subject` (the ref), `time`. Dedupe key = `source`+`id` = existing `events.event_key` unique index |
| Standard Webhooks / Stripe | Already adopted by push research S2a | no change |

(Sources: Google Calendar push guide developers.google.com/workspace/calendar/v3/push; Twilio Call resource twilio.com/docs/voice/api/call-resource; CloudEvents spec v1.0.2. Retry behaviour of Twilio callbacks was not found and is not relied on.)

## 3. Options compared

Criteria: failure isolation (a calendar or calls outage or stall must not stall message hints, and the reverse: the
subscription path must not stall calendar sync or a live call), data egress, schema and role changes, compatibility with
what is deployed (N-1 schema, installed gateway), effort.

### (a) Each module keeps its own journal; a router pulls and publishes into messaging's outbox path

- Calls: read-only `GET /v1/events?after=<seq>&limit=<n>` over `voice_events` plus terminal-state projection. Calendar: a new `change_journal` table written inside the same transaction that replaces a window; same read endpoint. Contract: CloudEvents-named envelope, content-free `data`.
- Router: `messaging_service/event_router.py`, a worker like `subscription_worker` with its own restricted DB role and private read tokens, polling each module with a timeout, persisting `(module, last_seq)`, publishing through a fenced SECURITY DEFINER function (pattern: `publish_file_text_event`, collector_role.sql:50) with `event_key = source:id` for idempotent retry.
- **Failure isolation:** modules never call out (pull), so calendar's serial worker loop (`worker.py:411`) and the calls event loop cannot hang on messaging. A module outage makes the router back off; the router publishes into a separate module journal with its own counter (decision a2 below), so it takes no part in the `feed_counter` lock and cannot stall a message hint; its publish is a short database-only transaction with a deadline, and all network work happens before it. A router crash loses nothing: cursor and `event_key` make replay idempotent. Module read handlers share the module's lock/loop: the calls handler must be O(limit) on the `seq` index and obey the single `RLock` (`core.py:376`), which is the only live-call risk (a test must show a 10k-row backlog read does not delay a transcript write beyond a bound).
- **Data egress:** module-to-router HTTP stays on the host; the journal, cursors and backups hold references only (ids, kind, timestamps, enumerated reason), no titles, locations, transcripts or destinations. A delivered hint reaches the subscriber's configured receiver, so it leaves the host: it carries kind, the `subject` ref and the module name only, and the card for the router states and tests that payload. Background calendar refresh contacts Google or CalDAV. Module read tokens are new secrets (owner-kept).
- **Schema/roles:** messaging: generalise `events` (nullable `account`/`message` for non-message sources, a `source` column) or add a separate journal (sub-option below); `subscription_sources` gets a scope kind beyond `(account, chat)`; router role; a link table `(messaging principal, module, module principal)` set by the operator. Calendar: `change_journal`. Calls: reuse `voice_events` or one small table.
- **Compatibility:** the risky part. Named consumers of `events` that assume a message: the matcher (`subscriptions.py:351-356` reads `account` and `snapshot->chat`), `get_updates`/consumers, retention and the backup table map (backup.py:28). Sub-option a1 generalises `events` (least code, but shares the `feed_counter` lock with every message hint and needs a gate old readers respect: the S5 review found that rolling back to a plain `paused` state lets the old API resume a dynamic definition, Astra r2 M8); sub-option a2 adds a separate `module_events` journal with its own counter and matches (old code never sees it and no lock is shared, but position semantics of `subscription_outbox.through_position` need a second dimension). **Decision after card review: a2.** Either way the probe "old matcher, old get_updates and old retention ignore the new rows" is a test, and the backup table inventory must cover the new tables (the deployed backup rejects a schema above its own, backup.py:70, as for any migration).
- **Effort:** journal, matching and contract 8, router and deployment 5, calls producer 5, links/scope 8, calendar change journal 8, calendar refresh and reminders 5, capabilities/gateway 3.

### (b) Each module runs its own subscriptions and dispatcher under a shared contract

- Failure isolation: best (nothing shared). Egress: each dispatcher needs the SSRF-hardened webhook code (event_delivery.py:60-77), pinned TLS and the operator host policy copied into two more services; calendar's worker would gain outbound traffic it does not have; the calls service would gain an egress path next to live audio.
- Schema: matcher, ACK/fetch, outbox, hint policy, budget, retention (ANYM-182/217/218/252/446) reimplemented on SQLite (calls) and on `fcs` (calendar). The calls store is single-process SQLite with one `RLock`; a dispatcher with leases and retries is the opposite of its design (README: "SQLite is designed for one process").
- Experience: an agent registers three subscriptions and three endpoints, and nothing orders events across modules. That is not "one model", only one contract.
- Effort: at least three times the push epic's S1-S3. Rejected.

### (c) Calendar and calls publish directly into the messaging journal through a narrow write role

- Needs a messaging DB role reachable from the calendar worker and the calls service; both run on networks with no route to messaging's internal `backend`; a DB credential in each is a new secret and widens the trust boundary (calls holds a live-audio path).
- Failure isolation fails both ways: the calendar worker is a single serial loop, so a slow messaging write blocks sync and operations; the calls event loop shares a lock with live transcript writes; and a publisher holding the `feed_counter` row, or one that dies after taking a number, makes the matcher block every message subscription with `journal_gap`.
- Compatibility: same `events` generalisation as (a1), with no pull cursor to replay from.
- Effort looks lowest (about 8) but the failure modes are the worst. Rejected.

### Decision table

| Criterion | (a) router pulls | (b) own dispatchers | (c) direct publish |
|---|---|---|---|
| A module outage stalls message hints | no | no | **yes** (lock, `journal_gap`) |
| Subscription path stalls a module | no (pull) | yes (dispatcher in module) | **yes** (calendar serial loop, calls lock) |
| New secrets | module read tokens | webhook secrets x3 | messaging DB credential x2 |
| One subscription model for the agent | yes | no | yes |
| Schema changes | messaging + calendar + calls journals | all three | messaging |
| Effort (story points, sum of children) | 29 | 40+ | about 8 + hidden costs |

## 4. Event kinds and payload references (criterion 2)

Envelope (CloudEvents attribute names): `specversion: "1.0"`, `id` (`<module>:<seq>`), `source` (`deedkit:calendar` or
`deedkit:calls`), `type`, `subject` (the primary ref), `time` (when it happened, UTC), `data` (refs only). No titles,
locations, descriptions, guest names, destinations, transcripts or owner answers. The hint a subscriber receives carries the kind, the `subject` ref and the module name only; the agent then calls the module's own read tool (`calendar_agenda`,
`calls_get_call_result`), which applies the module's own access rules.

Shared references used, soft-linked to the platform epic: `event_ref` and `calendar_ref` (calendar), `call_id` (calls),
`person_id` (messaging person cards, ANYM-283). `person_id` is **not** in any payload now: calendar has no attendee
identities and a call's target is a destination, not a person; add it when the epic's S8a gives a mapping. Errors use the
epic's shape (S1: `state`, `error`, `next`); a module outage shows up in the subscription's health as a per-source, self-clearing `module_unreachable since <time>` entry (the matcher's `blocked_reason` allows only `match_budget` and `journal_gap` and is rewritten each pass), not as an event. Every event also carries `urgency` (`now` or `batch`) and `expires_at`, so that an owner request is not held by batching and a `starting` reminder is not delivered after the start.

Calendar (buildable only after the change journal exists):

| type | when | data |
|---|---|---|
| `calendar.event.created` | a diff of a re-read window finds an occurrence not seen before | `calendar_ref, event_ref, start_at, version` |
| `calendar.event.changed` | etag or start/end differ | same + `changed: [start, end, details]` (field names only) |
| `calendar.event.cancelled` | the provider marks the occurrence cancelled, or the provider reports it removed (absence from the stored windows alone is never cancellation: a located move is `changed`, an unlocated disappearance produces no event, failed or partial coverage is no evidence) | `calendar_ref, event_ref, start_at` (taken from the stored row; the ref cannot be rebuilt otherwise, providers.py:225) |
| `calendar.event.starting` | `start_at - now <= N minutes`, N set per subscription (default 15), once per `(event_ref, start_at, N)` | `calendar_ref, event_ref, start_at, lead_minutes` |
| invite response | **declined for now**: no attendee data exists (Google `guests` is a bool, iCloud checks `ATTENDEE` presence only; invitations unsupported) | needs an attendee model first (card candidate, not written) |

Limits that the cards must state: change events cover only windows the service has read; with no background refresh today a
change is seen no sooner than the next refresh; Google gives `originalStartTime` for moved instances. Rule chosen after card review: an instance whose provider identity is stored and found at a new start is `changed` (`changed: [start]`) and keeps its `event_ref`; a move out of the read window is `changed` when the identity is found in another stored window and otherwise produces no event; an unlocated disappearance is never a cancellation by itself.

Calls (buildable now from `voice_events` and `stop`):

| type | when | data |
|---|---|---|
| `call.finished` | terminal status `finished` | `call_id, status, end_reason (enum), duration_s, outcome (enum), transcript: final` |
| `call.failed` | terminal `failed` (model error, `call_errors.py:36`) or `end_reason` `provider_failed` | `call_id, status, end_reason` |
| `call.not_answered` | the card's "missed": outbound with `provider_no-answer`, `provider_busy` or `provider_canceled` | `call_id, end_reason` |
| `call.interrupted` | startup sweep (`core.py:392-394`) | `call_id, end_reason: service_restart` |
| `call.owner_request` | `decision_requested` (`core.py:877`) | `call_id, decision_id, state: pending` (no question text) |
| "transcript ready" | **folded into `call.finished`** (`transcript: final`); a separate `call.report.ready` (the learning report) is deferred because that analysis sends transcript text to a model (calls README:107-124) and the owner has not asked for it | |
| "source disconnected" | **declined for calls**: no transition is recorded anywhere; readiness is computed per request (`readiness.py`, `app.py:279-288`). The messenger side is S6 (ANYM-535). Revisit when a calls health state is stored | |

An owner-request event does not make the agent able to answer: `owner_decision_write: False` (`agent_api.py:~509`) stays.
The event only removes polling of `get_owner_requests`. Product note for the card: the owner is still not notified by
DeedKit (`OWNER_NOTICE`); this card wakes an *agent*, which is a different promise.

## 5. Access (the part that needs Astra)

A subscription today is scoped by `(account, chat)` and the key's principal (026:47-54, `subscriptions.py:331-343`). For
module events each key needs an entitlement that messaging cannot read from the modules. Proposal for the access card:

- An operator-set link table `module_links(principal, module, module_principal)`; absent means no module events, no
  capability advertised (same rule as the push defect: capabilities never name what the caller cannot use).
- Subscription sources gain a scope kind: `calendar:<calendar_ref>` allowed only for calendars the linked calendar
  principal can read, `calls:own` meaning calls whose `owner_principal` is the linked calls principal (`agent_api.py:403-415`).
- The router publishes `scope_ref` with every event; the matcher compares it with the subscription's sources exactly as it
  compares `(account, chat)` now. Revoking the link or the module permission sets the subscription `access_changed`, the
  existing state (`subscriptions.py:328, :342`).
- Granting a link is an access change: owner-level per AGENTS.md section 2.

## 6. Self-check for the PR/card review (AGENTS.md section 5)

1. Failure isolation: (a) pull model, module handlers bounded; lines to defend are `store.py:1424` (lock) and `worker.py:411` (serial loop).
2. Data egress: references only in storage; a delivered hint (kind, subject ref, module name) leaves the host to the configured receiver; calendar refresh contacts the providers; the only new secrets are module read tokens (owner-kept).
3. Deployed-version compatibility: N-1 matcher, `get_updates`, retention and backup must ignore the new rows; named consumers in section 3(a). The installed gateway profile gains nothing (README:244-253).
4. Tests: the cards name them; the first failing test per card is the cross-version one.

Not measured: Google/iCloud poll cost for a refresh cadence; the calls read-handler latency under a live call.

## 7. Child cards

The card text, criteria and status are kept in Tiden only; this list is the recommendation's shape. Review of the cards by Fable and Astra changed the first draft: the journal and the router are two cards, the calendar side is two, and the access card carries the scope and revocation protocol.

| Card | Title | Value | Effort | Depends |
|---|---|---|---|---|
| ANYM-562 | Module events have a journal of their own in messaging: storage, matching, contract, backup, old-reader safety | 5 | 8 | none |
| ANYM-575 | A router carries module events into messaging without stalling message hints | 5 | 5 | ANYM-562 |
| ANYM-563 | Call events: terminal events, `GET /v1/events` | 5 | 5 | ANYM-562 and ANYM-564 for delivery |
| ANYM-564 | Links, scoped sources, authoritative scope and revocation | 4 | 8 | ANYM-562 |
| ANYM-565 | Calendar passive change journal and endpoint | 4 | 8 | delivery needs ANYM-562 and ANYM-564 |
| ANYM-576 | Scheduled refresh of configured calendars and starting reminders | 3 | 5 | ANYM-565 |
| ANYM-566 | Capabilities and gateway catalog for module kinds | 3 | 3 | ANYM-562, ANYM-564 |

First owner-visible wake (call finished, owner request): ANYM-562, 575, 563, 564 (calls scope) and 566 (calls kinds). Calendar kinds follow.
Declined, with reason: (1) invite-response events: calendar has no attendee data, so an attendee model comes first.
(2) Calls "source disconnected": no stored transition; ask again after S6. (3) A separate `call.report.ready`: model-analysis
egress and no owner request. (4) Google Calendar push channels: needs public ingress to a loopback-only service and a channel
renewal job; the provider's incremental sync (Google `syncToken`) or a window diff in ANYM-565 gives the same result without ingress. (5) Options (b) and (c), section 3.

Not decided here: whether the calendar cards should wait until an owner needs calendar wakes at all. The calls card alone delivers the owner-request and
finished-call wakes the feedback named first; the journal, router and links are its prerequisites.
