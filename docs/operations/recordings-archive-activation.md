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
  (the receiver's wheel moves by hand). `/usr/local/libexec/deedkit/attest-verify` exists and runs, Docker works, and the Docker
  data root has the verifier's `reserve_bytes` plus 4 GiB free (the helper image pull of I1.6).
- P8. Decisions to confirm in the receipt before starting (section "Decisions to review"): receiver limits, the R2 prefix, the age
  recovery key custody and the archive `max_bytes` start value.

## Step R. The host backup runner and the pins (one host step, one lock window; milestone I, part 1)

727 changed `backup_runner.py` and the backup worker, so the host runner file and the backup image pin must move together. This is
production.md "Backup runner" with `pin=yes`, extended to the calendar pin, **as one script under one `flock` of
`/opt/personal-os-probe/backups/.runner.lock`**: nothing is written until both profiles passed every preflight, and the lock is
released only after the verification at the end (settle-before-write; no backup can interleave between the messaging and the
calendar pin). Autodeploy refuses to apply while the installed runner blob is not the release's, so this step is also what lets the
deploys after n9 run: remove `/etc/deedkit/autodeploy.pause` only if its reason is the runner's blob refusal (`the backup runner ...
is blob`); a pause for another reason stays.

1. Record P6. Create `/etc/deedkit/autodeploy.pause` with reason `ANYM-731 runner install` unless one with another reason exists
   (then do not touch it and stop for the orchestrator).
2. Copy `step-r.sh` (the block after Undo R: the production.md script with the calendar profile added, nothing else changed) to
   the receipt directory and run it `sudo flock --nonblock /opt/personal-os-probe/backups/.runner.lock sh -eu step-r.sh <S> <src>`.
   In this one lock window it:
   1. requires `personal-os-backup.service`, `deedkit-calendar-backup.service` and `deedkit-autodeploy.service` idle
      (`inactive` or `failed`);
   2. **preflights both profiles before any write**: the new runner's `--check` against messaging's `runner.json` and against the
      calendar's (`/opt/deedkit/calendar-backup/runner.json`) must both print `idle`, exit 0 (exit 3 is an unfinished journal in
      either profile: stop, production.md says how its recovery stays under the existing rules; the calendar's journal is
      therefore looked at *before* its pin changes); resolves `deedkit-production-backup:S`, compares its revision label with `S`,
      writes `runner.json.new` for **both** profiles beside the live files and `--check`s each `.new` with the new runner;
   3. preserves both configurations and the runner: `cp -p` of the runner and of both `runner.json` to `.before-S` (create-only:
      the script stops when a `.before-S` file already exists);
   4. installs both pins (`mv` each `.new`) and then the runner (`install -m 0600`), and verifies the blob of the installed runner
      against `S`'s;
   5. verifies inside the lock: `git hash-object` equality, both `--check` lines (`idle`), and that both `runner.json` carry the
      same image ID. Only then does the script end and the lock is released.
   The abort rule inside the script: a failure after step 4 begins stops the script with the `.before-S` copies in place; Undo R
   (below) starts from them, still under a fresh `flock`.
3. Remove the pause (only if it is ours). Read-back **R-read**: `git hash-object` of the installed runner equals `S`'s blob; both
   `--check` print `idle`; after the next scheduled messaging backup `/opt/personal-os-probe/backups/status.json` shows `state: ok`
   with a new `last_success_id`, and the next calendar daily likewise; `production.py deploy --dry-run` no longer reports the runner
   blob refusal.

Undo R: production.md "Backup runner" undo, in its order, under the same `flock` with the backup services idle (remove from
`runner.json` every key the `.before-S` runner does not know, install the `.before-S` copy, then put back the image pin;
the calendar `runner.json` from its `.before-S` copy). After the migration of n9 the old pin would fail every capture
(`unsupported_backup_schema`): then keep the new pin and undo only the runner if the production.md rule allows it; otherwise stop
and report. The recordings files of later steps do not exist yet at this point.

```sh
# step-r.sh <sha> <src>   (run under: flock --nonblock /opt/personal-os-probe/backups/.runner.lock sh -eu step-r.sh ...)
sha=$1 src=$2 ops=/opt/personal-os-probe/ops
new=$src/services/messaging/messaging_service/backup_runner.py
cfgs="/opt/personal-os-probe/backup-config/runner.json /opt/deedkit/calendar-backup/runner.json"
idle() { out=$(python3 -I "$1" --config "$2" --check); case $out in *'"idle"'*) ;; *) echo "$2 not idle: $out"; exit 1;; esac; }
for u in personal-os-backup.service deedkit-calendar-backup.service deedkit-autodeploy.service; do
  case $(systemctl show -P ActiveState $u) in inactive|failed) ;; *) echo "$u not idle"; exit 1;; esac
