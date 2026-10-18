# Telegram collector: preparing to connect

The owner connects each account in their own terminal. The number of accounts
is not limited to two: compose shows examples of two independent WhatsApp accounts and
one Telegram account. Further accounts need separate configs, sessions,
spools, account IDs and services. Live collection checks apply to a specific
installation and do not prove the completeness of another user's history.

## Design

- Telethon 1.45.0 is pinned by lock files. The new/edit/delete handlers first save the capture to a bounded queue with fsync. An unavailable DB does not hold the queue write lock. A queue overflow or save error stops the collector and requires reconciliation.
- The process holds a local `flock` on the session file and a renewable DB lease. Separate leases protect media jobs; a process that has lost ownership cannot finish an ingest or publish a file. This is not a way to stop an old Mac client remotely: its shutdown and autostart are checked before handover.
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

### Queue quarantine and resilience

One message does not stop an account's collection. Adapters truncate or
neutralize display fields supplied by the sender (MIME with parameters or
control characters, long file names and profile names, NUL in text,
a size over 8 GiB, an overlong `replyToId`) instead of rejecting them. A message
that still fails validation is skipped during history reconciliation and
counted in `invalid_provider_messages` of the `live_capture` status.

Telegram live capture follows the same rule. A live message, deletion, read
receipt or owner read that the spool validator rejects, or whose receipt or
read parser fails, is not written and does not stop the collector: a restart
would meet the same input again. `live_capture.rejected_live_captures` counts
these items by content-free reason code (for example `invalid_deleted_ids`,
`invalid_provider_message`, `receipt_parse_failed`, `read_parse_failed`; at most
32 codes, later ones count as `invalid_capture`). For a skipped message,
`rejected_live_messages` keeps the chat, message ID, reason and time of the last
20, never content. Reconciliation already re-reads new messages above the chat
checkpoint and the newest page of each chat; a message that is still invalid
there is skipped and counted again, never stopping the collector. An older
message is not re-read automatically; its recorded ID lets an operator fetch it
again once a fix accepts it. A deletion with more than 1,000 IDs is written as
several `telegram_delete` records of at most 1,000 IDs.

Capacity and I/O still fail closed. `spool_full`, `disk_reserve`, an I/O error
while writing, or any unexpected error stops the collector with
`durable_capture_failed`; nothing is acknowledged before it is durable, and
reconciliation after the restart covers the gap. The cause is kept as a fixed
detail (`spool_full`, `disk_reserve`, `spool_io_error` or `capture_error`) in
`live_capture.last_capture_stop` and in the supervisor line
`{"status":"stopped","code":"durable_capture_failed","detail":"disk_reserve"}`.
Exception text never leaves the process.

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
old head is reported as a gap. A linked device's exhausted history is always
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

This is not a new general rate governor: the existing FloodWait handling is kept. A shared limit on all API/media requests and a client interface for the targeted expansion of a single chat remain separate work.
