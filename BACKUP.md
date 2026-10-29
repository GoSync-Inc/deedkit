# Messaging backup and restore

An operator mechanism, not available through MCP/REST. It does not preserve the personal Core,
other applications or provider history that has not been received yet. The automatic job,
encrypted delivery to a second VM and retention are configured. The receiver VM is reachable
only over the private tailnet and relays to object storage at a separate provider. The owner keeps
an offline copy of the recovery key outside every provider account: when the only copy lived on
the receiver, losing that VM (2026-10-22) left every earlier backup undecryptable.

## Contents and consistency

`snapshot` without `--recovery-config` keeps the previous format 1: a consistent PG dump,
a manifest of all tables of this version and all referenced immutable originals.
With a private recovery config it creates format 2: additionally settings, cold
provider sessions and the durable spool. Absolute source paths are not written to the manifest;
inside the encrypted tar, state files are named by SHA256.

Before a full snapshot the operator cleanly stops **only the running collectors**
of their own deployment. After the snapshot only those are resumed. The API and database stay available.
A running provider's SQLite/LevelDB must not be copied as an ordinary directory.
The code takes the same session flocks, checks that no DB lease is active and that
a session+spool exists for every real account, including temporarily disabled ones.
The lock file must exist after pairing/first start; backup opens it
read-only and never creates it as root. The cold lock is released after the state
and the PG dump are copied; immutable originals are copied and encrypted after the
collectors resume. `captured_at` shows the time of the DB snapshot, not of the end of delivery.
The completeness of the account list is checked again inside the exported PG snapshot.
The lock also applies to pairing. An external client that ignores these locks
breaks the single-owner condition and must be stopped separately.

In the configuration below, paths refer to the filesystem of the **backup process**. When
running in a container the operator mounts the needed directories explicitly. `session_path` must
match exactly the file/directory the collector uses for its session lock.
Each additional account gets its own entry and its own sources. Sources
must not overlap or contain the destination directory.

```json
{
  "accounts": [
    {"account": "telegram-primary", "session_path": "/state/telegram/telegram.session"}
  ],
  "sources": [
    {"name": "telegram-session", "kind": "session", "account": "telegram-primary", "path": "/state/telegram"},
    {"name": "telegram-spool", "kind": "spool", "account": "telegram-primary", "path": "/spools/telegram"},
    {"name": "settings", "kind": "settings", "account": null, "path": "/settings"}
  ]
}
```

The config file has mode 0600. Settings may be a single file or a directory;
include the needed account config, permits, DB/bootstrap settings and the pinned
code/compose for recovery. The private age key is stored separately from the source.
An empty accounts list is allowed only when the DB has no real accounts;
such a snapshot is explicitly not declared to contain provider sessions.

```sh
python -m messaging_service snapshot --snapshot-dir PRIVATE_NEW_DIRECTORY \
  --dump-command '["pg_dump", "-U", "OWNER_ROLE", "-d", "DATABASE"]' \
  --recovery-config PRIVATE_RECOVERY_CONFIG
```