done
image=$(docker image inspect -f '{{.Id}}' "deedkit-production-backup:$sha")
case $image in sha256:*) ;; *) echo "no backup image for $sha"; exit 1;; esac
test "$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image")" = "$sha"
test ! -e "$ops/backup_runner.py.before-$sha"
for c in $cfgs; do                                  # pass 1: read-only preflight of both profiles, nothing is written
  test ! -e "$c.before-$sha"
  idle "$new" "$c"
done
for c in $cfgs; do                                  # pass 2: stage the two .new files beside the live ones, then check them
  rm -f "$c.new"
  python3 -I -c 'import json,os,sys;p,i=sys.argv[1:];c=json.load(open(p));c["image"]=i
fd=os.open(p+".new",os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600);os.write(fd,(json.dumps(c,indent=2)+"\n").encode())
os.fsync(fd);os.close(fd)' "$c" "$image"
  idle "$new" "$c.new"
done
cp -p "$ops/backup_runner.py" "$ops/backup_runner.py.before-$sha"
for c in $cfgs; do cp -p "$c" "$c.before-$sha"; done
for c in $cfgs; do mv "$c.new" "$c"; done
install -o root -g root -m 0600 "$new" "$ops/backup_runner.py"
test "$(git hash-object "$ops/backup_runner.py")" = "$(git hash-object "$new")"
for c in $cfgs; do                                  # verification, still inside the lock
  idle "$ops/backup_runner.py" "$c"
  test "$(python3 -I -c 'import json,sys;print(json.load(open(sys.argv[1]))["image"])' "$c")" = "$image"
