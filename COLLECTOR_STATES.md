# Collector observations (0.9.6 / schema 18)

`get_sync_status` preserves legacy `status`, `detail_code`, partial coverage and
chat checkpoints. Account-scoped readers additionally receive four independent
`operations` objects. Chat-restricted readers do not receive account health,
account queues, processing profile summaries or lease observations.

- `connection`: connected/connecting, retry wait, owner block or stopped. Includes
  `reason_code`, `changed_at`, `observed_at`, `retry_at`, `retry_attempts`,
  `next_action` and the current collector lease's running indication. An expired
  lease makes a previously connected observation `not_running`; the recorded
  state and actual failure are retained. The lease verifies collector ownership,
  not provider history or absence of missed callbacks.
- `live_capture`: independent durable-spool drain state, pending capture count,
  cumulative committed observations and latest commit timing. A quiet account is
  not marked stale because no new message arrived. A healthy empty drain also
  does not prove the provider delivered every callback. Skipped live items and the
  cause of the last capture stop are reported here too (`rejected_live_captures`,
  `rejected_live_messages`, `last_capture_stop`; COLLECTORS.md, Queue quarantine
  and resilience).
- `history`: traversal evidence, partial history and gaps; it never claims full
  history. A WhatsApp gap can coexist with connected/receiving live capture: the
  legacy account `status` stays `receiving` with `detail_code=whatsapp_catchup_gap`,
  and the gap remains in `history.gap_chats` and the `provider_history_gaps` alert.
  Reconciliation and backfill pages use their own durable spool (WhatsApp pages,
  Telegram `history/` beside the live spool); `history.pending_events` counts them,
  so they never make `live_capture` look backlogged.
- `media`: separate queued/processing/failed original counts and bounded error
  codes with recommended actions. A failed original does not stop live messages.

Account-scoped readers also receive `liveness`, a summary of the live session
that does not depend on new messages or history: `idle_connected` (open session,
no live backlog, no recent live event; `quiet_but_healthy=true`), `receiving`,
`capture_backlogged`, `unconfirmed`, `not_connected`, `blocked` or
`collector_not_running`. `session_confirmed_at` is the drain loop's refresh, which
runs about every 15 seconds only while the provider session is open; older than
60 seconds is `unconfirmed`. `last_live_event_at` includes receipts and read
states, not only messages. It does not prove every provider callback arrived.
The compact view carries only `liveness_state`; `detail=full` has the block.

### Version conflicts (ANYM-557)

Account-scoped readers get one conflict line per account, `conflicts`:

- `attention`: open conflicts whose class is not known (including `unclassified`).
- `known`: open conflicts of a known class: expected to stay unresolved, no action.
- `top_classes`: at most 3 `{class, count, explanation}`, largest first.
- `next_step`: `none_required` when every open conflict is known, otherwise
  `inspect_chat`: call `get_sync_status` with a `chat_id` to list that chat's
  open conflicts.

`detail=compact` omits `conflicts` when both counts are 0. `active_conflicts` and
`historical_conflicts` keep their totals for older readers. `alerts` contains
`archive_version_conflicts` only while `attention` > 0. The archive detail code
itself stays recorded, and while it is `version_conflict` its `next_action` is the
line's `next_step`; without an unresolved conflict it is reported as
`coverage_not_reconciled`. A key restricted to some chats gets no account line.

`get_sync_status` with a `chat_id` returns `conflicts` with `active`,
`historical`, the same four keys for that chat, and `items`: at most 20 open
conflicts (attention first, newest first), each `{message_id, class,
explanation, observed_at}` with no message text. Search, message and context
results give a message with an open conflict `evidence.conflict{class,
next_step}`; its `next_action` is `inspect_chat` only for a class that needs
attention.

Each conflict stores its class in `message_conflicts.reason` with
`reason_version` (the rule version, now 1). The class compares the conflict's
receipt (the original candidate) with the revision it contradicted, never the
current revision. It is `<platform>_<field>_<relation>`, with platform
`telegram` or `whatsapp`; field is the first that differs, in the order the
archive tests them:

| Field | Meaning |
|---|---|
| `sent_at` | the sent time differs |
| `direction` | incoming vs outgoing differs |
| `sender` | the sender ID differs |
| `body` | the text differs |
| `media` | the attachment descriptors differ |
| `other` | another field differs (formatting or deletion) |

| Relation | Meaning |
|---|---|
| `equal_version` | the candidate has the same provider update time |
| `older_version` | the candidate is an older provider observation |
| `newer_version` | the candidate is a newer provider observation |

Known classes (a reviewed table in `conflict_classes.py`; the shape of a class
never makes it known):

| Class | Meaning |
|---|---|
| `telegram_body_equal_version` | two texts at one provider version; the archived revision stands until a later edit |
| `telegram_direction_equal_version` | the other direction at one provider version; the archived revision stands until later evidence |

`unclassified`: evidence the rule cannot read (for example a malformed legacy
receipt), and every row without a stored class (written by older code). It
counts as attention. Rows from before migration 054 keep no class until a
follow-up card classifies them.

### Conflict re-evaluation (ANYM-621)

