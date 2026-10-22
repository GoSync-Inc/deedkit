# Media storage admission

## Managed original retention (schema 33–35)

The separate lifecycle worker can expire **processed, unpinned originals after
30 days without an original request**. Search and reading retained text do not
refresh that clock. An authorized original request does. Existing originals get
a full 30-day grace period when residency tracking is first installed; message
age alone never authorizes immediate deletion.

Since schema 35 no original is deleted without a **verified cold copy** in the
private R2 bucket (see [Cold archive](#cold-archive-of-originals-schema-35)).
Every production entry point of the lifecycle worker enforces this; a candidate
without one is reported `cold_copy_missing` and stays on disk.

Residency belongs to shared SHA-256 bytes, while descriptions and source links
belong to individual occurrences. An operator pin protects all occurrences of
the same bytes. Eviction retains messages, filenames, titles, summaries,
extracted detail, ASR text and timestamp segments. Every active occurrence must
have usable current processing evidence. Unprocessed, stale, failed or
incompletely extracted/transcribed files are retained. A video description is
explicitly based on sampled frames and does not claim a complete transcript.

An operator who accepts losing originals without retained descriptions can add
`--expire-unprocessed` explicitly. This allows the same journaled eviction without
requiring extraction, ASR or descriptions. Every active occurrence of the shared
bytes must also have a message older than 30 days. Pins, the 30-day original-access
clock, active requests/grants/processing, backup fences, integrity checks and pass
limits still apply. Existing messages and any derived text remain unchanged;
missing descriptions are reported honestly. The original can be requested through
the existing bounded provider queue; provider availability is not guaranteed.
Without this flag, usable complete current processing remains required.

Extraction and original-dependent enrichment pause while a matching managed
eviction journal is prepared/completed. They preserve processing attempts,
budgets and existing text until verified re-publication makes the original
resident again. Unknown missing or corrupt originals keep their normal failure
diagnostics. Install the updated indexer/enrichment SELECT grants with the release;
these roles gain no residency or journal mutation permission.

The flag does not shorten the migration's initial access grace period. If the
owner explicitly chooses immediate expiry of historical originals, adopt their
historical age as a separate one-time operation during the initial maintenance
window: retain the fresh compatible restore proof, record the exact residency
preimage and latest shared-source timestamps, quiesce original readers and
publishers, and update only unchanged unpinned resident entries under the
lifecycle/source locks. Reject active grants, requests, processing and backup
fences. Never backdate a real original access or a new publication. Preserve
this audit on the host, then inspect the bounded preview before applying. Do
not edit migration 033 or delete directly from the original directory.

Destructive retention is opt-in and defaults to a dry run. Each pass has file
and byte limits. It rechecks pins, recent access, active downloads/grants,
processing and durable backup fences, verifies the exact regular file's size
and SHA-256, and journals the transition before unlinking. A failed or
interrupted transition is reconciled by the lifecycle worker; it never scans
and deletes arbitrary files, Docker volumes or other projects' data.

Only a completed managed eviction allows authorized file search and retained
text/segment reads while the original is absent. Unexpected missing bytes
retain their existing unavailable/corruption behavior. Metadata identifies
`original_evicted` separately and suggests `request_file_download`. Recovery
uses the existing bounded collector queue and checks the retained digest. It
does not guarantee that Telegram or WhatsApp can supply an old original.
Failed/exhausted requests, ACLs, provider cooldowns and storage limits still
apply. An original evicted under schema 35 also has a verified encrypted copy in
R2, and `request_file_download` restores it from that copy first
([Cold-first rehydration](#cold-first-rehydration-anym-350)); the provider queue
is the second source.

Backup fences are durable, without automatic expiry, from capture through
confirmed delivery. Pending captures from older runtimes must be reconciled
before enabling retention; the new worker cannot assume they are fenced.
Do not clear a fence while any retry still references its originals.
See [backup streaming](BACKUP_STREAMING.md) for replacement verification and
[the backlog](BACKLOG.md) for source versus live status.

### Activation and operator controls

Apply schemas 033/034 and their role grants through a reviewed release from the
exact verified main revision, after a fresh compatible backup has been restored
independently. Schema 035 (cold-copy catalog) follows the same rule and the
cold-archive activation below. Keep migrations 001..032 unchanged. Reconcile all older pending
backup jobs and snapshots before enabling retention; do not release a durable
fence merely because it is old. The receiver must retain a verified protected
anchor while its replacement is captured, delivered, decrypted and restored.

Install `file_lifecycle_role.sql` as the trusted migration owner and grant its
NOLOGIN role to a dedicated runtime login. Store that login's DSN in the private
`file-lifecycle.dsn` file. The runtime cannot edit source identities or remove
backup fences. `deploy/compose.file-lifecycle.yaml` is an optional overlay; its
profile starts with dry-run reports and bounded daily passes. Inspect the bounded
preview and blocked-reason report before explicitly adding `--apply` to the
reviewed runtime command. Repeated previews inspect the same first batch; they
do not constitute a full catalog audit. Dry runs never update the access clock
or traversal cursor. Apply passes advance a separate inspection cursor so blocked
old files cannot starve eligible files later in the catalog.

For an individual CAS digest, use the same restricted operator runtime:

```sh
python -m messaging_service.file_lifecycle --dsn-file /run/secrets/database_url --files /data/files --digest SHA256 --pin
python -m messaging_service.file_lifecycle --dsn-file /run/secrets/database_url --files /data/files --digest SHA256 --unpin
```

Install the independent understanding role/configuration separately, following
[ENRICHMENT.md](ENRICHMENT.md). The optional
`deploy/compose.file-understanding.yaml` overlay does not start a model daemon or
download weights. A local endpoint must be reachable in the worker's permitted
network namespace; container loopback refers to that container. Text/vision
profiles and their processing limits require explicit configuration. Existing
ASR results remain retained when model profiles change. Originals without usable
current processing evidence continue to occupy disk; enabling TTL alone cannot
reclaim a backlog of unprocessed videos under the default policy. The explicit
`--expire-unprocessed` policy can reclaim old originals without that evidence.

### Cold archive of originals (schema 35)

`messaging_service.cold_archive` copies each idle original (default: 23 days
without an original request, so a week before it can be due for eviction) to
`<prefix>/cold/<sha256>.age` in the existing private R2 backup bucket, then
reads it back. A copy is recorded `verified` only when the object downloaded
from R2 has the ciphertext size and SHA-256 recorded before its upload was
committed, and decrypts to exactly the original's SHA-256 and size. Upload
success alone never counts. A digest shared by many occurrences is archived
once. The pass never deletes anything, here or in R2.

- **Access.** The producer has no R2 credential. The archiver reaches R2 only
  through the receiver's `cold` SSH forced command, which stores or reads one
  object named by a digest and cannot list, delete or overwrite
  ([BACKUP_R2.md](BACKUP_R2.md#cold-archive-of-originals)).
- **Encryption.** Each object is age ciphertext to two recipients: the existing
  backup recovery recipient, so the offline backup key can always restore it,
  and a dedicated cold recipient. Only the cold identity is placed on the
  producer, as a private (`0600`), single-link regular file owned by the
  archiver's runtime user (UID 10001 in the overlay), outside Git, images and
  logs. Plaintext is read from the CAS and never staged; reports carry no
  paths, keys or contents.
- **Existing objects.** An object is never replaced. One already in R2 counts
  only when it is the ciphertext this archiver recorded, for the current
  recipients, before the receiver could commit it; decrypting with the cold
  key alone cannot show that the backup recovery key also opens it. Otherwise
  the copy is `failed` with `cold_object_unattested` and the original stays
  resident until an operator reconciles it.
- **Budget.** `total_bytes` caps the bytes of every copy that may exist in R2:
  verified and pending copies, and failed ones whose ciphertext was already
  offered for commit. `pass_files`, `pass_bytes` and `max_object_bytes` bound
  each pass; failed copies are retried after new candidates. A candidate over
  any limit is reported `cold_budget` and is not uploaded.
- **Failure modes.** A receiver, network or R2 error is `cold_unavailable` and
  ends the pass; a read-back that does not prove the original is
  `cold_verify_failed`. In every case the original stays resident, and the
  eviction pass reports it `cold_copy_missing`. A full disk is the visible
  result of a stopped archive, never data loss. A dry run uploads nothing.
- **Status.** `--status` prints copy counts and bytes by state, failure codes
  and the number of due originals without a verified copy.
- **Manual retrieval.** Outside the request path, an operator can send the
  line `{"v":1,"op":"get","digest":"<sha256>"}` over the cold SSH key. The reply
  is one JSON line with `bytes`, followed by exactly that many ciphertext
  bytes. Decrypt them with the cold or the backup recovery identity, and check
  the SHA-256 against the digest before using the original.
- **Cost.** As checked on 2026-10-16, Cloudflare R2 Standard lists
  $0.015/GB-month, and the 10 GB account allowance is already used by backups:
  about 16 GB of eviction candidates cost about $0.24/month. Each original
  needs one HEAD, one write (a few part requests above 64 MiB) and one
  read-back GET, well within the monthly free request allowances. R2
  Infrequent Access has retrieval charges and a 30-day minimum and is not
  used. See [R2 pricing](https://developers.cloudflare.com/r2/pricing/).

Activation: install `cold_archive_role.sql` as the migration owner and grant
its NOLOGIN role to a dedicated runtime login (`private/cold-archive.dsn`); it
can record copies but cannot change residency, journals or fences. Re-run
`file_lifecycle_role.sql`, which grants the eviction role read-only access to
the copy catalog. Copy [the example](deploy/cold-archive.example.json) to
`private/cold-archive.json`, place the cold identity at
`private/cold-archive-identity` and the forced-command key plus pinned
`known_hosts` under `private/cold-archive-ssh/`. Enable the `cold-archive`
profile of `deploy/compose.file-lifecycle.yaml` in dry run, inspect the report,
then add `--apply`. Keep eviction in dry run until the cold-archive status shows
verified copies for the due originals. The overlay mounts the CAS read-only.

### Cold-first rehydration (ANYM-350)

An authorized `request_file_download` for a managed-evicted original whose
copy is `verified` queues a cold restore instead of a collector job and answers
`rehydration_source: cold_archive`. Repeated requests coalesce on the queued
restore (`poll_after_seconds: 10`); at most 128 restores (`media_request_limit`)
are outstanding. A request whose provider fallback would be admitted counts
against the provider demand quotas (32 per key, 128 global) at once, so a
fallback never exceeds them. The `cold-restore` service of the same profile
polls every 10 seconds: it fetches
the object through the receiver's `cold get`, checks the recorded ciphertext
size and SHA-256, decrypts with the cold identity and streams the plaintext only
into `BlobWriter` with the expected SHA-256 and size. Only then is the original
published and re-registered resident; text, ASR and descriptions are untouched.

- **Capacity.** Before any R2 read the worker takes the collectors' storage
  admission lock and requires free space minus their media reservations and
  pending uploads, minus the object, to stay above the larger of
  `restore_disk_reserve_bytes` (default and minimum 10 GiB) and the highest
  floor any collector publishes for that volume. Each collector publishes the
  max of its media, live-media and capture-spool floors per volume in
  `collector_health.operations.disk_floors`; the cold role reads that column
  (`cold_archive_role.sql`, which also exposes the rest of the operations JSON,
  including the identity's self id). An object over `max_object_bytes` is
  refused the same way. In the same transaction the admitted restore leases its
  bytes: a copy whose `restore_finished_at` lies in the future (one transfer
  timeout ahead) holds a lease, and the gateway's 15-minute provider hand-over
  keeps that time, so a stalled restore still counts until its lease ends.
  Collector admission and file uploads count every live lease once their role
  may read `attachment_cold_copies` (`collector_role.sql`). The lease ends with
  the restore and lapses by itself after a crash; `BlobWriter` stops the
  restore at the same floor and when the lease ends, which fails the restore
  over to the provider.

- **Fallback.** A missing, different or truncated object or an R2 or receiver
  error publishes nothing, records `restore_error` on the copy and queues the
  provider re-download while the request's demand window is open. The next
  response reports `rehydration_source: provider` and `cold_failure: <code>`.
  When the provider path refused the request (no fallback was reserved), the
  answer carries the provider's refusal with `rehydration_source: none`. An object that
  does not match its record also turns the copy `failed`, so it never
  authorizes another eviction or restore until the archiver proves it again.
  An R2 outage leaves the copy `verified`; a later request tries R2 again,
  also after the provider job failed or ran out of attempts.
- **Local limits.** `disk_reserve` and `cold_size_limit` queue nothing at the
  provider, which would write to the same disk. For 5 minutes a request answers
  `request_status: unavailable` with that reason and `retry_after_seconds`;
  afterwards a request tries R2 again.
- **Worker presence.** The worker holds a session advisory lock while it runs;
  a second worker claims nothing. Without a worker a request goes to the
  provider at once (`cold_failure: cold_worker_unavailable`), and a gateway that
  lacks the restore grant reports `cold_failure: cold_first_inactive`.
- **Stall.** A restore the worker has not finished within 15 minutes goes to the
  provider on the next request
  (`cold_failure: cold_restore_stalled`). Each worker step is conditioned on the
  request generation it claimed, so a late result never overwrites a newer
  request or the fallback: a late success makes the original resident and
  leaves the provider hand-over standing.
- **Interrupted publication.** A verified original already in the CAS whose
  registration did not commit is registered resident by the next worker pass,
  whatever the request's age and without reading R2 again. Each pass walks all
  candidates in pages; it changes residency only, and a file that cannot be
  read is skipped (`reconcile_skipped`) without holding up due restores.
- **Unchanged.** An original evicted without a verified copy, and a gateway
  whose role lacks the restore grant, keep the provider path. The demand window
  opens only when the provider path itself would accept the request (collection
  policy, attempt cap, cooldown, history bounds); otherwise a cold restore still
  runs, without a provider fallback.
- **Activation.** In this order: re-run `cold_archive_role.sql` (read the
  eviction journal, `media_reservations` and `file_uploads`; execute
  `register_resident_original` and `queue_managed_original`), start
  `cold-restore`, then re-run `gateway_role.sql` (SELECT on `attachment_cold_copies` and UPDATE of its
  `restore_*` columns). The `cold-restore` service mounts the CAS writable and
  reuses the archiver's DSN, configuration, identity and SSH key.
- **Restore drill.** Restore a backup point into an isolated database and files
  directory ([BACKUP_R2.md](BACKUP_R2.md#cold-archive-of-originals)), start
  `python -m messaging_service.cold_archive --restore --interval-seconds 10
  --config <config> --dsn-file <isolated dsn> --files <isolated files>`, then
  request one evicted original with a key issued in the restored database; the original's SHA-256 must equal its digest in `absent_blobs`.
  `tests/test_cold_archive.py::test_restore_drill_rehydrates_from_cold` proves
  this with a real `pg_dump`/`pg_restore` into a second database and a fake
  `cold/`.

## Download admission and prefetch

Telegram and WhatsApp collector configurations use `media_prefetch_days: 7` by
default: automatically download originals from the rolling last seven days.
Set an integer from 1 to 36500 for another window, `0` for explicit requests
only, or `null` to prefetch all available history. This changes original-file
selection only; it never truncates captured messages, deletes stored originals,
or changes Telegram's separately configured `history_since` hard boundary.
Direct embedded `Archive.claim_media` calls retain their legacy unbounded
default; live collector entry points always pass the validated configuration.

An unexpired explicit original request is selected ahead of automatic prefetch,
including outside its rolling window. It does not override history bounds,
collection/account/chat access, deleted or superseded revisions, the attempt cap,
provider cooldown, file-size limits or storage admission. Outside-window jobs
remain queued without consuming a provider attempt. Requests do not reset failed
or exhausted jobs; originals may no longer be available from the provider.

Such dormant jobs cost an idle collector nothing per poll. With an integer
window, a claim reads one fresh database time after its lock waits and looks
only at candidates that can be claimable then: unexpired requests through the
`media_requested` index, and messages from the window (also bounded by
`history_since` and the five-minute future limit) through `message_scope_date`,
then their jobs through `media_message`. Its work grows with requests and
recent messages, never with the dormant backlog. `null` keeps the full history
scan by design. The retry-exhausted sweep, which marks an expired fifth lease
`failed`, has no selective index: each collector runs it on its first claim for
an account and then on the first claim at least 60 seconds after the previous
sweep, so the stored row lags by at most 60 seconds plus one claim cycle (about
one second on an idle collector). Attachment status and media requests report
such a lease as `retry_exhausted`/`failed` meanwhile; the operator status media
phase still counts it as a lease until the sweep. Expired storage reservations
are still reclaimed on every claim. `tests/test_media_claim_performance.py`
checks the plans with 30,000 and 60,000 dormant jobs.

Claims run in this order: explicit requests, then live originals (newest first), then
voice notes (`audio/ogg`, `audio/opus`, at most 32 MiB), then other files of at most
1 MiB, then everything else. Within each group the newest message comes first. The
Telegram collector also backfills dormant out-of-window jobs on its own
(`media_backfill`, default `true`). This covers voice notes and files of at most 1 MiB
from a bounded window (`media_prefetch_days` from 1 to 36500), inside `history_since`.
Backfill runs only when no window job is due, takes at most one original every 15
seconds, and waits 10 minutes once nothing is left. Each claim first reads at most 16
candidates without the storage locks and then checks them again under those locks.
A backfilled original whose lease expired is claimed again by backfill. Requests keep
their own due order. Larger dormant originals still wait for an explicit request. Set `media_backfill: false`
to turn backfill off. WhatsApp collectors never backfill.

Message capture and original downloads have separate disk budgets. Collector
`disk_reserve_bytes` retains the capture/spool floor (default 1 GiB).
`media_disk_reserve_bytes` controls the original-file floor (default the larger
of 10 GiB and the existing capture floor),
must be at least the capture floor, and can be configured independently. A
storage pause never changes message coverage into a successful history result.

Telegram and WhatsApp can opt into a separate floor for recent small originals with
`media_live_disk_reserve_bytes`. It is disabled by default (`null`), must remain
at least the capture floor, and cannot exceed the background media floor.
`media_live_max_bytes` defaults to the smaller of 32 MiB and `max_media_bytes`;
`media_live_window_seconds` defaults to 24 hours (allowed: 10 minutes to 7 days).
Only known-size originals within both limits qualify, based on message time;
unknown-size, older and larger originals retain the background budget. Future
timestamps more than five minutes ahead cannot enter this tier. History bounds
and account/chat exclusions apply to both tiers.

After explicit requests, eligible recent originals are claimed before background
history, newest first. An older or larger explicit request still uses the
background floor; requesting a file never silently lowers its storage budget.
Both tiers share the same CAS, atomic reservations and lease fences. The chosen
floor also applies during streaming, rather than only before download. This
policy keeps small fresh originals available during a background storage pause;
it does not implement cold-file eviction, guarantee latency during provider
rate limits or interrupt a transfer already in progress. Under sustained recent
traffic, history can wait. Extraction/transcription remains a separate queue:
agents can obtain a downloaded original before its analysis is ready.

Before starting a provider download, live collectors pass their `BlobWriter` to
`Archive.claim_media`. One PostgreSQL advisory transaction serializes all media
admissions in the archive DB. Filesystem device keys group aliases/bind mounts;
workers for one shared volume must use the same device identity in their runtime.
The lease reserves the declared original size plus 1 MiB streaming workspace.
Unknown sizes reserve the full configured `max_media_bytes` quota. Known files
larger than this limit fail before any provider request. Current Telegram and
WhatsApp paths stream directly into CAS; they do not stage another full-size
provider file. A future adapter that stages complete temporary originals must
budget its additional workspace before admission.

The calculation subtracts **full active reservations**, even if their writers
have already consumed some disk space. It is conservative, not a promise of
exact available capacity. It cannot protect against unrelated software filling
the same disk: the writer rechecks the media floor on every chunk and cleans up
its own pending file after interruption. No original is deleted or evicted by
this feature. A writer killed outright (SIGKILL, power loss) leaves a
`.pending-*` file; collectors remove such top-level files untouched for a day
at startup. If a stored original fails verification, the next verified download
of the same digest replaces it instead of failing with `blob_corrupt`.

Telegram original transfers get a deadline sized for paced 512 KiB requests
(at least one hour, at most six), so files above about 1.7 GiB can finish.

If there is insufficient capacity, the job stays `queued` with
`error_code=paused_storage`, retries after 60 seconds, and does not increment
provider attempts. A transfer interrupted by the local disk reserve returns to
the same state and rolls back its claimed attempt. Per-account/per-chat sync
diagnostics expose the reason; account media operations also expose
`storage_paused_jobs`. Terminal failures and queued retries have independent
counts and state-specific diagnostics: a terminal network failure never promises
another automatic retry. `needs_attention` remains true when other jobs are
simultaneously paused for storage. Other smaller eligible jobs can proceed on later passes.
Normal capture and metadata/text search remain independent of this pause.

### Which queued originals count as pending (schema 35, no migration)

A recorded `error_code` is the reason of the job's last attempt, not a current
diagnosis: a job paused while its message was in the window keeps
`paused_storage` after the message ages out, and is then no longer claimed. Each
collector run therefore publishes the window and history bound it claims with in
`collector_health.operations.media_policy` (`prefetch_days`, `history_since`,
`epoch`). Sync status uses that record only while its `epoch` equals the account's
collector lease epoch, so a run that could not publish never inherits an earlier
run's window.

With a current record, account media operations report
`storage_paused_basis: "eligible_jobs"` and `prefetch_window_days`, and classify
each queued original without a registered original exactly once:

- **eligible**: an unexpired explicit request, or a message inside the window
  (`null` window: any message), in both cases not before the run's history bound.
  `eligible_queued_jobs` counts them. `storage_paused_jobs`,
  `storage_unavailable_jobs`, `retry_queued_jobs` and the queued `diagnostics`
  count eligible jobs only, by their recorded code. Media `state` is
  `paused_storage` or `queued` only because of eligible jobs.
- **`outside_prefetch_window_jobs`**: known occurrences that wait for an explicit
  `request_file_download`, whatever code they recorded earlier.
- **`outside_history_bounds_jobs`**: messages older than the chat's confirmed
  history bound; a request does not download them.
- **`history_bound_unknown_jobs`**: messages older than the run's published bound
  in a chat whose bound is not confirmed (see below); the run does not download
  them, and a request cannot be promised.
- **`collection_excluded_jobs`**: collection disabled for the account or chat.

`errors` keeps every recorded code as evidence, and `download` counts are
unchanged (each occurrence in exactly one download state). Without a current
record (a collector from before this change, or a run that has not published yet),
status keeps the recorded-code counting and reports `storage_paused_basis:
"unknown"` with the new counters `null`.

A chat's history bound is **confirmed** only when its `collector_chats.history_since`
equals the bound the current run published; a `NULL` equal to a published `null`
is a confirmed unbounded history. A row a live callback created without a bound, a
row from an earlier configuration, or a missing row is unknown evidence and never
yields `outside_history_bounds`.

Attachments follow the same rule, but only with a confirmed chat bound. A queued
original of an excluded chat or a disabled account has `download_phase: blocked`,
reason `collection_excluded` and next action `check_collection_policy`; a
managed-evicted original there has the same reason and action instead of
`request_file_download`. A queued
original outside the window has
`download_phase: available_on_request`, reason `outside_prefetch_window` and next
action `request_file_download` (conversation text: "файл доступен по запросу"),
not `blocked`. Before a confirmed bound, an original that needs a provider download
or re-download (including a managed-evicted one) has reason
`outside_history_bounds` and no `request_file_download` action; its request returns
`request_status: unavailable` with that reason and records no `requested_until`. A
present original stays `ready` regardless of the bound. Access, deletion,
active-transfer and terminal-failure answers keep their precedence. With an
unknown bound (or no current record) the attachment and every request for it,
including a repeated (`coalesced`) one, report `download_eligibility: unknown` and
`eligibility_reason: history_bound_unknown`; the first request is still recorded
(`request_status: queued`) and nothing is advertised as available on request.

A local write failure that is not a capacity limit (missing directory,
permissions, read-only mount, or the native adapter's temporary media directory)
is `error_code=storage_unavailable`, not `network_error`. Like a capacity pause it
does not consume a provider attempt and never fails the job terminally; it
retries every 5 minutes, and account media operations report
`state=storage_unavailable` and `storage_unavailable_jobs` (eligible jobs only, as
described above: an aged-out job resumes only on request). Fix the storage path,
then downloads resume on their own. Jobs recorded as `network_error` before this
classification keep their recorded code.

Media reservation expiry follows the media lease and is renewed with it.
Expiry, edit/delete supersession, successful completion and failure release
capacity. Cleanup is idempotent and matches the exact lease epoch, so an old
worker cannot release a replacement worker's reservation. The streaming guard
rejects writing after local lease expiry or beyond the admitted payload quota.
Current message deletion and current collection include/exclude policy are
checked before a job is claimed and when its lease is used.

Migration 21 adds `media_reservations`. Operator migration, collector DML grants
(`SELECT, INSERT, UPDATE, DELETE`) and backup table inventory must be updated
together. These transient capacity leases are not a durable blob residency or
cold-storage catalog. Trusted offline replay may use `claim_media` without a
writer for compatibility; live collectors always provide one. Gateway/MCP has
no direct media claim capability.

Migration 31 adds bounded download demand. Call `get_attachment(attachment_id)`
to inspect an existing occurrence without changing queues. If it is queued,
`request_file_download(attachment_id)` (gateway: `messages_request_file_download`)
requests priority for one hour. The caller needs the existing `download` right;
it cannot supply a URL, server path or provider descriptor. Duplicate requests
coalesce without extending that hour. Outstanding demand is capped at 32 per
credential and 128 globally; `media_request_limit` means to let earlier requests
finish. Poll `get_attachment` after the returned interval; requesting priority
does not promise completion or bypass any collection, retry or storage limit.
If a provider cooldown outlasts the one-hour request window, the response is
`deferred` with `next_due`/`retry_after_seconds`; no demand lease is created.
Request again after that time instead of repeatedly polling or resetting backoff.
`get_file_text` on such a known but undownloaded attachment returns no text and
`empty_reason: attachment_not_downloaded` with the same download status, reason and
`next_action` as `get_attachment`; the read itself fetches nothing. After
`request_file_download`, a voice note is transcribed automatically (ANYM-455).

Pending responses retain the full declared name, MIME, size, source message and
download reason. Download, text extraction and enrichment have independent
statuses. `get_attachment` verifies local presence before issuing a temporary
link; an absent registered original returns `original_missing`, without resetting
the download receipt or discarding the recorded processing statuses.

The seven-day prefetch window is **not** an eviction TTL. Original expiry is the
separate opt-in 30-idle-day policy described above. It preserves readable
text/analysis, requires the documented processing and safety checks, and since
schema 35 requires a verified cold copy in R2 before any deletion.
Provider history/CDN availability alone is not a durable backup.

Sync status (`get_sync_status`, chat `resolve_reference.sync`, file search evidence)
counts each current file occurrence of a non-deleted message in exactly one
`download` state, using the same rule as each attachment's `download_status`:
a registered original is `ready` even if an edit re-queued its job, a legacy
original without a job is `ready`, and a `ready` job without a registered original
is `unavailable`. `download.ready` therefore equals `stored_files`.
`current_files` sums every state except `superseded` (replaced older revisions);
`download_basis: current_file_occurrences` names this rule. Queued originals
outside the prefetch window are known occurrences that are not stored yet; account
media operations count them in `outside_prefetch_window_jobs`, not as pending work.
