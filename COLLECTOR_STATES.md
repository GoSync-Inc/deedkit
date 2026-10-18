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

`alerts` contains `archive_version_conflicts` only while a conflict is unresolved
(`active_conflicts`). The archive detail code itself stays recorded; without an
unresolved conflict it is reported as `coverage_not_reconciled`.

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
