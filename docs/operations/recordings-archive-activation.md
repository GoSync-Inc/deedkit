# Recordings archive: install, activation and restore drill (ANYM-731)

Status: a plan, **not applied**. It is the production operation plan of ANYM-731 (reviews.md Rounds 7: Astra r1 and one
recheck). An agent executes it only on the orchestrator's GO, under AGENTS.md section 2 and harness process.md section 2:
Astra-reviewed plan, a read-back and an undo for every step, no step during an active call. It contains no send, call, ACK,
pairing, provider-session start or stop, or allowlist change, and it never touches the on-demand grants (ANYM-713). Receipts
hold digests, modes, ids and counts, never a key, a transcript or any text of the archive.

The backup itself is in source and reviewed: the bundle (ANYM-714, BACKUP.md "Recordings archive bundle"), the staging runner
(ANYM-727, BACKUP.md "Staging, slot and admission"), the receiver, verifier, restore helper and continuity rules (ANYM-728,
BACKUP_R2.md "Recordings verifier instance"). This plan installs them in order and proves them on a real archive. It needs
no migration of the messaging database. The bootstrap cycle (the backup can only be proven on a real archive, and the archive
may only run once a backup exists) is broken by the milestones: polling starts at milestone A, after milestone I proved the
wiring with the archive still off.

## Milestones

| | Milestone | Done when | Read-back | Undo |
|---|---|---|---|---|
| S | Source ready | ANYM-714, 727, 728, 724 are merged and deployed as source/images; no `archive` block exists; nothing polls | P1-P8 | none (read-only) |
| I | Install, archive off | the host runner and its blob are installed; the receiver instance, verifier units and the recordings timers are installed and enabled; the first timer run records `recordings_skipped_archive_disabled` | I-read | Undo R, I1, I2 |
| A | Bounded activation | the archive config is applied; polling runs under ANYM-724's budgets; the first discovery count is read back and `max_bytes` sized | A-read | Undo A |
| V | First protected snapshot | the next timer run makes a bundle, the verifier accepts it, the receiver holds it as the protected point, freshness shows it | V-read | Undo V |
| D | Restore drill (closes the card) | one production-parity restore of the protected point into a scratch directory passes the checks and its receipt is recorded | D-read | Undo D |

Polling may start once milestone I is verified (docs/DATA-POLICY.md), not before. The card is not done until D.

## Conventions

- `S` is a green `main` SHA whose `push` CI finished and whose backup image `deedkit-production-backup:S` and recordings image
  exist on the host (production.md "Backup runner", "CI-published images"). `<date>` is the day of the run. `backup-1` is the
  shared backup server over the tailnet; `prod` is the production host. Commands run as root on the named host.
