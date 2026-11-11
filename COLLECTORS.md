# Telegram collector: preparing to connect

The owner connects each account in their own terminal. The number of accounts
is not limited to two: compose shows examples of two independent WhatsApp accounts and
one Telegram account. Further accounts need separate configs, sessions,
spools, account IDs and services. Live collection checks apply to a specific
installation and do not prove the completeness of another user's history.

## Design

- Telethon 1.45.0 is pinned by lock files. The new/edit/delete handlers first save the capture to a bounded queue with fsync. An unavailable DB does not hold the queue write lock. A queue overflow or save error stops the collector and requires reconciliation.
- The process holds a local `flock` on the session file and a renewable DB lease. Separate leases protect media jobs; a process that has lost ownership cannot finish an ingest or publish a file. This is not a way to stop an old Mac client remotely: its shutdown and autostart are checked before handover.
- One provider client per account across hosts (ANYM-914, schema 78). Every opener (the Telegram and native WhatsApp collectors, `telegram_pair`, `whatsapp_pair`) first takes the canonical host lock `/run/deedkit/session-locks/<account>.lock` (host directory `deploy/private/session-locks`, mounted by compose.collectors.yaml and compose.whatsapp.yaml; the native and pair overlays inherit it), then the per-directory `.owner.lock`, then the account's row in `session_claims`, and only then opens the session store. The row names the host (`session-locks/host_id`, random, created once), the kernel boot ID and the lock file. The same host, boot and lock file take it over at once (the kernel flock proves the earlier owner gone); anything else waits for a clean release or the operator release below. A refused collector waits in-process (5 s doubling to 5 min, one log line an hour) with no session open and shows `waiting_host_id` in `session_owner`; a pairing command reports the refusal at once. A database outage at start waits the same way, before the claim (no exit for Compose to restart); a shutdown releases the row right after the verified disconnect and before other cleanup, within 5 s. A database outage never disconnects a running session through the claim (the write lease's own deadline is unchanged). The native child refuses to open `auth.sqlite` without the inherited canonical lock descriptor (`ANYMSG_SESSION_CLAIM_FD`). The browser fallback is not claimed: while `session-locks` is mounted (or a configured `session_lock_directory` exists) it refuses with `session_claim_unsupported`, in the Python parent and in `provider.mjs`/`pair.mjs` run directly; it runs only from an overlay without that mount, with the per-directory guarantee alone.