The dump command is a trusted operator argv, without a shell and without a password in arguments.
[`pg_dump --snapshot`](https://www.postgresql.org/docs/18/app-pgdump.html) is used.
The snapshot checks for files changing during the read, SHA256/size, free disk,
and the limits of 100000 state items and 20GiB. Symlinks, hardlinks, FIFOs and other special
files are rejected. If Chromium Singleton links remain after a crash, first
check that the profile has stopped; backup neither deletes them nor ignores the error.

`backup_crypto.encrypt_snapshot` verifies the whole bundle and encrypts it as a stream with the pinned
[age 1.3.2](https://github.com/FiloSottile/age/releases/tag/v1.3.2). The final ciphertext appears only after
successful completion. The encrypted file is what is transferred to the target storage;
its presence on the same VPS does not count as an independent backup.

Since 0.9.4 the operator worker enables `consume_staged_blobs=True`: after the
full snapshot check it deletes only its own temporary copy of each
original right after it has been read into the tar/age. The ciphertext takes the freed space,
so two full copies of the originals do not have to be held at once. The source volume is
mounted read-only. On failure the staging may already be partly consumed:
the runner deletes only that job's directory and starts a new capture. A capture interrupted before
delivery (timeout, SIGTERM, Docker error) is deleted entirely, including a
partial `.encrypt-*` and an unsent `bundle.tar.age`; directories with `delivery.json`
and a package waiting for redelivery are left alone. Directories of interrupted jobs
left over from versions before this fix are not deleted by the runner: the operator removes
by hand only directories without `delivery.json` whose ID is not listed in `journal.json`. A manual call to
`encrypt_snapshot` keeps the snapshot by default; the opt-in accepts only a bool
and rejects hardlinks in staging before encryption starts.

Free space must be enough for the full snapshot, the largest original,
metadata and downloads that continue after the cold capture. The receiver additionally
needs space for the new ciphertext **before** the old confirmed copies are deleted.
Before the archive grows, the operator explicitly raises `--max-bytes`/`--quota-bytes` in the
restricted forced command, keeping the disk reserve and the ban on shell/read/delete.
To restore a package larger than 20GiB, explicitly set a suitable `decrypt_snapshot(max_bytes=...)`
and check the free space in quarantine. The decryption key is not moved to the source VM.

For local ASR, recovery settings must include the profile, its separate DSN,
the compose overlays and the pinned model revision/SHA256. The model can be downloaded again
by provenance instead of copying the weights into every backup. `pg_dump --no-owner --no-acl`
does not preserve global PostgreSQL roles: the operator separately creates the restricted
ASR login/grants and agrees the DSN **before** starting the worker in the restored environment.

## Restore drill

1. Decrypt into a **new** private directory with `decrypt_snapshot`. It checks
   age authentication, allowed tar members, the size limit, and the manifest/dump/blobs/state
   hashes. No `extractall`, symlinks, absolute paths, `..`, duplicates or extra
   unaccounted files. An interrupted restore does not publish the destination directory.
2. Create an empty separate DB with a name ending in `_restore_test`.
   Restore with `pg_restore --exit-on-error --no-owner --no-acl`. Do not mount the
   production DSN and do not connect provider workers or provider networks.
3. `verify-restore` compares all table counts/hashes and referenced file bytes/hash.
   Check **before** a migration or quarantine, which intentionally change data.
4. `quarantine-restore` revokes all old API keys, disables policies, expires
   leases and sets coverage unknown. Principals/consumers remain. An old backup must
   not bring back revoked access.
5. `recovery_state.materialize_recovery(bundle, NEW_QUARANTINE_DIRECTORY)` creates a
   separate `alias/root/...` tree, files 0600, directories 0700. It never writes to the
   source paths and does not start sessions/permits. A stored `enabled=true` inside a
   configuration is historical data: before moving it into a runtime, disable collection and
   run handover/reconciliation again. Send conditions and the outbox, once added,
   must be included in quarantine separately.
6. For an upgrade, compare all immutable data tables before/after and run the migration
   twice. Keep a runnable old version and a verified encrypted pre-upgrade
   snapshot. Clean up only your own restore DBs, plaintext staging and temporary processes.

The checks in `tests/test_recovery.py` use fictional sessions. The transport test
with a stub age checks framing, not cryptography. Real pg_dump/pg_restore,
age encrypt/decrypt and the migration drill are recorded as separate results.

## Automatic run without a Mac

For an archive that no longer fits in the extra plaintext/ciphertext
staging on the source VM, an opt-in `stream_v2` is prepared: [streaming delivery,
compatibility and verification order](BACKUP_STREAMING.md). The old mode below remains the
default; switching requires updating the receiver, host runner and backup image and
checking restore. The formats of stored backup packages do not change.

A separate opt-in [R2 receiver](BACKUP_R2.md) is available for closed object storage:
the same upload-only SSH and `stream_v2`, R2 keys only on the receiver, and an independent
restore before the protected copy is replaced.
An optional root verifier on the independent receiver keeps the recovery key
separate from the upload user and performs restore/rotation without daily Mac involvement;
its configuration and service/timer are described in the [enablement order](BACKUP_R2.md#automated-independent-restore-on-the-receiver).
Installation does not switch the delivery route by itself and does not enable the timer.

`backup_runner.py` is a separate stdlib operator program on the source VM. Its
example configuration is in `deploy/backup-runner.example.json`.
The operator sets up the scheduler (for example a systemd timer) and the cleanup of the
source runner separately; service/timer examples for it are not shipped. The examples
in BACKUP_R2.md concern verification on the receiver and do not start a capture.
This is infrastructure backup; it does not move user Codex routines
and does not send notifications. The configuration is 0600, directories 0700; in the working configuration
the image is pinned by digest. The example with `accounts: []` is meant
only for a stand with no real accounts. Before registering each account,
add its session, spool, credentials/config/permit to the RO mounts/recovery sources.
The coverage check stops the backup if a real account is missing.

On start the job saves a journal and the exact container IDs of the collectors that are already running; the operator chooses the scheduler frequency. The job
stops only those and resumes them after the cold capture. Disabled collectors are not
started. A second process is rejected by a local lock, without interfering with
the work of the first. After a failure the journal lets the existing container continue;
a Docker inspect error is not treated as the container being absent. When setting up the scheduler, provide a call to the runner cleanup after an interruption,
to finish the container owned by the job and restore the state of the collectors.

The `backup` target container has pinned PostgreSQL 18.6/Python 3.13/age 1.3.2.
It has 384MiB/0.75 CPU, a read-only rootfs and sources, is writable only for staging/tmpfs,
and has no Docker socket, SSH key or private decryption key. The only additional
capability `DAC_OVERRIDE` is needed to read the collector UID's private files through RO
mounts. The PG password is passed through a 0600 PGPASSFILE in tmpfs, with no password in argv.
After successful encryption the plaintext staging is deleted before sending.

The worker's `pg_dump` writes the custom format with `--compress=zstd:1`. Restore it with `pg_restore` from
PostgreSQL 18 built with zstd, as the backup image and the receiver's verifier are (or run `pg_restore` in a
`postgres:18` container). zstd in custom format needs PostgreSQL 16 or newer, but PostgreSQL 18 writes archive
version 1.16, which `pg_restore` 16 already refused for the gzip dumps before (`unsupported version (1.16) in
file header`), so on the owner's own machine install the 18 client tools. The dump has its own limit inside the capture: `dump_timeout_seconds` in
`runner.json` (optional, a whole number 300–7200, at most the capture `timeout_seconds`; anything else is
refused as `invalid_backup_timeout`). Without it the limit is 1800 s, or `timeout_seconds` if that is lower,
so every `runner.json` valid before stays valid. A dump past the limit fails the capture as before (the worker
reports `backup_worker_failed`, the fence is released, the collectors resume). Trade-off: a slow dump now keeps
the messenger collectors paused for up to 30 minutes instead of failing the backup at the former fixed 10
minutes. The paused collectors catch up on resume, as after the usual daily pause; a verified daily point is
worth more than a failed run.

Upgrade order: the runner now always writes the limit into `worker.json`, and an older worker refuses keys it
does not know, so a newer runner with an older backup image fails every capture; a newer worker accepts
`worker.json` with or without the key (without it, `pg_dump` keeps 600 s), so an older runner with the newer
image works. The install is the one step of [production.md, Backup runner](docs/operations/production.md#backup-runner),
with the timer and the backup service idle and no capture started until it ends: first set the `runner.json`
`image` pin to the release's backup image and read it back, then install the runner, then add
`dump_timeout_seconds` if wanted. Undo, in the same kind of idle step: remove `dump_timeout_seconds` from
`runner.json` (an older runner refuses unknown keys), install the `.before-<sha>` runner, then put back the
image pin it ran with (the new runner with the old image is the pair that fails).

Schema follow (ANYM-589 B1): an unattended migration whose target the pinned image's `backup.py` does not
read moves the messaging `runner.json` `image` pin itself, before the copy, to the release's prepared backup
image, under the runner lock with both backup services idle, keeping `runner.json.before-<sha>` byte for
byte ([production.md, Unattended migrations](docs/operations/production.md#unattended-migrations), phase 2).
It moves only the image, never the runner: it needs the installed runner to be the release's already.
Undo: under the same lock and idle services, `cp -p runner.json.before-<sha> runner.json`.

Delivery uses a separate SSH key created on the source VM. The key does not give
shell/SFTP/forwarding/read/delete: the target VM runs only
`backup_receiver.py` through [`restrict,from=...,command=...`](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT).
The receiver code, SSH home and authorized_keys belong to root; the closed archive directory belongs to the
unprivileged upload user. The host key is pinned through an already verified admin
channel. Password login and SSH agent are not used. The receiver checks framing,
the age header, size and SHA256, writes with fsync and does not replace an existing ID.
Redelivery of the same ID/hash returns the earlier receipt. It checks
the integrity of the ciphertext but cannot check decryption without the recovery key.

The receiver has no public SSH: its provider firewall has no inbound rules, `from=` in each
authorized_keys line holds the producer's tailnet address, and `runner.json` `transfer.host`
and the pinned `known_hosts` name the receiver's tailnet address. When one receiver serves
several projects, each gets its own upload user, forced-command config, state directory,
recovery key and R2 bucket (R2 tokens are scoped to buckets, not prefixes). Exception: DeedKit's
messaging and family calendar, both the owner's, share one bucket and its token under separate
prefixes (ANYM-607; owner 10-29 «почему у нас не может быть один и тот же токен Р2 для всех
бэкапов»). What that gives up: one leaked receiver token reads and deletes both prefixes; each
prefix's objects stay age-encrypted to its own
recovery key, and the receiver, its rotation and each verifier list and delete only inside their own
prefix (`tests/test_backup_r2_shared_bucket.py`).

On a delivery error the same encrypted package and journal are kept; a retry
does not take a new snapshot and does not stop the collectors again. The error
is saved in `backups/status.json`; the last successful ID/hash/delivery and
`last_snapshot_at` are there too. Staleness must be counted from the snapshot, not from a late retry.
The receiver's own verifier holds the upload lock for a whole restore (about an hour). A delivery it
refuses as `receiver_busy` before it stores anything (stream_v2: before the stream started; staged_v1: the
receiver checks its lock before it stores a byte, though the runner may already have sent part of the bundle
to it) is offered again, same ID, after 60 seconds. A new offer starts only within `busy_wait_seconds` of
the first (optional in `runner.json`, default and maximum 7200, 0 turns the wait off), and each offer keeps
its upload timeout; a wakeup past the bound starts none. Every busy answer prints one line to the unit's
journal (`{"status":"waiting","code":"receiver_busy","id":…,"attempt":n,"retry_seconds":60}`, the last one
`"status":"refused"`); a line that cannot be written is dropped, never the delivery. Any other error, or a busy answer
after the stream started, is not retried. The collectors already run again during that wait, the capture
timeout does not count it, `status.json` stays `uploading`, and the runner lock stays held, so a deploy skips
meanwhile. A SIGTERM during the wait takes the usual stop recovery and keeps the stream_v2 capture. A run lasts
at most capture `timeout_seconds` + the wait + upload `timeout_seconds` (21600 s at the maxima), which must
fit the backup unit's `TimeoutStartSec`. Install a runner that knows this key before adding it, and remove
the key before installing an older runner: a runner refuses keys it does not know.
systemd repeats a failed job after 5 minutes. A separate operator record
`backup-config/last-restore.json` shows the last actual restore check.

The default template keeps one confirmed copy locally. This does not give the
upload key the right to delete remote backups. For one verified copy on the
receiver, use a separate root rotation after an independent restore,
described in [BACKUP_STREAMING.md](BACKUP_STREAMING.md); the earlier shared prune timer
must be disabled or replaced before this policy is enabled. While the
replacement is being verified, both copies are temporarily kept. The old daily rotation mode keeps
the last 24, all copies younger than 24 hours, daily ones for 7 days, weekly ones for 4 weeks and the protected
verified recovery point. The root-owned `protected.json` contains its ID/size/hash;
without it, or if the hash does not match, **nothing is deleted**. The upload key cannot
change this file or start retention. The protected point is unprotected only
by a separate operator decision after the replacement has been verified.

The default receiver is limited to 30GiB/4096 packages, 20GiB per package, with a 1GiB reserve. The operator sets the working values. These are full
copies: sizes and frequency must be reviewed before a large media archive is loaded.
For example, 48 full copies a day would quickly exhaust the quota; the limit gives an explicit error
but does not solve the capacity problem. Measuring growth, moving to incremental/object backup
if needed, an independent provider/account, a separate key copy, delivery of
visible errors through a channel chosen by the owner and a regular restore drill are still
part of M18. A 30-minute frequency does not prove RPO≤1h on live volume; a synthetic
restore likewise does not prove live RTO/session recovery.

### Schema 5: permissions and access log

Since Messaging 0.5 the manifest includes 23 tables, among them `audit_log`,
chat/operation permissions and public key UUIDs. Before migration 005 the pinned
backup image must be updated: 0.4 rejects schema 5. Image 0.5 stays
compatible with backups of schemas 2–4, so it can be installed and verified before
the live DB is updated. When proxy configuration is added, include it in the explicit
list of settings sources; this does not replace a backup of the previous application and its DB.

Restore first checks all hashes, then revokes the stored keys and disables
the collectors. The old gateway 0.4 must not be run over a restored schema 5: it
ignores the new chat/operation restrictions. Code, schema and role grants versions
are restored consistently. Policy details: [ACCESS.md](ACCESS.md).

## Reviews: schema 6

Messaging 0.6 adds reviews, review_sources and review_counter to the manifest: 26 tables.
Install backup image 0.6 before migration 006; it reads schema 2–6. The old image 0.5
does not accept schema 6. In a restored copy, check that authors,
request UUIDs, revisions/provenance and the counter are preserved, then run the standard quarantine.
Review marks do not turn into send permissions or ACK changes.

## Schema 32: event delivery and private feedback

The manifest includes 63 tables, adding subscription consumption, hashed page
receipts, registered receiver metadata, receiver bindings, the durable hint
outbox and key-owned local feedback, original residency, eviction journals,
durable backup fences and independent file-understanding artifacts/profiles.
Supported schema versions are 2–34. Install
the matching backup runtime before applying schema 32; older pinned runtimes
must reject newer schemas rather than silently omit the delivery state.

Receiver signing bytes remain in separate private operator files, never in the
DB tables or packages. Include those files and the dedicated dispatcher DSN in
the explicit encrypted recovery-state configuration. Verify table hashes before
quarantine. Quarantine then disables receivers, cancels pending/leased hints and
removes page receipts, while preserving consumed positions, historical hints
and feedback. Closed subscriptions, revoked API keys and disabled collectors
still require explicit reviewed reconfiguration before any restored runtime
may resume work. The dispatcher must stay stopped during a restore drill.
From schema 40 quarantine also sets `retention_hold` on every closed definition, so
subscription pruning never touches restored matches or consumption until the
operator runs `release-retention-hold` ([SUBSCRIPTIONS.md](SUBSCRIPTIONS.md#event-retention)).
An older restore quarantined at its own schema has no hold after migration 40, but its
definitions are closed and acknowledged-event retention never prunes closed ones; rerun
quarantine after migrating it to set the hold for later pruning paths.

Schema 41 adds `reply_snoozes`, `reply_mutes` and the `reason`/`note` columns of
`reply_dismissals` to the manifest; backups of schemas 2-40 still restore.

Schema 42 adds `owner_message_reads`, the receiving-period columns of
`collector_health` and the read-source columns of `chat_read_observations`;
backups of schemas 2-41 still restore.

Schema 43 adds `person_links` (owner-made person links, ANYM-283) to the manifest;
backups of schemas 2-42 still restore.
Schema 44 adds `api_key_operations` (repeatable key issue, ANYM-495) to the manifest;
backups of schemas 2-43 still restore.

## Family calendar

The backup image also captures the family calendar (ANYM-605): `python -m
messaging_service.backup_worker` with `"profile": "calendar"` and the keys `id`, `dsn_file`,
`vault_directory`, `profile_directory`, `recipient_file`, `work_directory` (read-only
mounts of `${CALENDAR_STATE_DIR}/service/vault` and `${CALENDAR_STATE_DIR}/service`). It
writes `bundle.tar.age`, `result.json` and `captured.json` like a staged messaging run;
without `profile` the worker behaves as before. Receiver verification is ANYM-606, the
daily schedule and the data-policy line ANYM-607 (below and docs/DATA-POLICY.md), the restore
procedure ANYM-610.

The host runner takes it with its own `runner.json` (ANYM-608; example:
`deploy/backup-runner-calendar.example.json`: the production paths, with the receiver host and
the image as placeholders) and its own `directory`,
journal and lock. Keys: `profile: "calendar"`, `directory`, `project`, `network`, `image`,
`dsn_file`, `recipient_file`, `state_mounts` (`vault` and `profile`: the host paths of
`${CALENDAR_STATE_DIR}/service/vault` and `${CALENDAR_STATE_DIR}/service`, mounted read-only at
`/sources/vault` and `/sources/profile`), `transfer`, `timeout_seconds`, `keep_local` and
`host_lock` (messaging's runner lock, `/opt/personal-os-probe/backups/.runner.lock`); optional
`delivery_mode` (`staged_v1` only: the calendar worker stages) and `busy_wait_seconds`.
`files_volume`, `mounts`, `recovery` and `dump_timeout_seconds` are refused, and so is a
`host_lock` that is the profile's own lock. Without `profile` (or with `"messaging"`) a
`runner.json` is read as before.

A calendar run never lists, stops or starts an application container: its journal records
`stopped: []` and names only its own worker, `deedkit-calendar-backup-<id>`, labelled with its
project; a journal naming anything else is refused before any Docker call. Recovery
(`--recover-stop`) stops and removes only that worker. A capture that failed before its
`result.json` is abandoned (the job directory goes, no fence action: the calendar capture
opens none) and the next run takes a new snapshot; an encrypted bundle and a lost receipt are
delivered again under the same ID, with messaging's journal, `receiver_busy` wait and resume.
The calendar delivers to the [R2 receiver](BACKUP_R2.md), which speaks only the v2 wire: the runner
sends its staged `bundle.tar.age` over `backup-put-v2` (header `max_bytes` = the file's size and
`snapshot_sha256` = the sha256 of the bundle's `manifest.json`, which the worker writes into
`result.json` and the verifier's proof must equal; then 1 MiB frames and the footer), and a stored
duplicate answers the header (ANYM-607). A `result.json` without `snapshot_sha256` (a backup image
from before this) is refused `invalid_backup_result` before any upload, so this runner needs a backup
image with the same change; messaging's delivery is unchanged.
The worker deletes the plaintext staging right after encryption.

Exclusion: a calendar run, a `--recover-stop` and a `Persistent=` catch-up first take the
profile's own `<directory>/.runner.lock` (held: `backup_job_already_running`), then wait up to
5400 s for `host_lock`, polling every 5 s; at the bound the run ends `host_lock_busy` (the
status file says so) with no journal and nothing started. It holds both until it ends.
Before it creates its worker it writes `<host_lock>.unsettled`, and removes it only once that
worker is verified absent or stopped (the run's end, `--recover-stop` or the next run): while
it exists, `production.py deploy` (with or without `--auto`) returns `busy`
(`backup_running`), so a worker left running by a failed recovery or a killed runner never
overlaps a deploy. While
it holds `host_lock`, autodeploy skips (exit 75), messaging's runner refuses
`backup_job_already_running` and systemd retries it, and a manual `production.py deploy`
returns `busy` (`backup_running`) before any change. `backup_runner.py --config <file>
--check` is read-only (no Docker call, no lock, nothing created): it prints
`{"status":"idle"}` and exits 0, or prints the unfinished journal's `id`, `phase` and the
number of stopped collectors and exits 3.

The bundle holds:
- `database.dump`: `pg_dump --format=custom --no-owner --no-acl` of `family_calendar`,
  taken from the snapshot exported by one REPEATABLE READ READ ONLY transaction;
- `manifest.json`: `profile: calendar`, `schema_version` (`max(version)` of
  `fcs.migrations`), `tables` and `type_warnings`, `dump_sha256`, `state_files` (bytes and
  sha256 each), `vault_warnings`, `vault_staging_skipped` and
  `restore_mode: verify_only_no_provider_calls`;
- `state/vault/`: the Fernet `key` and every `<connection>.enc` token;
- `state/profile/`: `postgres.env`, `gateway.json`, `worker.json` and `runtime.json` when
  present. `gateway.json` and `worker.json` hold the two service roles' passwords and are kept
  because the restore reuses their `bind`, `allowed_hosts` and `allowed_origins`.

The table manifest needs no table list: in the snapshot, with `TimeZone=UTC`, `DateStyle=ISO`,
`IntervalStyle=postgres`, `extra_float_digits=3`, `search_path=pg_catalog`, `lc_monetary=C` and
`bytea_output=hex` set locally, every ordinary table outside `pg_catalog` and
`information_schema` gets its row count and the sha256 of its sorted per-row sha256 digests of
the row's text form. A table a later migration adds is picked up from the catalog. A column
type outside the tested set (built-in base types, arrays, enums, domains over those) is still
hashed and listed in `type_warnings`; the backup is never refused for it.
`backup_calendar.calendar_manifest` rebuilds the same manifest from a restored database.

State files are read after the snapshot export, each opened with `O_NOFOLLOW` and read
through the held descriptor; a symlink or non-regular file refuses the capture
(`calendar_state_unsafe`). The writer's `.tmp-*` staging files are skipped and counted. Each
token's Fernet HMAC is checked under the copied key. Refusals, which build no bundle and leave
the previous protected point as it is: an enabled connection without its token
(`calendar_vault_incomplete`), a token under another key (`calendar_vault_key_mismatch`),
tokens without a key (`calendar_vault_key_missing`), a missing `postgres.env`, `gateway.json`
or `worker.json` (`calendar_profile_incomplete`). The worker prints only the code; the
connection or file it names is in `refused.json` in the private work directory. A disabled
connection without a token, a token without a connection row, a missing `runtime.json` and an
unknown vault entry are `vault_warnings`; the bundle is still built. An empty calendar (no
connections, a vault with only `key`, or no vault directory yet) builds a valid bundle.

Not in the bundle: agent key files (the database keeps their hashes; recovery is ANYM-610),
calendar logs, the container image and the raw provider data outside the database.
`backup_crypto` accepts `state/` members only in a bundle whose first member is a manifest with
`profile: calendar`, and such a bundle accepts no `files/` or `recovery/` member; messaging's
member rules are unchanged.

Retention: the calendar keeps one protected point plus room for one candidate
(`max_objects` = 2, and rotation keeps `protected: 1`, backup_r2_receiver.py:870), so
calendar data edited later is recoverable only as of the last protected point.

### Daily run and freshness (ANYM-607)

On the production host `deploy/deedkit-calendar-backup.service` runs the installed host runner
(`/opt/personal-os-probe/ops/backup_runner.py`, the same file as messaging's) with
`/opt/deedkit/calendar-backup/runner.json`, and `ExecStopPost` runs the same config's
`--recover-stop`. `deploy/deedkit-calendar-backup.timer` fires daily at 16:00 UTC with
`Persistent=true`: inside autodeploy's no-start hours (13:00-18:59), and the runner's 5400 s wait for
messaging's lock ends before messaging's 18:00 run. The hour only schedules; the lock excludes (above).
Its `ExecCondition` admits a start only up to 16:02 UTC (a `Persistent=` catch-up after a reboot or a
late manual start is skipped; an unreadable clock skips too; the timer fires within 16:00:31,
`AccuracySec=1s`). `TimeoutStartSec=6300s` plus the main process's stop and `ExecStopPost`, each with
its own `TimeoutStopSec=180s`, ends every admitted run, its lock wait and recovery included, before
18:00; a skipped start runs no recovery (`SERVICE_RESULT=exec-condition`), and the unit has no
`Restart=`, so the calendar never holds messaging's lock into messaging's run. A skipped or failed day
stays in `status.json` until the next day's run; a journal an earlier recovery left waits for the next
admitted run, and its `.unsettled` marker keeps deploys out meanwhile. The unit may write only its job directory and messaging's
backup directory (the shared lock and its `.unsettled` marker). The capture limit is
`timeout_seconds` 900 and the receiver busy wait `busy_wait_seconds` 1800.

The calendar's bundle goes to its own receiver project on the shared backup server: its own upload
user, forced-command line, state directory, recovery key and prefix in messaging's private R2
bucket (BACKUP_R2.md,
Calendar verifier instance), verified there by the calendar verifier, which takes turns with
messaging's ([Two verifiers on one receiver](BACKUP_R2.md#two-verifiers-on-one-receiver)).

Freshness at a glance, read-only, one command per host. Production host: the state, error code,
time and age of the last successful capture (`status.json` `last_snapshot_at`) of each side:

```sh
sudo python3 -I -c '
import json
from datetime import datetime,timezone
for name,path in (("messaging","/opt/personal-os-probe/backups/status.json"),("calendar","/opt/deedkit/calendar-backup/backups/status.json")):
    try:s=json.load(open(path))
    except OSError:print(name,"no status");continue
    at=s.get("last_snapshot_at");age=str(datetime.now(timezone.utc)-datetime.fromisoformat(at)).split(".")[0] if at else "-"
    print(name,s.get("state"),s.get("error_code") or "-","last capture",at or "never","age",age)'
```

Backup server (root): the verifier status and the time and age of the last protected proof
(`last-success.json` `verified_at`) of each side:

```sh
python3 -I -c '
import json
from datetime import datetime,timezone
for name,directory in (("messaging","/srv/backups/deedkit/verifier"),("calendar","/var/lib/deedkit-calendar-backup-r2/verification")):
    def read(file):
        try:return json.load(open(directory+"/"+file))
        except OSError:return {}
    s,p=read("status.json"),read("last-success.json");at=p.get("verified_at")
    age=str(datetime.now(timezone.utc)-datetime.fromisoformat(at)).split(".")[0] if at else "-"
    print(name,s.get("status","no status"),s.get("error") or "-","last proof",at or "never","age",age)'
```

Nothing alerts on a stale age yet (alerting is a separate card).