- **Abort rule.** On any failure: stop, mutate nothing further, read state first (`production.py status`, `systemctl status` of
  the units named in the step, the runner's `--check`, `docker ps`), then take the undo of the stage that failed. A failed
  command stops a step (`sh -e`). Journals, proofs, markers and status files are evidence: preserved, never deleted to make a
  step pass. Anything unclear stops the plan and goes to the orchestrator.
- **Windows.** Steps R and I2 run between 08:00 and 15:30 UTC (the calendar daily is 16:00, messaging's 18:00, the calendar
  catch-up 19:15, the recordings job 21:30, its catch-up 23:15; production.md "Backup runner" says to avoid the minutes around
  18:00). Milestone A starts after I-read is recorded and never within an hour before 21:30. Nothing runs during a call, a
  pending autodeploy/plan attempt or while `/etc/deedkit/autodeploy.pause` holds a reason that is not ours.
- Receipts go to one research note (`research/recordings-731/receipt.md`), appended per step with the time and the read-back
  lines, not the secrets.

## Prerequisites (step 0, read-only; any miss stops the plan)

- P1. GO from the orchestrator, given after release-n9 (migration 067) is applied and autodeploy has resumed or is paused by its
  owner. Until then nothing below runs.
- P2. `S` contains ANYM-714, 727 and 728 (`git merge-base --is-ancestor <sha> S` for 359613de, 614f390f and f55687df) and ANYM-724's schema 3
  (archive collector), its push CI is green and Tiden sync B covers it. No other `S` is mixed in: the runner file, the backup
  image, the receiver wheel and the recordings image all come from this one `S`.
- P3. ANYM-713's activation (recordings-api enrolled, first start by hand, egress admission) is applied and `docker ps` shows
  `deedkit-recordings-recordings-api-1` healthy at a release that carries 727's compose mount. If not, stop: the staging mount and
  the container name this plan uses do not exist yet. The archive is still off: `config.json` has no `archive` block and no
  account has `archive_namespace`.
- P4. No active call (`production.py`'s active-call check, production.md "Apply and verify"), `production.py status` clean, deploy
  lock free, `personal-os-backup.service`, `deedkit-calendar-backup.service`, `deedkit-autodeploy.service` and the recordings
  units inactive, 15 GiB free on `prod`, and on `backup-1` free space for the receiver's two slots (below).
- P5. Fresh backups exist and are receiver-verified: the latest messaging and calendar dailies are `state: ok`, restore-verified
  (BACKUP.md "Freshness at a glance"). This plan changes no data, so these are the restore point of the host files it touches;
  a copy of each file the plan edits is taken before the edit (`.before-<date>`, mode kept).
- P6. Host facts read into the receipt: the pinned messaging image in `/opt/personal-os-probe/backup-config/runner.json`, the
  installed runner blob (`git hash-object /opt/personal-os-probe/ops/backup_runner.py`), the blob of `S`'s
  `services/messaging/messaging_service/backup_runner.py`, the backup server's host key line in the messaging `known_hosts`, the
  recordings container name and the mount (`docker inspect` fields `Mounts` only: destination `/run/deedkit/backup-staging`,
  source, read-write) and `RECORDINGS_BACKUP_STAGING` in the env file the recordings project reads.
- P7. On `backup-1`: `/opt/deedkit-backup-r2/venv` exists, the messaging and calendar verifier timers are active and their last
  `status.json` is `verified` or `idle`, no `active.json` journal is pending in either, `/etc/deedkit-backup-r2/instances.json`
  lists the two configs (messaging first), and `receiver_schema_max` is current. The follow units are not installed there
  (the receiver's wheel moves by hand).
- P8. Decisions to confirm in the receipt before starting (section "Decisions to review"): receiver limits, the R2 prefix, the age
  recovery key custody and the archive `max_bytes` start value.

## Step R. The host backup runner and the pins (one host step; milestone I, part 1)

727 changed `backup_runner.py` and the backup worker, so the host runner file and the backup image pin must move together. This is
production.md "Backup runner" with `pin=yes`, plus the calendar pin, in one lock window. Autodeploy refuses to apply while the
installed runner blob is not the release's, so this step is also what lets the deploys after n9 run: remove
`/etc/deedkit/autodeploy.pause` only if its reason is the runner's blob refusal (`the backup runner ... is blob`); a pause for another
reason stays.

1. Record P6. Create `/etc/deedkit/autodeploy.pause` with reason `ANYM-731 runner install` unless one with another reason exists
   (then do not touch it and stop for the orchestrator).
2. Run the script of production.md "Backup runner" with `sha=S; src=<state_directory>/S/source; pin=yes`. It holds
   `/opt/personal-os-probe/backups/.runner.lock` (`flock --nonblock`), requires `personal-os-backup.service` and
   `deedkit-autodeploy.service` idle, checks the new runner against messaging's config, resolves `deedkit-production-backup:S` and
   its revision label, writes the new `runner.json` beside the live one and checks it, keeps `.before-S` copies of the runner and
   `runner.json`, installs the runner and verifies its blob.
3. In the same idle window (the same services idle, under a `flock --nonblock` of the same `.runner.lock`): set
   `/opt/deedkit/calendar-backup/runner.json`'s `image` to the same ID (keep `runner.json.before-<date>`, write a `.new` file, `mv`), then check both:
   `python3 -I /opt/personal-os-probe/ops/backup_runner.py --config <cfg> --check` for messaging and for the calendar must print `idle`
   (exit 0). Exit 3 means an unfinished journal: stop (production.md says how its recovery stays under the existing rules).
4. Remove the pause (only if it is ours). Read-back **R-read**: `git hash-object` of the installed runner equals `S`'s blob; both
   `--check` print `idle`; after the next scheduled messaging backup `/opt/personal-os-probe/backups/status.json` shows `state: ok`
   with a new `last_success_id`, and the next calendar daily likewise; `production.py deploy --dry-run` no longer reports the runner
   blob refusal.

Undo R: production.md "Backup runner" undo, in its order, under the same `flock` with the backup services idle (remove from
`runner.json` every key the `.before-S` runner does not know, install the `.before-S` copy, then put back the image pin;
the calendar `runner.json` from its `.before-<date>` copy). After the migration of n9 the old pin would fail every capture
(`unsupported_backup_schema`): then keep the new pin and undo only the runner if the production.md rule allows it; otherwise stop
and report. The recordings files of later steps do not exist yet at this point.

## Step I1. The receiver instance on backup-1 (milestone I, part 2)

The instance has its own upload user, SSH route, receiver config, upload state, recovery key, R2 prefix and protected marker,
exactly like the calendar's (BACKUP_R2.md "Calendar verifier instance" and "DeedKit's recordings project on the shared receiver").
Nothing here touches the messaging or calendar instance, except the shared `instances.json` and the shared helper pins.

1. **Wheel.** Build the wheel and the lock from `S` (`python scripts/build.py`, `services/messaging/requirements-backup-r2.lock`),
   inspect the contents (`unzip -l`: `backup_r2_receiver.py`, `backup_r2_verifier.py`, `backup_r2_follow.py`,
   `backup_restore_worker.py`, `backup_recordings.py`, `backup_recordings_continuity.py`), copy to `backup-1`, and with both
   existing verifier timers stopped and their services inactive (no pending `active.json`): `pip install --no-deps --force-reinstall`
   into `/opt/deedkit-backup-r2/venv`, then `pip install --require-hashes -r <lock>`. Keep the previous wheel as
   `/opt/deedkit-backup-r2/wheel.before-<date>`. Read-back: `python -c "import messaging_service.backup_r2_receiver as r; print(r.PROFILES)"`
   prints `('messaging', 'calendar', 'recordings')`; start both existing timers again; their next runs end `idle` or `verified`.
2. **Account objects.** Create the upload user `deedkit-recordings-backup` with a root-owned home and the `Match User` block and
   `AuthorizedKeysFile /etc/ssh/authorized_keys.d/deedkit-recordings-backup` (root, `0644`) as the calendar's, whose one line is
   `restrict,from="<prod tailnet address>",command="/opt/deedkit-backup-r2/venv/bin/python -I -m messaging_service.backup_r2_receiver --config /etc/deedkit-recordings-backup-r2/receive.json receive" <public key>`.
   The public key is the one generated in step I2.1 (the plan runs I2.1 first, then this step; the key line is the only value
   crossing between the hosts). `sshd -t` before reload; read the file ownership back.
3. **Directories and configs** (modes as BACKUP_R2.md "Receiver installation"): `/srv/backups/deedkit-recordings/state/receive`
   (upload-owned `0700`, ancestors root-owned), `/etc/deedkit-recordings-backup-r2/` with the upload-owned `receive.json` and its
   credentials, the root `operator.json` and its credentials (both `"profile": "recordings"`, messaging's bucket and endpoint, the
   prefix `deedkit-recordings/egor-<date>`; the credential files copy messaging's token), `control/`, `proofs/`, `restore/`, and
   `/var/lib/deedkit-recordings-backup-r2/verification` (root `0700`). Receiver limits: see "Decisions to review". The first
   receive initializes `.receiver.lock`, or provision it as the upload user `0600`.
4. **Recovery key.** Generate the instance's own age identity into `/etc/deedkit-recordings-backup-r2/restore/identity.agekey`
   (root `0600`, never printed); the public line (the recipient) is what step I2 installs on `prod`. The key is not copied to
   `prod`, and not to the messaging or calendar directories.
5. **Verifier config and instances.** `verifier.json` from `deploy/recordings-backup-r2-verifier.example.json` (root `0600`):
   `profile: recordings`, paths as above, `helper_image`/`helper_revision` equal to the two existing instances' current pins.
   Append `/etc/deedkit-recordings-backup-r2/verifier.json` to `/etc/deedkit-backup-r2/instances.json` (messaging first; copy
   kept as `instances.json.before-<date>`).
6. **Pins.** The helper must be a revision that carries 728 (a helper older than it fails the recordings self-test with
   `recordings_helper_failed`). If the current pins predate it: `python -m messaging_service.backup_r2_verifier --update-pins
   --helper-image <ID of S's backup image> --helper-revision S`. It self-tests all three profiles on synthetic bundles under the
   host lock and moves the pins of all three configs together, or none (production.md/BACKUP_R2.md "Two verifiers on one receiver").
   Read back that all three configs show the same pair.
7. **Rehearsal on a synthetic archive (before any real data).** With a dedicated synthetic prefix and a throwaway config
   (BACKUP_R2.md "Scheduling and enrollment": the server-native synthetic drill), real age encryption and the pinned images:
   make a synthetic recordings bundle (`synthetic_archive` + `create_recordings_snapshot` + `encrypt_snapshot` from the installed
   wheel), deliver it through the forced SSH route with a throwaway key, and run the verifier on it. Check, with receipts of
   statuses only: (a) the six checks pass and the first point is protected (`protect` path), (b) a second synthetic bundle with
   one transcript emptied is refused `archive_content_lost` and the first point and its object stay, (c) a grown third bundle
   rotates to two points then one, (d) restarts: the verifier is killed (`systemctl kill`) mid-run and re-run, and the receiver
   process is restarted between delivery and verification, each ending in the same protected point, (e) the scratch
   (`payload-*`, `secrets-*`) is gone after success, failure and restart, (f) the three profiles keep passing on the shared
   pins. Remove only its exact owned objects, directories and config (never a bucket-wide delete).
8. **Units.** Install `deploy/deedkit-recordings-backup-r2-verifier.service` and `.timer` into `/etc/systemd/system/` (`0644`),
   `systemctl daemon-reload`, `systemctl enable --now deedkit-recordings-backup-r2-verifier.timer`. The timer finds no candidate and
   writes `status.json` `idle`; that is correct before the first upload.

Read-back **I1-read**: wheel profiles line (above); `ssh -G`/`sshd -T` for the user show the forced command only; the three verifier
timers active; `status.json` of the recordings instance `idle`; the messaging and calendar instances' next runs unchanged;
`instances.json` and the three `verifier.json` pins equal; `receiver_schema_max` unchanged.

Undo I1 (reverse order): `systemctl disable --now deedkit-recordings-backup-r2-verifier.timer` (let a running service end);
remove the unit files and `daemon-reload`; restore `instances.json.before-<date>` and re-run `--update-pins` only if the pins
changed in 6 and the previous pair is wanted (`pins-previous.json`); remove the `Match User` block and the `authorized_keys.d`
file, `sshd -t`, reload; move `/etc/deedkit-recordings-backup-r2`, `/srv/backups/deedkit-recordings` and
`/var/lib/deedkit-recordings-backup-r2` aside as `*.removed-<date>` (root `0700`, evidence, the recovery key included); the wheel
back to `wheel.before-<date>` only with all verifier services inactive (an older wheel refuses nothing of the other two
profiles: it does not know `recordings`, which is why the unit files go first). The R2 prefix holds only the rehearsal's removed objects.

## Step I2. The producer side on prod (milestone I, part 3)

1. **Directory and delivery key.** As root: `install -d -o root -g root -m 0700 /opt/deedkit/recordings-backup` and its children
   `backups` (`0700` root), `state` (`0700` root) and `staging` (`0700`, **10001:10001**; the runner refuses any other owner or mode,
   `recordings_staging_unsafe`). `ssh-keygen -t ed25519 -N ""` into `delivery_ed25519` (`0600`); the public line goes to step I1.2.
   `known_hosts`: copy messaging's, keep its mode, and check `ssh-keygen -F <backup-1 tailnet address>` finds the host.
2. **Staging mount.** The recordings container mounts `${RECORDINGS_BACKUP_STAGING:-/opt/deedkit/recordings-backup/staging}` at
   `/run/deedkit/backup-staging` (`deploy/compose.recordings.yaml`). If P3's read-back shows the mount already present and read-write,
   nothing changes. If the deployed overlay predates 727, the mount arrives with the next release that carries it, applied by
   `production.py` (never `compose up --build`, never by hand); the staging directory exists before that release, or Docker creates
   a root-owned one and the runner refuses it. Read back: the `Mounts` entry of the container, and
   `docker exec deedkit-recordings-recordings-api-1 ls -ld /run/deedkit/backup-staging` shows `10001` ownership.
3. **Recipient and config.** `recipient` = the public age line from step I1.4 (`0600` root). `runner.json` from
   `deploy/backup-runner-recordings.example.json` (`0600` root): `image` = messaging's pin after step R (the same ID), `transfer.host` =
   the backup-1 tailnet address, `transfer.user` = `deedkit-recordings-backup`, paths as the example, `container` as P6 read it,
   `recordings.timers` = the real timer files of the other profiles. Check: `python3 -I /opt/personal-os-probe/ops/backup_runner.py
   --config /opt/deedkit/recordings-backup/runner.json --check` prints `{"status":"idle","profile":"recordings"}`.
4. **Units.** `deploy/deedkit-recordings-backup.service`, `.timer`, `-catchup.service`, `-catchup.timer` into `/etc/systemd/system/`
   (`0644`), `daemon-reload`, then `systemctl enable --now deedkit-recordings-backup.timer deedkit-recordings-backup-catchup.timer`.
   Optionally the freshness script: install `deploy/recordings-backup-freshness.py` as `/opt/deedkit/recordings-backup/freshness.py`
   (`0755` root) for the read-backs below (it is read-only; no unit is needed for it).
5. **First run, archive off.** Do not wait for 21:30: `systemctl start deedkit-recordings-backup.service` once, in an allowed window
   and not within 15 minutes of another profile's timer (the runner decides; a `recordings_skipped_window` is also a valid
   answer, repeat later). The run reaches the container: its `docker exec ... --backup-to` answers `archive_disabled`, which proves the
   container name, the config path and the mount without data. Expected: exit 0, `status.json` of the recordings job `state: skipped`
   with `recordings_skipped_archive_disabled`, staging empty, no worker container, no bundle, nothing delivered.

Read-back **I-read** (milestone I verified): the file modes (`stat`), the `--check` line, the skip status and code, `ls -A staging`
empty, `systemctl list-timers 'deedkit-recordings-*' 'deedkit-calendar-*' personal-os-backup.timer` listing every timer with
unchanged next elapse for the other two profiles, and the next messaging and calendar dailies reaching `state: ok` on their usual
times. The recordings-api health and `archive_status` (`archive disabled`) unchanged. Record the receipt. Only now may milestone A start.

Undo I2: `systemctl disable --now deedkit-recordings-backup.timer deedkit-recordings-backup-catchup.timer`; let a running
`deedkit-recordings-backup.service` end or `systemctl stop` it (its `ExecStopPost` recovers); go on only when it is inactive, no
`deedkit-recordings-backup-*` container exists, `/opt/deedkit/recordings-backup/state/.unsettled` and
`/opt/personal-os-probe/backups/.runner.lock.unsettled` are absent and `--check` prints `idle`; remove the units, `daemon-reload`;
move `/opt/deedkit/recordings-backup` aside (`*.removed-<date>`, root `0700`, keep its journal and status). A failed check stops the
undo. Messaging's and the calendar's files are not touched. The staging mount stays (harmless without the timer).

## Milestone A. Bounded activation (archive on)

Applied by the ops step of this plan; the agent only checks the owner's key and file modes, and starts no provider session.

1. **Preconditions.** I-read recorded; the Omi key in place (`omi.key` mode `0600`, owner 10001, non-empty: modes only); no call; not
   within an hour before 21:30 UTC; `archive_status` for the Omi account answers (archive disabled) with an operator backend key.
2. **Namespace.** For each account to archive generate a fresh 32-hex `archive_namespace` once (`python3 -I -c "import secrets;
   print(secrets.token_hex(16))"`; record the first 6 characters in the receipt, not the value elsewhere). Omi first; PLAUD only
   if its key is in place and the owner's 11-03 words cover it (they cover Omi and PLAUD text, not audio).
3. **Config.** Back up `config.json` (`cp -p` to `config.json.before-<date>` inside the private directory, mode kept). Write the new
   document to a `.new` file in the same directory (`0600`, owner 10001) from `deploy/recordings-archive.example.json`'s shape: the
   `archive` block (`directory: /data/archive`, `poll_seconds: 300`, `max_bytes: 4294967296`, `free_reserve_bytes: 1073741824`,
   `omi_budget: {transcript_per_hour: 18, reads_per_hour: 45}`) and the `archive_namespace` of the chosen accounts; **no
   `event_router` block** (ANYM-732/733 own it). Validate with the image's own loader before replacing: `docker run --rm --network none --read-only --user 10001
   -v <private dir>:/run/deedkit/recordings:ro <recordings image of S> python -I -c "from deedkit_recordings.security import
   read_document as r; d,_=r('/run/deedkit/recordings/config.json.new'); print(d.archive is not None, len(d.accounts))"` must print
   `True <n>` (it prints no value of the file); then `mv config.json.new config.json` (atomic replace inside the directory).
4. **Start.** The collector reads the configuration each pass; read `archive_status` after one `poll_seconds` plus a minute. If the
   traversal has not started, restart **only** `recordings-api` (`docker compose $F restart recordings-api`; admission re-checks the
   egress rules) and read again. The first start applies schema 3 (additive, single writer).
5. **Sizing, after the first hour.** Read `archive_status` (`traversal.discovered_total`, `projected_db_bytes`, `capacity_warning`,
   `usage`). Set `max_bytes = 3 x projected_db_bytes` (plus `free_reserve_bytes` of free space) by the same backup-and-atomic-replace;
   if `capacity_warning` shows or the free space on the archive volume is below the reserve plus one expected archive, stop polling
   (Undo A) and report.

Read-back **A-read**: `archive_status` shows the traversal advancing, `usage` within the Omi share (the aggregate stays under 45/h
for this collector so agents keep the rest), `db_bytes` below the ceiling; `docker stats --no-stream` of the container within its limits;
the on-demand tools still answer for the existing principals (a read-only `recordings_capabilities`, no content request); the gateway
policy revision unchanged; no new principal or grant; the egress admission still passes (`docker logs` shows no `exit 78` loop).

Undo A: restore `config.json.before-<date>` (atomic replace) or remove the top-level `archive` and every `archive_namespace`; the
service stops new collection (README "Optional private text archive"); stop only `recordings-api` if the collector must end at once.
Keep the archive directory and its state intact for re-enable or recovery; do not delete it.

## Milestone V. The first protected snapshot

1. Let the 21:30 UTC job run (or `systemctl start deedkit-recordings-backup.service` in the window of step I2.5 once the archive has
   records). It stages an Online Backup snapshot, seals and encrypts it, delivers it over the forced SSH route (`recordings` profile,
   the v2 wire), and empties staging on every exit.
2. On `backup-1` the receiver commits the upload; the recordings verifier (root timer, 2 minutes) restores it under the host lock,
   runs the six checks, judges continuity (no baseline and no protected point: the first enrollment passes) and calls `protect`: the marker
   is written, the baseline `continuity.json` is stored next to the proof.
3. Restart rehearsal on the real flow (each step ends with the same protected point): stop `recordings-api` only and start it again
   (the snapshot of the next day is taken from the restarted service, which must not recreate an empty archive: its `meta` identity
   is the same `archive_id`); restart the receiver process between a delivery and its verification (a test candidate is not
   needed: do it on the first real upload only if the timing allows, otherwise rely on the synthetic rehearsal of I1.7 and say so
   in the receipt); `systemctl restart deedkit-recordings-backup-r2-verifier.service` between a run's end and the next.

Read-back **V-read**: the job's `status.json` `state: ok` with `archive_bytes` and a `last_success_id`; on `backup-1` the verifier's
`last-success.json`, a `proof-<id>.json` whose `verification.archive` equals the job's manifest (`archive_id`, `incarnation`,
`sequence`, `records`) and whose counters are consistent with `archive_status` (records within the discovery count), the protected
marker present and naming that backup, exactly one object under the prefix, `continuity.json` present (`run` equals the verification
id; no text in it: `grep -c` of a known word is 0), staging on `prod` empty, no `payload-*` or `secrets-*` left on `backup-1`, and
`recordings-backup-freshness.py --verifier-directory /var/lib/deedkit-recordings-backup-r2/verification --max-age-hours 30
--state <state file>` printing `fresh` with the bytes and identity. The messaging and calendar dailies of that day are `ok`.
A refusal instead (`archive_content_lost`, `archive_identity_changed`, any helper code) is not retried blindly: read the status, keep
the evidence, stop polling if the cause is the archive (Undo A), report.

Undo V: nothing to remove on `prod`. If the first point must be withdrawn: `systemctl disable --now` the recordings verifier timer
and the two recordings producer timers, preserve `protected.json`, the proof, the journal and `continuity.json`, and move the
recordings prefix's single object aside only under a reviewed deletion operation (data deletion keeps its own gates); the archive
itself is untouched.

## Milestone D. The restore drill (closes the card)

Production-parity: the protected point from R2, decrypted with the instance's own recovery key, restored into a scratch directory,
checked, then removed. It never touches the live archive volume.

1. On `backup-1` as root, with no verification running, make a scratch directory `/var/lib/deedkit-recordings-backup-r2/drill-<date>`
   (`0700`). Download the protected object and verify its full ciphertext hash and size against its receipt (BACKUP_R2.md "Commit,
   download and independent restore"), decrypt it with `restore/identity.agekey`, and extract the two members. The key and the
   plaintext never leave `backup-1`.
2. Place the member as `<scratch>/archive.sqlite` (`0600`). Run, in a throwaway container with `--network none --read-only`
   (the backup image, no volume but the scratch): `verify_recordings_bundle` on the extracted bundle (structure, hashes, schema,
   `integrity_check`, identity), then a **synthetic** readback against the restored copy using a synthetic configuration (no real
   token, no path of the live archive): the installed recordings code opens the file read-only and answers an `archive_status`-like
   count query and a search for a fixed synthetic token, and `continuity.archive_facts` produces the hash-only facts. Print counts
   and the OK lines only, never text.
3. Compare: `archive_id`, `incarnation`, `sequence` and `records` from the restored copy equal the manifest and the proof's;
   `continuity.json`'s facts equal the restored copy's facts (hash for hash).
4. Record the receipt (the backup id, ciphertext hash, sizes, the identity line, the four check results, the times). Remove the
   scratch directory (`rm -r` with the exact path, cwd outside it) and read back that it is gone; the recovery key and every other
   file are untouched.
5. **Restore procedure for a live volume (documented here, not executed by this plan).** Stop only `recordings-api`; move the live
   archive directory aside whole (`archive.removed-<date>`, kept as evidence); place the restored `archive.sqlite` (`0600`, owner 10001)
   in a new `0700` directory with **no** `journal.marker`; start the service. The mismatched or missing marker bumps the incarnation
   to the clock's minute and gives the journal a new `journal_id` (README "Identity"), so a restored archive never reuses event
   identities; the module router then reports a source reset and waits for its operator (`set-module-cursor`). The next backup judges the
   candidate against the baseline: a higher incarnation is accepted, and if the restored snapshot lost parts the continuity rules
   refuse it until the operator re-baselines with a reason (`--rebaseline`, BACKUP_R2.md).

Read-back **D-read**: the receipt lines above and the absence of the scratch. Undo D: none needed (the drill changed nothing);
if it stopped midway, remove only the scratch directory.

## Order and timing

R (0.5 h) -> I1 (2-3 h with the rehearsal) -> I2 (1 h) -> I-read -> A (0.5 h, plus an hour's wait and the sizing) -> V (next 21:30
slot) -> D (1 h). Steps R, I1.1-I1.6 and I2 can be done in one working window; the archive is switched on only after I-read, at least
an hour before 21:30. If a window closes mid-step, the stage's undo or the abort rule applies; nothing is left half-installed
(each step writes new files beside the live ones and replaces atomically, or enables a unit last).

## Data egress and authority

- Plaintext of the archive exists in the recordings volume (as before), in the 0700 staging directory for one run and in the
  verifier's 0700 scratch on `backup-1` for each verification and for the drill, nowhere else. The ciphertext goes over the
  tailnet to `backup-1` and, from there, to the private R2 prefix. Statuses, proofs, the baseline and the freshness line hold
  identity counters, hashes and byte counts, never text (docs/DATA-POLICY.md; owner 2026-11-03: «давай включим да», «пусть качает все
  и дает удобную интексацию агентам»).
- The activation starts only the Omi (and PLAUD) text reads the owner approved, inside ANYM-724's budgets. No audio, no send, no
  call, no pairing, no session, no allowlist or grant change.
- Principal: the executing agent, on the orchestrator's GO; owner decision D25 (AGENTS.md section 2). Budget: R2 storage for one
  point plus one candidate (a few GiB) is inside the monthly cap.

## Decisions to review

1. Receiver limits for the instance: `max_bytes` 8 GiB per object, `quota_bytes` 16 GiB, `max_objects` 2, `part_bytes` 64 MiB,
   `timeout_seconds` 3600 (the archive is about 1.3 GiB at the 4 GiB page budget; the example's 40/80 GiB are messaging's).
2. The verifier example is the calendar's copy: `max_plaintext_bytes` 40 GiB and a 21600 s deadline match `TimeoutStartSec=26400s`.
   Smaller values are possible but then the unit's timeout must be reduced consistently; the plan keeps the reviewed pair.
3. The recordings verifier waits for the shared host lock behind a messaging restore (up to the `verify_wait_seconds` bound); it does
   not get a slot of its own, by design (one heavy restore per host).
4. `max_bytes` starts at the example's 4 GiB and is sized from the first discovery count, as production.md "Omi backfill sizing".
5. Where a read-back cannot be done on the real flow (a receiver restart exactly between a delivery and its verification), the
   synthetic rehearsal of I1.7 stands in and the receipt says so.
6. The plan leaves ANYM-730 (deletion, tombstones) and ANYM-732/733 (the event router block) out; the tombstone contract
   (`tombstones(provider_hash)`, SHA-256 of namespace NUL provider_id) is in BACKUP_R2.md.