| Code | Layer | What an agent does |
|---|---|---|
| `session_already_owned` | flock (canonical or per-directory) | Another process on this host holds the session: find and stop the second opener; never start a second client. |
| `session_claimed_elsewhere` | claim row | Another host, boot or lock directory owns it. Confirm that host is stopped, then `deedkit admin session-claim release --account <a> --other-host-down <host_id>` (a provider-session start: the owner's exact confirmation, except incident recovery of an existing session after this host crashed or rebooted uncleanly). |
| `session_claim_restore_hold` | claim row | A restored database. Stop the original environment's session services first, then the same release per account. |
| `session_claim_lost` | claim row | The row changed under a running client (an operator released a live owner): the client stopped; check which host should own it before any release. |
| `session_claim_required` | native child | Started without the parent's claim (a direct `anymsg-whatsmeow` run): start it through the collector or `whatsapp_pair`. |
| `session_claim_unsupported` | browser fallback | Claims are on: use the native provider, or the documented overlay without `session-locks`. |
| `host_id_invalid` | host identity | `session-locks/host_id` or the boot ID is unreadable or malformed: fix the file (a new host gets a new directory); never copy it from another host. |
| `foreign_session_opener` | `production.py deploy` | A one-off or foreign container holds a session directory: stop it before the release. |
| `pre_claim_rollback_refused` | `production.py rollback` | The target predates the claim: roll forward, or pass `--pre-claim-rollback "<reason>"` only while one host runs sessions. |
| `session_locks_unmounted` | `production.py prepare`/`apply` | The host's Compose files lack the `session-locks` bind or the 25 s stop grace on a session service: install the host Compose change first (production.md, First claim release). |
| `session_locks_unsafe` | `production.py prepare`/`apply` | The host's `session-locks` directory is a symlink, not a directory, shared or of another owner: fix it to uid 10001, mode 0700 (never repaired, never copied from another host). |

Session holders (ANYM-937 WhatsApp, ANYM-938 Telegram). A holder is the account's only session opener: it takes the canonical host lock, the per-directory locks and the claim with `holder_kind=holder` (the same statements as `session_claim.py`), opens the session store, and serves the collector on two unix sockets (`control.sock`, `send.sock`, mode 0600) in a private 0700 socket directory with a fresh 0600 `token`. A refused or failed acquire waits in-process with the backoff above and serves nothing. The collector attaches; it no longer owns the session. The rules both holders keep:

- Handshake. The holder speaks first on each lane: `hello {protocol, supported: [min, max], holder_source_sha, account, methods[]}`; the collector answers `hello {protocol, supported}` with the token and its write lease `{owner, epoch}`. A peer of another UID or with another token is refused before any method. The agreed protocol is the higher common one; a collector below the holder's range is refused with `collector_protocol_unsupported`, a holder below the collector's with `holder_protocol_unsupported`, with no method called and no send. A holder supports at least the previous collector protocol, and a collector at least the previous holder protocol. A method absent from `methods[]` is `operation_unsupported` and never called.
- Attach authority. The holder checks the lease in `collector_leases` through its `session_holder` login at attach, at each 15 s keepalive and before every send-lane method. Only a positive answer that the lease is not live (`collector_lease_invalid`: a forged owner or epoch, a released or expired lease, a successor's lease) detaches; while the database cannot answer, the last verified lease holds until its own `expires_at`, and a new attach is refused (`collector_lease_unverified`, or `holder_busy` while one is attached). One attachment covers both lanes; a second live one is `holder_busy`.
- Detached mode. Policy authority comes only from the attached collector: each keepalive carries the policy with `{version, expires_at}`, `expires_at` at most 45 s ahead (the holder bounds it), refreshed every 15 s and once more right before a planned collector stop. A version never goes back. While the policy is fresh the holder captures as before, exclusions applied before any write; when it expires (the collector gone longer than 45 s) the holder disconnects from the provider at once and captures nothing under the expired policy. It reconnects only when a collector attaches with a fresh policy and calls `start`; the provider then delivers what came meanwhile, as after any restart. A collector restart that re-attaches inside the 45 s keeps the connection (the holder's `start` answers with the running connection's generation); a longer absence costs one disconnect and no reconnect until a collector is back.
- Mutations. Text sends keep their receipt journal. Messenger operations go through the holder's outcome journal (`<events>/operation-outcomes/<command_row_id>.<step>.json`): the pending record is durable before the provider call and the verified outcome before the reply; a repeated key returns the journaled outcome, and a key left pending by a holder that ended mid-call never sends again and answers `operation_outcome_unknown`.
- Failures and evidence. A session-terminal error (`whatsapp_logged_out`, `whatsapp_pairing_required`, `provider_auth_failed`, `whatsapp_identity_mismatch`) keeps the holder up and idle with no further connect until a pairing or a `deedkit admin` action replaces the session store (`.holder-terminal` in the session directory records it across holder restarts). The holder logs `holder.attached`, `holder.detached`, and one `holder.provider_connected` / `holder.provider_disconnected` per provider connection with its connection generation (kept in the session directory as `.holder-generation`), reason and uptime; never message data.
- The collector's side (WhatsApp: `whatsapp_holder.py`, docs/whatsapp-native.md). The policy `version` is the collector's lease epoch shifted left by 20 bits plus its count of policy changes, so it rises with every change and a successor collector's (higher epoch) is newer than anything its predecessor sent; the heartbeat sends the keepalive every 15 s and the stop sends one more. The drain checks every spooled record against the account's current policy (CollectorState.apply re-reads it), so a record of a chat excluded after it was captured is dropped whatever version it was captured under. After a restart, a predecessor's `dispatching` command whose journal record is a verified, confirmed one-step outcome takes that outcome (`succeeded`); every other one (pending, refused, several steps, the open-chat and history operations) is left to the claim's sweep, which ends it `unknown` / `operation_result_unknown`, never retried. The holder's `operation_outcome_unknown` reply is that same `unknown`. When the lanes end the collector reports `provider_disconnected` with a reason in source status (`operations.holder.reason`): `provider` (the holder answered that it has no provider connection), `policy_expired`, `session_window` (a planned-stop marker `{reason: "session_window", planned_end}` in the socket directory, written after this attachment began) or else `holder_restart`; its run ends and the next one reattaches under the ordinary connection backoff. While a holder is configured the collector never starts its own child.
- Night window (ANYM-896, docs/operations/production.md "Session holders"). A holder is recreated for a change only from 03:00 to 05:00 Bali time (WITA), at most once a night, by the release that comes then or by the night's session-only pass at a per-night minute between 03:30 and 04:30; a stopped or unhealthy holder goes at once, and a failed night recreate rolls back in the same pass. Its collector is never held back. Only an agent under the incident recovery override (with its runbook record) or with the owner's quoted line may recreate a holder at once: `production.py deploy --session-restart-now <holder> --reason "incident:<record>"` or `"owner:<line>"`.

Runbook: a changed, cloned or restored host gets a new `session-locks` directory (never a copy) and needs the operator release once per account; so does a host that rebooted without stopping its collectors cleanly (the telegram service has `stop_grace_period: 25s`, above the 15 s disconnect bound, so a normal shutdown releases).
- The directory that holds a session and its `.owner.lock` (Telegram: the directory of `session_file`; WhatsApp: the parent of `session_directory`) must be a real directory owned by the collector's UID or by root, with no group or other permissions. Every directory used to reach it, including those that resolve a symlink on the way (like macOS `/tmp`), must be owned by that UID or root and not writable by group or others unless it is sticky (like `/tmp`), and every such symlink must be owned by that UID or root, so no other user can redirect the path after the check. The session itself and its directory must not be symlinks. Otherwise the collectors and the pairing commands stop before they create the lock or open the provider: with `private_session_required`, or `session_symlink` for a symlinked session or session directory (a collector's own earlier check may report `private_session_required` first). They never repair modes: the operator runs `chmod 700 <dir>` (and `chown 10001:10001 <dir>` for a foreign owner) and starts again. The Telegram collector also requires an existing `session_file` that is a regular file without group or other permissions, not a symlink; Telegram pairing sets the file to 0600 after sign-in.
- Own role (schema 46): each discovery pass reads, from the dialog entities it already loads and with no extra request, the account's `my_role` in a channel or group (`creator`, `admin`, `member`, `left`, `unknown`), `can_post` (null when not known) and the channel @username, stamped `role_observed_at`. A cached replay between passes keeps the stamp; a chat the latest pass did not list reads `unknown`. Malformed role data reads `unknown` and never aborts a batch. Before migration 046 nothing is stored and every row reads `unknown`.
- How a chat enters the catalog (ANYM-845): with its first captured live message, or with the next dialog discovery pass, whichever comes first, as long as the account's collection policy allows the chat. Service messages (creation notices, joins, title changes, pins) are captured live too (ANYM-847): Telethon's new-message event skips them (Telethon 1.45.0 `events/newmessage.py`), so a raw-update handler takes the new-message updates whose message is a service message, initializes it from the entities sent with the update (no entity-fetch request) and hands it to the same capture path. A group or channel created in the Telegram app is therefore listed with its creation notice at once, with its title and type when Telegram sent its entity; without one, the next discovery pass fills them. A service message that cannot be prepared or normalized is rejected alone as `invalid_provider_message` and never stops the session; spool failures still fail closed. The next pass is scheduled 900 s after the last one finished; that is a scheduling interval, not a deadline, and a failed pass is retried on the next loop. `list_chats` with `include_coverage=true` shows, per account in scope, `catalog_refresh: {account, last_started_at, last_completed_at, limited, interval_s, state}` with `state` `never`, `incomplete` (a pass is running or the last one failed; `last_completed_at` keeps the last good pass), `limited`, `complete`, or `not_applicable` for WhatsApp, which runs no discovery pass.
- A new chat gets a recent page; earlier history is loaded in small portions. New messages go through a separate high-water cursor from PostgreSQL. The cursor moves only after the whole page is written, and a live callback does not jump over it. If a page fails, its captures remain for replay.
- A repeated check of the recent window detects edits; persisted tombstones keep a late backfill from resurrecting a known deletion. A deletion without a chat ID applies only to the ID space of a private/basic group, not to a channel with a matching message number.
- Files are downloaded by the refreshed provider reference in 512 KiB parts. The size is checked before publication, and the worker renews the lease. FloodWait sets the minimum time of the next attempt; a thumbnail is not issued in place of the original. In 0.7.3 a PhotoSize/Progressive whose size matches the descriptor is chosen explicitly; the order of sizes in the response is not used as a guarantee. A cached/stripped-only preview stays unavailable.
- The gateway does not receive Telegram credentials. A separate collector role has DML only for ingest/jobs/positions; agent keys, consumer ACKs, grants, DDL and changes to the collection policy are not available to it. This role is intended for a trusted collector of one owner and is not isolation between different owners.

Coverage stays `partial`: Telegram does not guarantee a notification about every deletion, unavailable history is not the same as empty history, and a bounded overlap window does not prove there are no old edits. Disappearance from a recent page is not treated as a deletion. A discovery limit and an unavailable explicitly chosen chat have separate status codes.

Original downloads use [independent storage admission](MEDIA_STORAGE.md):
`media_disk_reserve_bytes` (default 10 GiB) is separate from the capture/spool
`disk_reserve_bytes` floor. Jobs deferred by local capacity remain queued with
`paused_storage`, preserve their provider attempts, and do not stop new-message
capture. Configure these floors after measuring the host and backup workspace.

### Availability of new messages

Telegram new/edit callbacks and WhatsApp `message_create` (including outgoing)
save events to a durable spool. The queue is drained every 250 ms, independently
of the periodic history check. The text and its Postgres full-text vectors become
visible after one ingest transaction. Downloading originals, extracting text
and model processing run separately and have their own statuses.
250 ms is the queue polling interval, not an SLA: provider delivery,
fsync, the DB queue and the agent's own request time are added to it. After a stop/reconnect
catch-up events are possible; `origin=live` does not prove there was no such delay.
A message the live stream lost is stored later by the periodic reconcile pass (every
`scan_interval_seconds`, 60 s by default, but paced by the request governor, so
a long pass can be much later). The governor admits one request per second per account and
gives the next slot to the most urgent tier (ANYM-877): agents' live reads and commands,
then live and requested originals, then history reconcile, discovery and backfill. A waiting
lower tier still gets at least one of every five admissions, so under continuous live work
a reconcile pass can take up to about five times longer. A FloodWait or account cooldown
stops every tier; each one is also kept as `{started_at, seconds, phase}` in
`collector_health.operations.cooldowns.recent` (the last 20), which the full `get_sync_status`
shows under `operations.cooldowns`. A reconcile pass reads no head page of a chat whose dialog catalog, seen at most 30 minutes ago in a pass whose
discovery did not fail, shows no message above its cursor; it still rereads the newest page every pass, and the
catalog's time becomes the chat's `last_head_scan_at`. An unchanged chat costs one request per pass instead of two. The journal records it as `reconcile`; if it was sent
at most 6 hours before, live subscriptions read it as `live_recovered` and match it
(SUBSCRIPTIONS.md, ANYM-733).
Stateless MCP does not wake an idle agent: the client must call read/updates.

Account sync freshness keeps successful scan age in `scan_state`, separately
from live capture state. Pending spool captures report `capture_backlogged`; an
expired collector lease reports `collector_not_running`, even after a recent
history scan. An old last commit with zero pending captures is not evidence of
lag on a quiet source. Media storage pauses qualify original availability
independently. Chat-restricted readers receive no account-wide collector state.

Owner mentions are saved by Telegram `mentioned` or by a match of WhatsApp
mentioned IDs with the cached phone/LID identities of the currently authorized account.
For WhatsApp, a quoted participant can also confirm a reply to the owner. Capture
makes no extra contact/quoted-message requests; an unknown LID is not
matched to a phone by name or numeric coincidence. Previously saved reply
IDs can be matched to an outgoing message only within the same account/chat. This is a
heuristic for the attention queue, not the provider's unread state or a fact of an obligation.

API documentation used: [Telethon catch-up and streaming download](https://docs.telethon.dev/en/stable/modules/client.html), [MessageDeleted limitations](https://docs.telethon.dev/en/stable/modules/events.html#telethon.events.messagedeleted.MessageDeleted). Real Telegram downloads were verified; long-term observation and reconnect scenarios need separate acceptance. In 0.7.2 expired or refresh-needing file references are handled by the queue with bounded attempts and a fresh metadata load, without stopping collection of other chats.

### Stage latency (ANYM-865)

`get_sync_status(include_latency=true, latency_window_h=24)` adds three lists to the account
summary. The window is 1, 24, 72 or 168 hours; any other value is `invalid_argument`.

- `latency`: one row per account, stage, origin, chat type, message kind and hint policy, with
  `n`, `p50_s`, `p90_s` and `window_h`. The stages:
  - `arrival`: commit of a live `message.created` minus the provider's `sent_at`. Negative values
    are clipped to 0. `message_kind` is `message` or the service kind.
  - `download`: `attachment.ready` minus the message's `message.created`.
  - `text`, `transcript`, `description`: the `file.*.ready` event minus that file's `attachment.ready`.
  - `failed`: a count of `file.processing.failed` events, with no percentiles.
  - `match`: `matched_at` minus the event's commit.
  - `hint`: the first delivered hint covering the match minus `matched_at`. Its `policy` is
    `immediate`, or `held_by_policy` for a debounce, batch or quiet-hours subscription.

  A stage with nothing in the window has no row.
- `latency_pending`: what waits for each stage now, as `pending: {n, oldest_s}`.
  - `download`, `text`, `transcript` and `description` add `on_request_n`, the work that waits for
    `request_file_download` by policy.
  - `hint` adds `held_n`, the matches your own debounce, batch or quiet hours hold. These are never
    counted as pending.

  A stalled worker shows a growing `oldest_s` while the window's p50 still looks normal.
- `latency_unavailable`: stages whose statement ran out of time (2 s up to 24 h, 5 s for 72 and
  168 h) or failed. The rest of the status is unaffected.

Rows hold counts and seconds only: no chat IDs, titles, senders or bodies. A key sees only the
accounts and chats it may read, and only its own subscriptions in the `match` and `hint` rows.

How exact the numbers are:
- Provider `sent_at` has 1 s resolution and comes from the provider's own clock, so arrival values
  under 1 s are indicative.
- The window filters each stage by its own commit time: `events.created_at`, served by the
  `events_created_at` index from migration 073. `match` and `hint` filter by `matched_at`, so a
  late match of an old event still counts.
- One budget covers the whole block: 2 s up to 24 h, 5 s for 72 and 168 h. A group that starts with
  the budget spent is named in `latency_unavailable`.
- A match whose first covering hint the retention worker already deleted has no hint sample. It is
  not pending either.
- A queued download with an unknown claim (no published policy or history bound) is `unknown_n`. It
  is not counted as pending.

Operators get the same rows, for every account and subscription, as one table:
`python -m messaging_service sync-latency [--window-h 24]` (operator DSN in `DATABASE_URL_FILE`).

### Queue quarantine and resilience

One message does not stop an account's collection. Adapters truncate or
neutralize display fields supplied by the sender (MIME with parameters or
control characters, long file names and profile names, NUL in text,
a size over 8 GiB, an overlong `replyToId`) instead of rejecting them. A message
that still fails validation is skipped during history reconciliation and
counted in `invalid_provider_messages` of the `live_capture` status.

Telegram live capture follows the same rule. Exactly three rejections of a
live message, deletion, read receipt or owner read are skipped, because a
restart would meet the same input again: the message adapter's `ValueError`
(an unsupported peer kind included), a malformed-input error from the receipt
or read parser (an outbox boundary of an unknown peer type included), and a rejection by the spool validator or canonical
serialisation. A skipped item is not written, not quarantined and not retried,
and it does not stop the collector. `live_capture.rejected_live_captures`
counts skipped items, cumulatively, by a code from a fixed vocabulary: the
validators' own codes (for example `invalid_deleted_ids`),
`invalid_provider_message`, `receipt_parse_failed`, `read_parse_failed` and
`receipt_peer_unsupported` (an outbox read boundary with an unknown peer type, ANYM-868) and
`receipt_deferred_full` (a group read boundary dropped because the deferred-receipts spool was full).
Any other text becomes the generic `invalid_capture`. `invalid_provider_message`
is the live counterpart of `invalid_provider_messages`: the same message counts
once in each. For a skipped message, `rejected_live_messages` keeps the chat,
message ID, reason and time of the last 20, never content. Counts and IDs are
written with the next capture-progress record, at most about 15 s later; a
failed write keeps them for the retry, but a stop before the write loses them.

Reconciliation re-reads new messages above each chat checkpoint and the newest
page of each chat. A message that is still invalid there is skipped and counted
in `invalid_provider_messages` again, never stopping the collector. Reconciliation
does not read `rejected_live_messages` and does not re-read older messages; the
recorded ID is evidence for a later fix. A skipped deletion, receipt or owner
read keeps only its count: reconciliation does not infer deletions or read
state. A deletion with more than 1,000 IDs is written as several
`telegram_delete` records of at most 1,000 IDs each, with one observed time.

Capacity and I/O still fail closed. `spool_full`, `disk_reserve`, an I/O error
(also one that is a `ValueError`), a resource error or any other unexpected
error in a live callback, including one raised while normalising a message,
stops the collector with `durable_capture_failed`. Nothing is acknowledged
before it is durable. After the restart, reconciliation re-reads messages as
above; a deletion, receipt or owner read not written before the stop is lost,
as is any part of a multi-record deletion not yet written. The cause is kept
as a fixed detail (`spool_full`, `disk_reserve`, `spool_io_error` or
`capture_error`) in `live_capture.last_capture_stop` (`code`, `detail`, `at`)
and in the supervisor line
`{"status":"stopped","code":"durable_capture_failed","detail":"disk_reserve"}`.
A `durable_capture_failed` stop without a detail did not come from a live
callback (for example a capture after a confirmed send) and is read as
`capture_error`. Exception text never leaves the process.

Telegram live callbacks (messages, deletions, read receipts and owner reads)
share 16 capture write slots, so at most 16 spool writes run at once. A
callback that finds all slots busy waits in arrival order instead of failing,
so a burst of updates (albums, busy groups, reconnect catch-up) is written in
full. Under such a burst a live event, and a subscription on it, may be
written up to 30 seconds later than usual. The collector still fails closed
with `capture_overload` when more than 512 callbacks wait, or when one
callback waits longer than 30 seconds for a slot. The two bounds mean
different things, so `live_capture.last_capture_stop.detail` and the
supervisor line name the one that tripped: `waiters_limit` is a burst larger
than the queue, `slot_wait_timeout` means one callback waited more than 30
seconds for a slot because capture writes could not keep up; a slow disk or
spool is a likely cause, not a proven one. The
overloading callback writes nothing, and neither do the up to 512 callbacks
still waiting at the stop. Reconciliation after the restart re-reads their
messages as above; their deletions, read receipts and owner reads are lost,
as for any other stop (ANYM-291).

If a spool record fails validation or its send receipt permanently
disagrees with the DB (for example, after restoring an old backup), `drain`
moves the file to `quarantine/` inside the same spool
(0700) next to `<name>.reason` with a code and no content. Later records
continue to be processed. `live_capture` shows `capture_quarantined`,
`quarantined_captures` and `next_action=inspect_capture_quarantine`. The held
records are not in the archive; nothing deletes the files. To replay a record after
fixing, move the file back, without renaming it, from `quarantine/` into the spool directory
while the collector is stopped. A corrupt file (digest does not match
the name) and an overflowing quarantine (1000 records) still stop
processing with `spool_corrupt`.

Transient PostgreSQL errors (statement timeout, dropped connection, busy
pool) do not restart the collector: reconciliation, media, sends and heartbeat
retry with a pause of up to 60 s. The lease is renewed with a local deadline:
if the DB does not respond for longer than the lease, the collector stops itself rather than waiting on a
half-open connection.

## Conditions before a live connection

1. Finish and verify the automatic encrypted offsite backup, including session/spool, and restore in isolation. Keep the rollback of the original Mac client. The previously verified one-off backup of the DB/originals does not close these conditions entirely.

   The current automatic runner has `recovery.accounts: []` because there are no real
   accounts yet. When connecting, prepare RO mounts for the session and spool,
   add the account/session_path and the private config/credentials/permit to settings.
   After pairing, while the permanent collector is off, run a backup and verify
   restore into quarantine; do not start the restored session. The backup must
   see the existing `.owner.lock`; it does not create it. After the first start,
   check the stop/capture/resume cycle and the delivery status on the real account.
2. Fix the exact account and the expected Telegram user ID. Stop the previous runtime and all its autostart paths; check that no client is running. Do not move the SQLite session while the process is running and do not run the old MCP in parallel.
3. The operator applies the schema migrations. Run [collector_role.sql](messaging_service/collector_role.sql) and set a separate password over a private connection, without printing it to stdout/argv. Save the DSN as `private/collector.dsn`. PostgreSQL is not exposed to the internet.
   Since schema 37 a collector login can be bound to its one account: install a copy of the template under a per-account role name, give it its own password and DSN file, and as owner run `INSERT INTO collector_account_roles(role_name,account) VALUES('<role>','<account>')`. A bound role's writes to messages, events, checkpoints, chats, media and receipt rows of any other account fail with `insufficient_privilege` (a `collector_account_guard` trigger). An unbound role, such as the shared `messaging_collector`, keeps writing for every account; retire its login once every collector runs bound. The feed counter and the append-only audit log stay shared.
4. Register the account through the owner CLI: `register-account --account telegram-primary --platform telegram --label Telegram --discover-chats`. For limited collection, give `--include-chats` instead of discovery; exclusions are set separately. Registration does not connect the provider.
5. Prepare `private/telegram.json`, `telegram.credentials.json`, `telegram.permit.json` from the [templates](deploy/telegram.example.json). Credential values and the expected ID are entered privately. Secrets are 0600, readable only by the collector UID and the operator; a new session directory is 0700. Create the `sessions` directories and the spool volume owned by UID 10001. The gateway does not mount them.
6. Only after verifying that the previous client has stopped, set `prior_clients_stopped: true`, matching account/expected ID and `enabled: true`. A missing secret or wrong permissions on it, a different user ID or a disabled configuration make startup fail.

A session can be moved after a clean stop or created anew by the owner. For a new login there is a command, run **by the owner themselves in their own terminal**, after the preparation above:

```sh
docker compose -f compose.yaml -f compose.collectors.yaml run --rm --no-deps telegram \
  python -m messaging_service.telegram_pair --config /run/secrets/telegram_config
```

It requires an interactive terminal, a separate DB lease and the session lock. The phone, code and password are entered hidden directly in the terminal. The gateway/MCP do not accept these fields. The command checks the expected user ID and closes the connection; it does not start the permanent collector. On an identity error collection is forbidden until the operator finds the cause.

The permanent start is a separate operator step after pairing/a verified move:

```sh
docker compose -f compose.yaml -f compose.collectors.yaml --profile collectors up -d telegram
```

The [Compose overlay](deploy/compose.collectors.yaml) does not enable the collector by default. It has no incoming ports; the root filesystem is read-only, UID 10001, dropped capabilities, CPU/RAM/PIDs limits. Only this process has access to the session, spool and writable originals. The presence of the overlay does not mean it is already running.

## Checks after connecting

Check the expected identity and account scope, a recent text/file, an edit and an explicit deletion, a restart, a temporary loss of network/DB, the size/hash of a large original and the actual RAM use. Compare provider IDs in the chosen control window. Check separately two independent consumer ACKs and a closed foreign scope. The owner can send test outgoing messages themselves; the agent needs exact confirmation to send.

Then watch for 48 hours and shut down the previous host before claiming long-term stability. Each further account is connected separately, after verifying that its session has a single owner. On rollback, first prove that the server collector has stopped and save its fresh session/state; then restore the single owner on the previous path. Do not delete the original archives.

## WhatsApp adapter

Two supervisors use the same implementation with **different account IDs,
expected WhatsApp IDs, LocalAuth profiles, event/page spool volumes and leases**.
`compose.whatsapp.yaml` is disabled without its explicit profile. Configuration
and handover permit examples start disabled/unconfirmed. Stop and disable the previous client for each account before pairing here.

The Node child runs whatsapp-web.js 1.34.7 with Puppeteer 25.13.0 pinned via an
explicit override (24.38's unused browser downloader dependency chain had audit
findings). `npm ci --ignore-scripts` never downloads a browser; the image uses
Debian Chromium and records its package inventory. Node 24 image is digest pinned
for the current amd64 deployment. Browser/provider compatibility still requires
real account acceptance; importing the modules or passing a blank-page smoke is
not evidence that WhatsApp's current private web modules work.

A private policy file expires after45seconds and is renewed with DB ownership every15seconds, independently from media RPC. Callbacks write allowlisted fields to a private, fsynced, bounded disk queue
before returning. No raw provider object, media key, signed CDN path or QR enters
the archive. Python consumes that queue after checking its hash and schema.
Failed DB writes retain the file. A separate page queue prevents concurrent Node
and Python writers from sharing a publication lock. Changes/deletions are
idempotent, and a deletion arriving before history is a durable tombstone.

WhatsApp IDs are opaque. Backfill and catch-up use persisted `(timestamp, ID)`
boundaries, independently from live callbacks and client ACK. A burst spanning
several pages is reconciled back to the last committed head before advancing it.
Loading at most four provider history batches per call bounds one request; a
cursor not yet reached stays pending, not exhausted. An unavailable bridge to the
old head is reported as a gap; the flag clears only by the owner's `--clear-gap`
command (COLLECTOR_STATES.md), a judgement based on the archive, not on the
provider. A linked device's exhausted history is always
**partial source coverage**, never proof of a full account archive. Same-second
provider ordering, very deep browser history after restart, old edits/deletions,
media keys and expired links must still be tested with a real account. Status
broadcasts, newsletters and local-only deletion events are outside this adapter's
current supported source types; private/group chats and `@lid` IDs are handled.

Media jobs refresh their reference and stream encrypted HTTPS bytes from the fixed
`mmg.whatsapp.net` host. Redirects, arbitrary URLs/headers and browser `downloadMedia`
are not used. Decryption verifies the MAC, ciphertext hash, original hash and
known length before the blob writer publishes the original. The iterator has
bounded chunks and pipe backpressure; a truncated/cancelled/bad-hash transfer
leaves no `ready` attachment. An expired original is marked unavailable; a
thumbnail never substitutes for it. Protocol reference:
[Baileys media implementation](https://github.com/WhiskeySockets/Baileys/blob/master/src/Utils/messages-media.ts).
Provider API reference:
[whatsapp-web.js Message](https://docs.wwebjs.dev/Message.html) and
[Chat source](https://docs.wwebjs.dev/structures_Chat.js.html).
These source formats are compatibility dependencies, not official WhatsApp APIs.

Owner onboarding, after backup/migration and host sandbox acceptance:

1. Record the expected phone digits as `expected_wid` (`digits@c.us`). Prepare
   separate private files for each account, owned by UID10001 and mode0600;
   session directories are mode0700. Do not reuse one profile for two services.
2. Inventory and stop the old account's one client and its autostart, then record
   its exact account/identity in the permit. Register the real account using the
   operator CLI; install the collector role grants including migration004.
3. Pair from an interactive owner terminal using the matching service:
   `docker compose -f compose.yaml -f compose.whatsapp.yaml -f compose.whatsapp-pair.yaml run --rm --no-deps whatsapp1`.
   The pairing overlay disables Docker logging; QR appears only on terminal stderr. Non-TTY execution is rejected before
   config/session access. The session lock is inherited by the Node child; a
   database lease is renewed through pairing. Wrong identity aborts. Pairing
   does not start the collector.
4. Enable/start **one** matching service, verify live texts/files/reconnect and
   archive identities, then repeat separately for the second account. Keep old
   clients disabled until a verified rollback. Confirmed text sends and allowlisted
   [messenger operations](MESSENGER_OPERATIONS.md) run through this existing
   collector's priority lane under fresh key/account/scope checks. Passive archive
   reads do not invoke this lane or mark provider chats read. Do not start a second
   client against the same profile for agent actions.

Never fix a failing browser sandbox check by adding `--no-sandbox`, privileged
mode or an unconfined container. Resolve the smallest host/container policy
needed and repeat the offline browser smoke before live pairing.


Run the offline sandbox smoke on your deployment host. Required properties:
PID namespaces=Yes, network namespaces=Yes, seccomp-BPF/TSYNC=Yes, UID10001,
no host capabilities, no-new-privileges, read-only root, network disabled and no
account mounts. The custom seccomp profile changes only this container's syscall
filter; host AppArmor/sysctls were not changed. `browser-smoke.mjs` asserts those
sandbox properties before returning success. This smoke test alone does not prove live WhatsApp compatibility.


## Chromium profile restart and cold backup (0.7.4)

Chromium leaves process-local `SingletonLock`, `SingletonSocket`, and
`SingletonCookie` symlinks in its user-data directory. Their targets refer to a
container hostname/PID or temporary socket, not login credentials. Before starting
Node, pairing and collection remove only these symlinks under the account's
exclusive session lock; unexpected regular files and symlinked profile directories
are rejected. Original authentication databases remain intact.

Cold backup omits only these three symlinks in recognized browser profiles within
session sources (`Default` directory and regular `Local State` file). It does not
follow or modify them. Every other special file still fails closed. A restored
profile therefore has no stale container locks. This does not permit concurrent
clients or replace the existing ownership/lease checks.

Reference: [Chromium process singleton implementation](https://chromium.googlesource.com/chromium/src/+/main/chrome/browser/process_singleton_posix.cc).


## Provider compatibility fixes (0.7.5–0.7.9)

Voice-note MIME parameters are normalized to their base type before ingest and
media metadata comparison; control characters remain rejected. A bounded page
load returns `pending` after five seconds instead of terminating all collection
on a stalled chat. Outstanding browser history loads are reused per chat and
capped at four; pending is never evidence of complete history.

Reconciliation enriches sender names from the existing browser cache for the
exact senders in the bounded message page. It does not fetch the contact book or
merge phone/LID identities. Live captures remain durable immediately; missing
names can be filled during reconciliation. See the upstream
[contact fields](https://docs.wwebjs.dev/Contact.html).


## Startup and memory checks (0.7.7–0.7.9)

Provider injection retries only known navigation context loss in the same browser.
Page reads have a Node-side deadline, optional name lookup is bounded, and persisted
last-attempt ordering prevents one failing chat from starving later chats. Cursors
advance only after successful ingest. Media downloads accept fixed-host /v/, /o1/v/
and /o2/v/ paths and still verify ciphertext/MAC/plaintext hashes.

Pairing now waits for initialize and identity verification, confirms a clean Chromium
process exit, then opens the same LocalAuth profile again without allowing another
QR. Success requires both clean closes and the restored identity. The existing
Python 300-second supervisor bounds the entire attempt. No session-saved event or
arbitrary delay is assumed.

Pairing explicitly closes its owned Puppeteer browser before auth-strategy cleanup.
whatsapp-web.js 1.34.7 otherwise skips that close because Puppeteer 25 removed
`isConnected()`. The process-exit check remains mandatory: a timeout, failed close
or abnormal exit cannot start another client or report successful pairing.

The Compose WhatsApp memory cap is 2560 MiB per account. A live test proved that
a 1536 MiB cap could kill the Chromium renderer while Docker OOMKilled remained
false for the container. Inspect kernel/cgroup OOM counters as well as container
status, and size total host memory for every enabled account. This cap does not
prove stable operation for every history size.


## Shutdown during startup (0.7.9)

The stop waiter is active while the provider connects. Shutdown cancels a pending connect, disconnects the owned client, and releases the fenced lease. Timeout and lost-lease errors still propagate. Regression tests cover all three paths; production shutdown must still be verified against the actual browser process.


## Bounded Telegram window (0.8.0 / schema 11)

Operator config `history_since` is an ISO timestamp with a mandatory timezone, or null for unbounded history. It is the lower bound of the initial load, not a data deletion period: saved messages and files remain, and new events continue to be accepted. All initial/backfill/reconcile pages are filtered by message time. Old media jobs that are already queued stay in the queue but are not picked by the collector while the bound is in effect. `history_since` does not apply to WhatsApp.

`max_dialogs: null` enables a full pass over the available list through Telethon pagination; an integer stays an explicit operator limit. Before the first history load the catalog is saved in `chats` and `collector_chats`, so an empty chat history does not mean the chat is absent. The include/exclude policy applies to the catalog too. A repeated pass picks the attempts not made for a long time; an attempt is saved before the API request, so an error/restart does not make it start from the same chat again.

`window_complete` means the set time bound was reached; `history_exhausted` is the end of the provider's available pages. These are different flags. Changing `history_since` resets only the first; the boundary of the last page is kept so that extending the period does not lose the part of a page that was cut off. Neither flag proves the completeness of old edits/deletions or the availability of all attachments. Discovery prints only the aggregated number of dialogs, the limit and history_since, with no names or messages.

This is not a new general rate governor: the existing FloodWait handling is kept. A shared limit on all API/media requests remains separate work. The targeted expansion of a single chat on request is `request_chat_history` (MESSENGER_OPERATIONS.md).

## Deleted messages (schema 45, ANYM-511)

A deletion a collector reports (WhatsApp delete for everyone, a Telegram deletion it
receives) is a tombstone, as before. The `message-purge` worker
(`deploy/compose.message-purge.yaml`, role `messaging_purge`) then clears the message's
text, file names, file text, transcripts, AI output, review references, send and command
text within minutes and keeps a content-free `message_purges` mark. Collectors need no
change: guard triggers keep a later import or backfill of a purged message content-free,
its ingest receipt included. The worker runs on its own login `messaging_purge_runtime`. It
takes `purge:<account>` and `archive:<account>` with try-locks, at most 50 messages per
transaction (one after a failure), a 2 s lock and statement bound and a 3 s transaction bound
(PostgreSQL ends the session there), so ingest, sends and commands behind it wait at most
3 s; a busy account is retried on the next pass. Messages deleted before schema 45 are
purged by the operator: `python -m messaging_service.message_purge --dsn-file <dsn> --role
messaging_purge purge-deleted --preview`, then `--apply` after a fresh verified backup;
`--apply` exits non-zero while marks stay pending.
`get_sync_status` shows `deletions` per account (guarantee, pending, failed, last purge);
`purge-status` lists failed message IDs. Telegram's guarantee stays `best_effort`: it does
not report every deletion.