done
echo "installed $(git hash-object "$ops/backup_runner.py"), image $image"
```

## Step I1. The receiver instance on backup-1 (milestone I, part 2)

The instance has its own upload user, SSH route, receiver config, upload state, recovery key, R2 prefix and protected marker,
exactly like the calendar's (BACKUP_R2.md "Calendar verifier instance" and "DeedKit's recordings project on the shared receiver").
Nothing here touches the messaging or calendar instance, except the shared `instances.json` and the shared helper pins.

1. **Wheel.** Build the wheel and the lock from `S` (`python scripts/build.py`, `services/messaging/requirements-backup-r2.lock`),
   inspect the contents (`unzip -l`: `backup_r2_receiver.py`, `backup_r2_verifier.py`, `backup_r2_follow.py`,
   `backup_restore_worker.py`, `backup_recordings.py`, `backup_recordings_continuity.py`), copy to `backup-1`.
   **Capture first** (receipt, `systemctl show -P ActiveState,UnitFileState`): the state of both existing verifier timers and
   services (`deedkit-backup-r2-verifier`, `deedkit-calendar-backup-r2-verifier` or the names `systemctl list-timers` shows),
   `pip freeze` of the venv (`/opt/deedkit-backup-r2/venv`, saved as `freeze.before-<date>`), the previous wheel kept as
   `/opt/deedkit-backup-r2/wheel.before-<date>` (copied before anything is replaced), and `TimeoutStartSec` of both services read
   against BACKUP_R2.md "Install order" (`TimeoutStartSec=26400s`, the deadline pair of the verifier wheel): a service with a shorter
   timeout is fixed (unit file from `S`, `daemon-reload`) **before** the wheel moves, because a wheel that waits for the receiver
   lock needs the longer unit.
   Then stop both existing timers and wait until both services are inactive and no `active.json` is pending (stop the timers in
   **both directions**: the same stop applies before the install and before any undo that reinstalls a wheel). `pip install
   --no-deps --force-reinstall` the wheel into the venv, then `pip install --require-hashes -r <lock>`. Read-back: `python -c "import
   messaging_service.backup_r2_receiver as r; print(r.PROFILES)"` prints `('messaging', 'calendar', 'recordings')`. Restore both
   timers to their **captured** states (not unconditionally enabled), start them if they were active; their next runs end `idle` or
   `verified`. **A failure at any point of this item** (install, import, a timer that does not come back) is handled before the
   abort rule goes on: reinstall the kept wheel and `freeze.before-<date>` with the timers still stopped, restore the captured
   states, confirm both existing instances' next runs, then stop the plan.
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
6. **Helper image on backup-1, then the pins.** The helper must be a revision that carries 728 (a helper older than it fails the
   recordings self-test with `recordings_helper_failed`). `--update-pins` only inspects images already in `backup-1`'s Docker
   (`Docker.preflight`, `backup_r2_verifier.py:557`); it never pulls, and the image of `S` exists on `prod`, not on `backup-1`. So first:
   - Acquire it on `backup-1` as the follow does (BACKUP_R2.md, the follow's step 3): `/usr/local/libexec/deedkit/attest-verify`
     (empty environment) verifies the attestation of `ghcr.io/gosync-inc/deedkit-backup@sha256:<digest>` for `S` (the digest of
     the image `S`'s `publish` job pushed; the same digest `production.py` resolved on `prod`), then `docker pull
     ghcr.io/gosync-inc/deedkit-backup@sha256:<digest>` (the package is public: anonymous pull; if the registry asks for
     credentials, stop and ask the orchestrator, never put a token on `backup-1` for this). Needs the verifier's `reserve_bytes`
     (2 GiB) plus 4 GiB free on the Docker data root.
   - Read back on `backup-1`: `docker image inspect -f '{{.Id}}'` equals the attested digest (config digest; manifest digest under the
     containerd image store, BACKUP_R2.md), its `org.opencontainers.image.revision` label is `S`, and the same ID is the ID that
     `prod`'s `deedkit-production-backup:S` has.
   Only then, if the current pins predate 728: `python -m messaging_service.backup_r2_verifier --update-pins
   --helper-image <that image ID> --helper-revision S`. It self-tests all three profiles on synthetic bundles under the host lock
   and moves the pins of all three configs together, or none (production.md/BACKUP_R2.md "Two verifiers on one receiver"). Keep
   `pins-previous.json`. Read back that all three configs show the same pair. A new pin pair is a change of the two existing
   instances too: stop their timers again for this item (same rule as item 1: services inactive, no `active.json`) and restore
   their captured states after the readback.
7. **Rehearsal on a synthetic archive (before any real data), in separate isolated scenarios.** Each scenario has its **own**
   dedicated synthetic prefix, throwaway config, receiver state and verifier directory (BACKUP_R2.md "Scheduling and enrollment":
   the server-native synthetic drill), real age encryption and the pinned images; scenarios never share a protected point, a retry
   journal or a receiver quota, because a refused bundle stays beside the protected object and its verifier retry journal survives
   (a later upload would hit `receiver_quota`, `backup_r2_receiver.py:763`); the journal is never deleted to make a scenario pass.
   Make each synthetic bundle with `synthetic_archive` + `create_recordings_snapshot` + `encrypt_snapshot` from the installed
   wheel, deliver it through the forced SSH route with a throwaway key, and run the verifier on it. Receipts hold statuses only.
   Scenarios (each ends with its own removal of its exact owned objects, directories and config; never a bucket-wide delete):
   - **S1 enrollment and growth:** (a) the six checks pass and the first point is protected (`protect` path); (c) a grown second
     bundle rotates to two points then one; (e) the scratch (`payload-*`, `secrets-*`) is gone after success.
   - **S2 rejection:** on a fresh prefix and state, a first synthetic point is protected, then a bundle with one transcript emptied
     is refused `archive_content_lost`; the first point and its object stay; the refusal and its retry journal are kept as the
     evidence of the scenario and the scenario ends there (no third upload).
   - **S3 restarts:** on a fresh prefix and state, the verifier is killed (`systemctl kill`) mid-run and re-run, and the receiver
     process is restarted between delivery and verification (as the server-native drill's restart cases), each ending in the same
     protected point; the scratch is gone after the failure and the restart.
   - **S4 drill commands:** the commands of milestone D run end to end on S1's protected point (download, decrypt in the pinned
     image, bundle verify, readback, facts compare, cleanup), so D uses only rehearsed commands.
   - **S5 shared pins:** the three profiles keep passing on the shared pins.
   Only the scenarios' own prefixes and directories are removed afterwards; the preserved S2 evidence is moved aside to
   `*.removed-<date>` (root `0700`) with the other rehearsal leftovers and named in the receipt.
8. **Units and the verifier-side checker.** Install `deploy/recordings-backup-freshness.py` on `backup-1` as
   `/opt/deedkit-backup-r2/recordings-backup-freshness.py` (root, `0755`); this is the checker of the *verifier's* directory
   (`--verifier-directory`) and runs only here. Install `deploy/deedkit-recordings-backup-r2-verifier.service` and `.timer` into
   `/etc/systemd/system/` (`0644`), `systemctl daemon-reload`, `systemctl enable --now deedkit-recordings-backup-r2-verifier.timer`.
   The timer finds no candidate and writes `status.json` `idle`; that is correct before the first upload. Read back the checker
   before the first upload: `python3 -I /opt/deedkit-backup-r2/recordings-backup-freshness.py --verifier-directory
   /var/lib/deedkit-recordings-backup-r2/verification --max-age-hours 30` exits 4 (stale: no success yet), and exits 5 on a
   directory that does not exist.

Read-back **I1-read**: wheel profiles line (above); `ssh -G`/`sshd -T` for the user show the forced command only; the three verifier
timers active; `status.json` of the recordings instance `idle`; the messaging and calendar instances' next runs unchanged;
`instances.json` and the three `verifier.json` pins equal; `receiver_schema_max` unchanged.

Undo I1 (reverse order): `systemctl disable --now deedkit-recordings-backup-r2-verifier.timer` (let a running service end);
remove the unit files, the checker and `daemon-reload`; restore `instances.json.before-<date>` and re-run `--update-pins` only if the
pins changed in 6 and the previous pair is wanted (`pins-previous.json`); remove the `Match User` block and the `authorized_keys.d`
file, `sshd -t`, reload; move `/etc/deedkit-recordings-backup-r2`, `/srv/backups/deedkit-recordings` and
`/var/lib/deedkit-recordings-backup-r2` aside as `*.removed-<date>` (root `0700`, evidence, the recovery key included). The wheel
goes back to `wheel.before-<date>` (with `freeze.before-<date>`) only in this order: stop the two existing timers (and the
recordings timer, already disabled) and wait for all three verifier services to be inactive with no `active.json`; reinstall;
restore the captured timer states of item 1; read back both existing instances' next runs (an older wheel refuses nothing of the
other two profiles: it does not know `recordings`, which is why the unit files go first). The same applies when the undo is taken
from a failure inside item 1 (item 1's own recovery text). The R2 prefix holds only the rehearsal's removed objects.

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
   **The mount identity is proved here, not by the first skip** (the recordings CLI answers `archive_disabled` before it looks at
   any destination, `cli.py:17`): with `E` = the effective staging path (the `RECORDINGS_BACKUP_STAGING` value in the env file the
   recordings project reads, else `/opt/deedkit/recordings-backup/staging`), require all of: the container's mount of destination
   `/run/deedkit/backup-staging` is read-write and its `Source` (resolved) equals `realpath E`; `realpath E` equals the
   `staging` path of `runner.json` (item 3) and the path step I2.1 created and chowned. Any inequality stops the plan (make `E` and
   `runner.json` name one path; the compose side changes only through the release path, never by hand).
3. **Recipient and config.** `recipient` = the public age line from step I1.4 (`0600` root). `runner.json` from
   `deploy/backup-runner-recordings.example.json` (`0600` root): `image` = messaging's pin after step R (the same ID), `transfer.host` =
   the backup-1 tailnet address, `transfer.user` = `deedkit-recordings-backup`, paths as the example, `container` as P6 read it,
   `recordings.timers` = the real timer files of the other profiles. Check: `python3 -I /opt/personal-os-probe/ops/backup_runner.py
   --config /opt/deedkit/recordings-backup/runner.json --check` prints `{"status":"idle","profile":"recordings"}`.
4. **Producer-side freshness checker, then units.** The catch-up unit's second `ExecCondition` runs
   `/opt/deedkit/recordings-backup/freshness.py --status <status.json> --max-age-hours 22` and starts only on exit 4 or 5. The
   checker with that interface is the calendar's `deploy/calendar-backup-freshness.py` (it reads `last_snapshot_at` from the runner's
   `status.json`, which the runner writes for every profile at `finish`, `backup_runner.py:1097`); no new code. The verifier's checker
   `recordings-backup-freshness.py` takes only `--verifier-directory` (exit 2 on `--status`) and is **not** the one for this unit, so it
   is not installed on `prod`. **Before any timer is enabled**: install `deploy/calendar-backup-freshness.py` as
   `/opt/deedkit/recordings-backup/freshness.py` (root, `0755`; a missing file exits 2, which the unit reads as "do not start"), and
   read back the condition's three outcomes with the exact unit command on a copy of the status path: a status file with a
   `last_snapshot_at` of now exits 0 (`fresh`; condition false), one with a time 23 hours old exits 4, a missing path exits 5 (both start
   the catch-up). In the pre-activation state `status.json` has no `last_snapshot_at` (archive off): the checker exits 4, so
   the 23:15 catch-up starts and ends with the same `recordings_skipped_archive_disabled`; that is expected and harmless. Then
   `deploy/deedkit-recordings-backup.service`, `.timer`, `-catchup.service`, `-catchup.timer` into `/etc/systemd/system/` (`0644`),
   `daemon-reload`, then `systemctl enable --now deedkit-recordings-backup.timer deedkit-recordings-backup-catchup.timer`.
5. **First run, archive off.** Do not wait for 21:30: `systemctl start deedkit-recordings-backup.service` once, in an allowed window
   and not within 15 minutes of another profile's timer (the runner decides; a `recordings_skipped_window` is also a valid
   answer, repeat later). The run reaches the container: its `docker exec ... --backup-to` answers `archive_disabled`, which proves the
   container name and the config path (the mount was proved in item 2). Expected: exit 0, `status.json` of the recordings job
   `state: skipped` with `recordings_skipped_archive_disabled`, staging empty, no worker container, no bundle, nothing delivered.

Read-back **I-read** (milestone I verified): the file modes (`stat`), the `--check` line, the skip status and code, `ls -A staging`
empty, `systemctl list-timers 'deedkit-recordings-*' 'deedkit-calendar-*' personal-os-backup.timer` listing every timer with
unchanged next elapse for the other two profiles, and the next messaging and calendar dailies reaching `state: ok` on their usual
times. The recordings-api health and `archive_status` (`archive disabled`) unchanged. Record the receipt. Only now may milestone A start.

Undo I2: `systemctl disable --now deedkit-recordings-backup.timer deedkit-recordings-backup-catchup.timer`; let a running
`deedkit-recordings-backup.service` end or `systemctl stop` it (its `ExecStopPost` recovers); go on only when it is inactive, no
`deedkit-recordings-backup-*` container exists, `/opt/deedkit/recordings-backup/state/.unsettled` and
`/opt/personal-os-probe/backups/.runner.lock.unsettled` are absent and `--check` prints `idle`; remove the units and
`/opt/deedkit/recordings-backup/freshness.py` (it goes with the directory below), `daemon-reload`;
move `/opt/deedkit/recordings-backup` aside (`*.removed-<date>`, root `0700`, keep its journal and status). A failed check stops the
undo. Messaging's and the calendar's files are not touched. The staging mount stays (harmless without the timer).

## Milestone A. Bounded activation (archive on)

Applied by the ops step of this plan; the agent only checks the owner's key and file modes, and starts no provider session.

1. **Preconditions.** I-read recorded; the Omi key in place (`omi.key` mode `0600`, owner 10001, non-empty: modes only); no call; not
   within an hour before 21:30 UTC; `archive_status` for the Omi account answers (archive disabled) with an operator backend key.
2. **Namespace.** For each account to archive generate a fresh 32-hex `archive_namespace` once (`python3 -I -c "import secrets;
   print(secrets.token_hex(16))"`; record the first 6 characters in the receipt, not the value elsewhere). Omi first; PLAUD only
   if its key is in place and the owner's 11-03 words cover it (they cover Omi and PLAUD text, not audio).
3. **Config.** Keep an **immutable pre-activation copy**: `cp -p config.json config.json.pre-activation` inside the private
   directory (mode kept; the command is `test ! -e` guarded, and the file is never overwritten, moved or edited afterwards). Every
   later change (this item, the sizing of item 5) additionally keeps its own copy `config.json.before-<date>-<n>` (`n` counts the
   changes of the day, also create-only). Write the new
   document to a `.new` file in the same directory (`0600`, owner 10001) from `deploy/recordings-archive.example.json`'s shape: the
   `archive` block (`directory: /data/archive`, `poll_seconds: 300`, `max_bytes: 4294967296`, `free_reserve_bytes: 1073741824`,
   `omi_budget: {transcript_per_hour: 18, reads_per_hour: 45}`) and the `archive_namespace` of the chosen accounts; **no
   `event_router` block** (ANYM-732/733 own it). Validate with the image's own loader before replacing (the image's `ENTRYPOINT` is
   `deedkit-recordings`, `services/recordings/Dockerfile:12`, so the interpreter is named with `--entrypoint`): `docker run --rm --network none
   --read-only --user 10001 -v <private dir>:/run/deedkit/recordings:ro --entrypoint python <recordings image of S> -I -c "from deedkit_recordings.security import
   read_document as r; d,_=r('/run/deedkit/recordings/config.json.new'); print(d.archive is not None, len(d.accounts))"` must print
   `True <n>` (it prints no value of the file); then `mv config.json.new config.json` (atomic replace inside the directory).
4. **Start.** The collector reads the configuration each pass; read `archive_status` after one `poll_seconds` plus a minute. If the
   traversal has not started, restart **only** `recordings-api` (`docker compose $F restart recordings-api`; admission re-checks the
   egress rules) and read again. The first start applies schema 3 (additive, single writer).
5. **Sizing, after the first hour.** Read `archive_status` (`traversal.discovered_total`, `projected_db_bytes`, `capacity_warning`,
   `usage`). Set `max_bytes = 3 x projected_db_bytes` (plus `free_reserve_bytes` of free space) by a **new** create-only copy
   `config.json.before-<date>-<n>` and the same validate-then-atomic-replace of item 3 (`config.json.pre-activation` is not touched);
   if `capacity_warning` shows or the free space on the archive volume is below the reserve plus one expected archive, stop polling
   (Undo A) and report.

Read-back **A-read**: `archive_status` shows the traversal advancing, `usage` within the Omi share (the aggregate stays under 45/h
for this collector so agents keep the rest), `db_bytes` below the ceiling; `docker stats --no-stream` of the container within its limits;
the on-demand tools still answer for the existing principals (a read-only `recordings_capabilities`, no content request); the gateway
policy revision unchanged; no new principal or grant; the egress admission still passes (`docker logs` shows no `exit 78` loop).

Undo A: put back **`config.json.pre-activation`** (the archive-off document of item 3, copied back by `cp -p` to `.new` and an atomic
`mv`; never a `before-<date>-<n>` copy, which may already be an enabled document) or remove the top-level `archive` and every
`archive_namespace`; the service stops new collection (README "Optional private text archive"); stop only `recordings-api` if the
collector must end at once. **Verify that collection stopped**: after one `poll_seconds` plus a minute `archive_status` answers
`archive disabled` (or its traversal counters stop moving between two reads a minute apart) and the container's log shows no
poll. If it still collects, restart only `recordings-api` and read again; an undo that leaves polling on is not done.
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
marker present and naming that backup, exactly one **ciphertext object** under `<prefix>/objects/` (the protected backup also has
its receipt under `receipts/`; count the two prefixes separately, `ls`/`find ... | wc -l`, never `grep -c`, whose exit 1 on zero
matches stops a `sh -e` read-back), `continuity.json` present with `run` equal to the verification id and a **structure check**
instead of a word search: the verifier's own `backup_r2_verifier.read_baseline` (called from the installed wheel on an open
descriptor of the verification directory, root) returns a baseline whose `run` equals the verification id and whose `facts` pass
`backup_recordings_continuity.validate_facts` (exact keys, counts and hash strings only); the read-back prints `ok` or the refusal
code, never the content, staging on `prod` empty, no `payload-*` or `secrets-*` left on `backup-1`, and (on
`backup-1`, where the verifier's directory is)
`python3 -I /opt/deedkit-backup-r2/recordings-backup-freshness.py --verifier-directory /var/lib/deedkit-recordings-backup-r2/verification
--max-age-hours 30 --state <state file>` printing `fresh` with the bytes and identity (exit 0). On `prod`, the catch-up condition's
checker (`freshness.py --status ... --max-age-hours 22`) now exits 0 with the new `last_snapshot_at`, so the 23:15 catch-up does not
start. The messaging and calendar dailies of that day are `ok`.
A refusal instead (`archive_content_lost`, `archive_identity_changed`, any helper code) is not retried blindly: read the status, keep
the evidence, stop polling if the cause is the archive (Undo A), report.

Undo V: nothing to remove on `prod`. If the first point must be withdrawn: `systemctl disable --now` the recordings verifier timer
and the two recordings producer timers, preserve `protected.json`, the proof, the journal and `continuity.json`, and move the
recordings prefix's single object aside only under a reviewed deletion operation (data deletion keeps its own gates); the archive
itself is untouched.

## Milestone D. The restore drill (closes the card)

Production-parity: the protected point from R2, decrypted with the instance's own recovery key, restored into a scratch directory,
checked, then removed. It never touches the live archive volume.

The drill is the manual twin of the verifier's scheduled restore, so it takes the verifier's admission instead of bypassing it
(`BACKUP_R2.md` "One heavy restore at a time"). Its commands are the ones rehearsed in I1.7 S4.

1. **Admission and bounds, on `backup-1` as root.** Stop the three verifier timers (record their states, restore them at the end
   and on every failure path), wait until the three verifier services are inactive and no `active.json` or `host_directory/
   admission.json` is pending. Then run everything below under the host lock in a bounded shell: `timeout 3600 flock --nonblock
   <host_directory>/host.lock sh -eu drill.sh` (`host_directory` from the three verifier configs, the shared value, default
   `/var/lib/deedkit-backup-verify`; `flock` is the primitive the verifier uses, `backup_r2_verifier.py:248`). Check the free space on
   the filesystem of the scratch against `3 x` the protected object's size plus the verifier's `reserve_bytes`, and the Docker
   data root has the reserve; otherwise stop. The scratch is `/var/lib/deedkit-recordings-backup-r2/drill-<date>` (root `0700`).
   **Failure cleanup** (a `trap` in `drill.sh` and the abort rule): remove only the exact container `deedkit-drill-recordings-<date>`
   (`docker rm` by that name; started with `--rm`, so `docker stop`) and the exact scratch directory (`rm -r` of the path, cwd outside
   it), then restore the timers; evidence of the failure (the receipt lines) is kept.
2. **Download, decrypt, verify.** Download the protected object by its logical id with the instance's `operator.json`
   (`backup_r2_receiver ... download --id <id> --output <scratch>/candidate.tar.age`, BACKUP_R2.md "Commit, download and independent
   restore"; the download verifies the full ciphertext size and SHA-256 and publishes atomically), compare them with the receipt.
   Then one throwaway container of the **backup image pinned for the instance** (`messaging_service` is in it; the recordings image
   is not and is not needed), with the **scheduled helper's isolation and resource limits** (`backup_r2_verifier.py:596-619`):
   `docker run --rm --name deedkit-drill-recordings-<date> --network none --log-driver=none --restart=no --cpus=1 --pids-limit=256
   --memory=512m --memory-swap=512m --read-only --cap-drop=ALL --security-opt=no-new-privileges --tmpfs /tmp:<the
   `HELPER_TMPFS['/tmp']` options, `rw,noexec,nosuid,size=64m` at S, read from the installed wheel> --user <the verifier's helper
   user> --mount type=bind,src=<scratch>,dst=/drill --mount type=bind,src=<identity>,dst=/identity,readonly --entrypoint python
   <backup image> -I -c <code>`. The temporary file `decrypt_snapshot_stream` opens (`backup_crypto.py:225`) lands on that private
   `/tmp` tmpfs, which is mandatory, not optional. The image has `ENTRYPOINT []` (`services/messaging/Dockerfile:202`) and copies
   `messaging_service` into `/app` without installing it, while `python -I` leaves `/app` off `sys.path`: the code therefore begins
   `import sys; sys.path.insert(0, '/app')` (the one trusted path; the scratch is never on it). `age` is the image's own
   (`/usr/local/bin/age`); `backup_crypto.decrypt_snapshot_stream(source, identity, destination, age='age', max_bytes=...)` takes an
   explicit `max_bytes` bound, and `archive_facts` is bounded by the object size. If a container limit is hit, the drill ends with
   its exit code and the failure cleanup of item 1; the limits are never raised for the drill. The invocation is the one S4 of I1.7
   rehearses. The code, in this order: decrypt and extract into
   `/drill/bundle/` **preserving the layout** (`manifest.json`, `state/archive.sqlite`, the two members of `MEMBERS`);
   `backup_recordings.verify_recordings_bundle('/drill/bundle')` (structure, hashes, schema, `integrity_check`, identity);
   `backup_recordings_continuity.readback('/drill/bundle/state/archive.sqlite')` (a full-text search on a synthetic query and a
   context read of the three newest records that hold text, checked against the index rows: this is the search/context assertion, it
   returns nothing readable and raises `readback_failed`); `backup_recordings_continuity.archive_facts(...)` for the hash-only
   facts. It prints only `ok` lines, counts, identity counters and hashes, never a text.
3. Compare: `archive_id`, `incarnation`, `sequence` and `records` from the restored copy equal the manifest and the proof's;
   `continuity.json`'s facts equal the restored copy's facts (hash for hash).
4. Record the receipt (the backup id, ciphertext hash, sizes, the identity line, the four check results, the times). Remove the
   scratch directory (`rm -r` with the exact path, cwd outside it) and read back that it is gone; the recovery key and every other
   file are untouched.
5. **Restore procedure for a live volume (documented here, not executed by this plan).** Stop only `recordings-api`; move the live
   archive directory aside whole (`archive.removed-<date>`, kept as evidence); create the new directory **as owner 10001** (`install -d
   -o 10001 -g 10001 -m 0700`; root-created, the UID-10001 store rejects it) and place the restored `archive.sqlite` in it (`0600`,
   `10001:10001`), with **no** `journal.marker`; start the service. What the restart does depends on the feed, and both cases are
   read back (`archive_status` identity, never content):
   - **This plan's state (no `event_router` block).** `maintain_feed` returns at once when `doc.event_router is None`
     (`archive.py:395`): the restart neither keeps nor writes a marker and bumps no incarnation. The restored database keeps the
     snapshot's own `archive_id`, `incarnation` and `sequence`; read the identity back and record it (expected: equal to the
     manifest's, not a bumped one; a different value stops the procedure).
   - **With the feed enabled (ANYM-732/733).** A missing or mismatched marker bumps the incarnation to `max(database, marker, epoch,
     clock)+1` and, when the existing epochs require it, gives the journal a new `journal_id` (README "Identity"), so a restored archive
     never reuses event identities; the module router then reports a source reset and waits for its operator (`set-module-cursor`).
   The next backup judges the candidate against the baseline (`backup_recordings_continuity.judge`). With an **equal** incarnation a
   lower `sequence` is refused `archive_sequence_lower` (a restore from an older point; without the feed, the restored database
   keeps the snapshot's incarnation, so an older restore lands here); a **lower** incarnation is refused `archive_incarnation_lower`;
   a higher incarnation with a lower sequence is not refused by that rule and passes if the remaining continuity checks pass (lost
   parts, dropped tombstones and the others are still refused). The
   operator, after reading the refusal and keeping its evidence, may re-baseline with a reasoned `--rebaseline` (BACKUP_R2.md). Nothing
   is re-baselined by this plan.

Read-back **D-read**: the receipt lines above, the absence of the scratch and of the container `deedkit-drill-recordings-<date>`, the
host lock free, and the three verifier timers back in their captured states with their next runs scheduled. Undo D: the drill changed
no data; if it stopped midway, remove only the exact container and scratch directory of item 1 and restore the timers.

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
   synthetic rehearsal of I1.7 (scenario S3) stands in and the receipt says so.
6. The catch-up unit's freshness check reuses the calendar's `deploy/calendar-backup-freshness.py` (same `--status` interface and
   `last_snapshot_at` field) instead of a new recordings-specific producer script: no code change in this PR.
7. The drill is a manual run under the verifier's host lock with the three timers stopped (no new tool); a scripted drill mode in
   the verifier is a possible follow-up card, not part of this plan.
6. The plan leaves ANYM-730 (deletion, tombstones) and ANYM-732/733 (the event router block) out; the tombstone contract
   (`tombstones(provider_hash)`, SHA-256 of namespace NUL provider_id) is in BACKUP_R2.md.