A rule fix changes only what ingest raises from then on. A rule card registers a
job in `conflict_reevaluation.py`, and each collector runs its account's jobs at
start, on its own thread and database connection: `classify` stores a class on
open rows without one; `resolve` closes open rows of the job's classes that the
current rule would not raise (the retained revision stands); `accept` writes the
completed revision through ingest. Rule jobs wait for the account's classification
(`last_error` `classify_pending`). Each resolve records `message_conflicts.resolution`:
`newer_observation` or `sender_completed` from ingest, `rule_changed:<job>@<version>`
from a job; NULL is unknown (rows resolved before schema 57 or by older code).

Account-scoped `get_sync_status` (`detail=full`) shows `conflict_reevaluations`,
one `{job, version, done, done_at, rows_resolved, last_error}` per job the
account's collector has run; empty before a rule card registers a job. `last_error`
is a fixed code: `evaluation_error`, `invalid_result`, `evaluation_timeout` (a job
function ran over 10 s; the next start skips that row, and a classification stores
`unclassified` for it), `lock_timeout`, `statement_timeout`, `wall_deadline`,
`accept_rejected`, `classify_pending` or `rule_still_raises` (the job asked to resolve
a row today's ingest rule still raises; the row stays open). A job with an error
retries at the next collector start.

Telegram history reconcile isolates a provider error of one chat (ANYM-293).
Rate limits (`FloodWaitError`, the governor's cooldown), session-terminal
`UnauthorizedError`s, a duplicated authorization key, transport loss
(`ConnectionError`, other `OSError`, a cancelled read), the transport's
authorization key or crypto, the local session database (`sqlite3`) and memory
still stop the pass as before, and a private, removed or invalid peer is still
`provider_unavailable`. A transient server error (`ServerError`, Telegram's
`TimedOutError`, `TimeoutError`) of one chat's history page is retried for that
chat only, after 1 s and then 2 s; a chat may fail this way at most 3 times per
pass over all its pages (successful pages do not count), and the third failure
fails the chat with reason `provider_temporarily_unavailable`. Any other error
of that page, an RPC error or a Telethon library error such as an unresolvable
entity or an unknown TL constructor, fails the chat with reason
`provider_chat_error`, with no retry. The pass goes on with the next chat in the
usual order, and account status becomes `degraded` /
`configured_chat_unavailable`, as for an unavailable chat. The failed chat's
`last_error_code` is `provider_unavailable`, a code every deployed reader
already knows; `history.chat_errors` in the account's operations keeps the
exact reason for the last 20 failed chats (chat, `reason_code`, time, the
exception class name and, for an RPC error, Telegram's fixed code such as
`BAD_REQUEST`), and the collector prints one `telegram_chat_error` line per chat
and process with the reason, the class name and code frames. Exception text,
which can carry provider bytes, is never kept. The trade-off: a provider bug
that hits every chat now leaves the collector running with every chat failed
and status degraded, instead of restarting it.

Connection failures survive Node exits through a private fsynced side-channel
file. No unsolicited stdout frame can interleave with a page/media RPC. Raw
exception text, QR contents, credentials, media keys and paths are not exposed.
Transient reconnects honor a database deadline across restarts: 5, 10, 20, 40,
80, 160, then 300 seconds. Successful connection resets the attempt counter.
Startup timeout and cancellation close the owned child before another starts.

WhatsApp logout, QR requirement, rejected authentication and identity mismatch
block automatic reconnection. An ordinary container restart preserves this
block. Use the existing owner-terminal pairing flow if required, with the
collector stopped and exclusive session ownership. After fixing the diagnosed
cause, an operator may explicitly clear the selected account's block:

```sh
python -m messaging_service.collector_control --dsn-file /run/secrets/owner.dsn \
  --account example-whatsapp --resume
```

This owner-only command does not pair, send, delete a session or clear history
gaps/conflicts. It is not an MCP tool. The running blocked supervisor observes
the explicit resume, or a later collector start proceeds using the same session.

`last_commit` distinguishes `captured_at` from provider message time and gives
`committed_by`, an upper bound recorded after the archive transaction returns.
`capture_to_commit_seconds` includes durable queue delay and replay downtime;
`provider_age_at_capture_seconds` includes old callbacks/history observations.
The origin is retained, so backfill/reconciliation are not fresh-message latency
samples. Clock skew may make provider age negative. Old spool entries without a
capture timestamp report unknown timing, never substitute message creation time.
`mcp_available_after_commit` describes database visibility for a subsequent read,
not client polling/notification time or an end-to-end delivery SLA.

Processing profile configuration is independent of individual enrichment jobs.
The worker publishes only kind, provider, sanitized model label and a bounded
liveness deadline. No model path, URL or key file is exposed. An unvisited file
with an enabled profile is queued; a stale worker is diagnosed separately by
`get_file_segments` as `processing_worker_unavailable`. Ready outputs still
require matching current digest/revision. Run one configured profile per
account/kind; parallel workers must use the same profile for that account/kind.

Schema 18 adds operational JSON and a profile registry; message revisions,
original bytes, provider IDs, ACK positions, policy and sessions are unchanged by
the migration. Backups include the new table. Older backups through schema 17
remain readable, but an old schema-17-only backup runtime must not capture the
upgraded database. Promote recovery code/image with the migration. This release
does not close existing history gaps, reset failed downloads, migrate the
WhatsApp engine or enable sending.
