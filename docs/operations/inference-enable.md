# Production enablement of local media understanding (ANYM-686 plan)

Status: plan (no review: owner 11-11). Each step keeps its read-back and undo.

What it does: the owner's voice notes, photos and document text are processed by `deedkit-inference` on the
Mac mini (`enricher-mini`, profile `enrichment-mini`) for the owner's messenger accounts. Authority: owner
11-01 «2 b» and «инфру сделал чтоб могли все пользоваться»; owner 10-19 (enable by default); AGENTS.md section 2
and harness process.md section 2 gates. No send, call, ACK, pairing, provider-session or allowlist change is
part of it, and none may be made to test it.

Rules for every step: the active-call gate below passes immediately before the step (also before every undo
and drill); the first failed check stops the plan and the step's undo (the stage table) runs; secrets (the key,
DSNs) are never printed, logged or put in a card; nothing runs with `-v`. `S` is the production SHA, `F` the
project's Compose arguments plus `-f compose.enrichment.yaml`. The database container is never recreated: no
`up` without `--no-deps`, and its container ID and start time are recorded at step 0 and compared after steps
5, 6 and 7.

**Active-call gate (run as root on the host, bounded, fails closed).** It runs the existing calls probe of the
installed host tool, read-only: `python3 -I -c 'import sys; sys.path.insert(0, "/usr/local/lib/deedkit"); import
production as p; found = p.active_calls(p.config(p.CONFIG)); print("active calls:" if found else "no active call",
found); sys.exit(1 if found else 0)'`. It exits 1 with the call IDs when a call is active and exits non-zero
with a traceback when the probe cannot read (a missing mount, a timeout): both mean stop. Step 0 runs it once
to prove it works (it must print `no active call`).

**Gate G1 (owner's Tailscale token pending):** production must reach the mini on 443, which needs the tailnet
policy of inference-mini-setup.md section 10 (or the recorded "policy does not restrict the mini"). Nothing on
the host or in the database changes before G1 (step 1) passes: no release, no role, no DSN, no file, no rule.

## 0. Pre-check (read-only)
- `production.py status`: `S` and schema. Evidence on 11-02 22:06Z (deployed snapshot): `S` = b4a9ab5f, schema 64,
  an ancestor of the ANYM-685 squash d9866f94, so it lacks `deedkit_inference` and `enricher-mini`: a release
  is needed (step 2). Re-read at execution time; if `S` already contains c4ccecbc (ANYM-688), skip step 2.
- The active-call gate once (it proves itself); `docker inspect` of the database container: ID and start time.
- ANYM-682 to 684 installed on the mini with their `[open: measured]` evidence on their cards; the 24-hour
  record of ANYM-684 criterion 10 meets its target (open at least 60% of a weekday, no closed stretch over 3
  hours) or its thresholds are adjusted from it.
- The `messaging_enricher` login and `private/enrichment-local.dsn` exist and `private/processing-locks` is
  writable by uid 10001: `ls -ln`, and the role read through the host's documented database access.
- The owner's messenger accounts: `SELECT id FROM accounts ORDER BY id` through the same access; the owner
  confirms the list is theirs only (card criterion 4c: no other account).
- Nothing already runs: `docker compose $F ps` shows no `enricher-mini`; `private/enrichment-mini.json` and
  `private/inference/` do not exist, or are a previous attempt's, which the stage table removes.

## 1. Tailnet policy (G1; inference-mini-setup.md section 10, the owner's token)
Check: the after-matrix; production reaches the mini's `/health` with a verified certificate (`curl --max-time
10 https://<mini>.<tailnet>.ts.net/health`, a plain GET). Undo: the saved `policy.before.hujson`.

## 2. A release containing ANYM-685 and ANYM-688
Normal route only (`production.py`, autodeploy or the by-hand next green `main` SHA, production.md). Check:
`production.py status` shows a `current` SHA containing c4ccecbc, and the running services as before (this
plan starts no worker here; `compose.enrichment.yaml` need not be selected yet). The release does not change
the database container. Undo: the release's own rollback (production.md "Roll back the current release"). No
schema change is in this range; if one appears, the migration risk tiers of production.md apply first.

## 3. Role and DSN (only if the pre-check found them missing)
Apply `messaging_service/enrichment_role.sql` as the database owner and create the login per ENRICHMENT.md
"Isolation and limits", with the latest daily backup as restore point and an undo (a role change rewrites no
row, so no fresh backup, owner 11-04; undo `DROP ROLE` and removing the DSN file). The DSN goes to `private/enrichment-local.dsn`
(mode 0600, owner uid 10001). Skipped when present.

## 4. Key delivery (inference-mini-setup.md section 11)
Stage, hash-compare, probe the new file from production, keep `.prev`, rename. This creates
`private/inference/` (mode 0700, uid 10001) with `inference.key` (0600). The probe is run with its URL: `python3
scripts/inference_probe.py --url https://<mini>.<tailnet>.ts.net --key-file private/inference/inference.key`
(the script has no default URL here) and must print `reachable=yes status=ok` and `key_days_left=`. Record the
key's expiry and the rotation due date (30 days before) on the card. Undo: `mv inference.key.prev
inference.key`, or remove the key and the directory if there was none. The mini's copy is deleted after step 7's
read-back.

## 5. Staged files, environment and the enrollment image (nothing started)
- `INFERENCE_MINI_NAME` and `INFERENCE_MINI_IP` in the project's env file (backup first).
- `private/enrichment-mini.json`, mode 0600, owner uid 10001, **`"enabled": false`**. The complete structure:
  `accounts` exactly the list of step 0; `max_jobs_per_day` 2000; `profile_max_jobs_per_day` `{"audio": 500,
  "image": 1500, "document": 500}`; **no `account_max_jobs_per_day`** (the template's placeholder key is not an
  account in `accounts`, and the parser rejects it); `profiles` `audio` (`audio-default`, `timeout_seconds` 900),
  `image` and `document` (`vision-default`, 300), no `language`, each with `"backpressure_seconds": 120`,
  `provider` `deedkit_inference`, `local_inference` true, `base_url` the mini's `https://<name>.ts.net/v1` (the
  name of `INFERENCE_MINI_NAME`), `api_key_file` `/run/secrets/inference/inference.key`, no `allow_cloud`.
- The enrollment image of `enricher-local` at `S`. A selected service must run before `prepare` accepts it, so it
  is pulled the way `prepare` does, without selecting anything: `/usr/local/libexec/deedkit/attest-verify --image
  ghcr.io/gosync-inc/deedkit-enricher-local --sha S` (empty environment) prints the verified `reference`,
  `digest` and `config_digest`; `docker pull <reference>`; check that the image ID equals the config or the
  manifest digest and that `org.opencontainers.image.revision` is `S`; then `docker tag <reference>
  anymsg-enricher-local:enroll-S` (a new tag, never a production tag).
Check: the file's mode and owner; `docker image inspect anymsg-enricher-local:enroll-S`. Undo: delete the file,
restore the env file, `docker image rm anymsg-enricher-local:enroll-S`.

## 6. Host rules, networks and the stopped worker (ANYM-688, steps 1 to 3)
`inference_egress_check.py rules --mini-ip <ip>` printed and applied as root, and persisted as production.md "Host
firewall persistence" says: the `deedkit-inference` block in `/etc/ufw/before.rules` and `before6.rules` (filter
table, host-bound drops) and `/etc/ufw/after.init` (raw table); the files are copied to `rules.before` first, and
`verify` for both scripts must print `ok`; `MESSAGING_VERSION=
enroll-S docker compose $F --profile enrichment-mini up --no-start --no-build --pull never --no-deps
enricher-mini`; `docker network connect --alias db <project>_inference_db <db container>`. Check: `iptables -S
DEEDKIT-INFERENCE`, `iptables -t raw -S PREROUTING`, both networks exist, the database container's ID and start
time are those of step 0 and `db` shows the third network.

## 7. Egress check, then enable
1. The ANYM-688 check in the probe container (production.md "Mini enrichment worker", step 4): the JSON, the
   positive controls and the DNS evidence as that section says. Failure: stop.
2. Parse the final config with the worker's own parser, without starting the worker: in the probe container (`run
   --rm -T --no-deps --entrypoint python enricher-mini -I -c ...`) copy the file to `/tmp` (tmpfs, mode 0600) with
   `enabled` true and call `messaging_service.enrichment.configuration()` on the copy; it must return the three
   profiles. (`json.tool` alone proves only JSON syntax.)
3. `"enabled": true` in the file, then `MESSAGING_VERSION=enroll-S docker compose $F --profile enrichment-mini
   start enricher-mini` (`start`, never `up`). Check: running, no restarts, no `invalid_model_profile` in its
   log; `SELECT account, kind, enabled, pause_reason FROM processing_profiles` shows three enabled, unpaused
   profiles per listed account; `processing_control` has no `model_auth_failed` row; the database container is
   unchanged; the key read as the worker (`test -r /run/secrets/inference/inference.key`) and the probe from inside it show
   `reachable=yes`. Then delete the mini's copy of the key (section 11 of the mini runbook).

## 8. Promote
Select the worker for the release tool: append `compose.enrichment.yaml` to `files` in
`/etc/deedkit/production.json` **and** `/etc/deedkit/http-production.json` (backups first), and add
`"enricher-mini": "enricher-local"` to `services` of **`production.json` only**: the HTTP configuration keeps
exactly the runtime API and the gateway (its validator refuses anything else). Autodeploy pauses on the Compose
change; apply the next green `main` SHA by hand as item 5 of the delivery enrollment does, then remove the pause.
Check: `production.py status` `current` is that SHA, the worker runs on the release image, the read-back of
step 7.3 repeats.

## Unwind by stage
Stop the worker before removing its confinement. Each row undoes everything up to that stage, newest first.

| reached | undo, in order |
|---|---|
| 8 (promoted, or the edit/apply failed) | `"enabled": false`, `docker compose $F --profile enrichment-mini stop enricher-mini`. **Hold state:** the stopped worker is still selected, so the release tool and autodeploy refuse ("each selected service must already have exactly one running container"); the autodeploy pause stays and nothing else is released. Restore both configuration backups. The receipts name the overlay, so the way out is a hand apply of a **newer green `main` SHA that has never been prepared** (`prepare` refuses an existing candidate directory, so never the SHA just applied; if none exists yet, stay in the hold state until the next merge). After a failed apply that left `attempted.json`, recover it first as production.md "Apply and verify" says. Then the stage 7 row |
| 7 (running) | `stop` then `rm` the worker; `"enabled": false`; stage 6 row |
| 6 (networks, rules) | `docker network disconnect <project>_inference_db <db container>`; `docker network rm` both; `rules --undo` (hooks first, then both chains), and restore `rules.before` (the saved `before.rules`, `before6.rules` and `after.init`) with `ufw reload`, then `verify`; check `iptables -S` is as before |
| 5 (files) | delete `private/enrichment-mini.json`; restore the env file; remove the enroll image tag |
| 4 (key) | restore `inference.key.prev` or remove the key and the directory; the mini's copy was still there until step 7 |
| 3 (role) | the role's `DROP ROLE` and DSN removal |
| 2 (release) | the release rollback |
| 1 (policy) | the saved `policy.before.hujson` |
Read back after any undo: `docker compose $F ps` (no worker), `iptables -S` equals the before state, the key
directory as expected, `processing_profiles` rows disabled, the database container unchanged. Queued jobs stay
queued and drain when a worker returns. Nothing is sent, called, paired or allow-listed at any stage.

## 9. Evidence runs (card criteria 5 to 7)
Real archived attachments only; no message is sent. Every drill is preceded by the active-call gate.
- (5) A voice note, a photo and a document reach `ready`: for each, `get_file_text` with `content='enriched'` and
  `search_files` (the existing read tools, through an existing read credential; no grant is created) return the
  transcript or description with the `model_generated` label and provider `deedkit_inference`; the photo's title
  and summary are Russian; the `events` row of its message (`file.transcript.ready` for the voice note) exists.
  On the mini the log shows `messaging-enrichment`, alias, bytes, seconds and `200`, and no content or file name;
  after the jobs, `find work -mindepth 1` is read recursively: no per-request directory remains, and no file is
  media or request text. The check: `file --mime-type` finds no `audio/*`, `image/*` or `video/*` file and
  no decoded or transcoded copy; `grep -rIlF` for three distinctive words of each returned transcript or
  description (the words are not written to the card) finds nothing; only markers, locks and the warm engines'
  `w-*` scratch remain, with names and sizes on the card.
- (6) Offline drill: the mini's service is stopped with its `launchctl bootout` (Tailscale stays up) and started
  again, once also by a real `inference_deploy.py` restart: within one cooldown the three profiles show
  `model_backpressure`; `enrichment_budget` and `attempts` are unchanged; then the jobs finish by themselves.
  `production.py status`, the collector heartbeats and the backup status are read before and after.
- (7) `between: true` in the busy file holds a job (`admission_closed`) until cleared. The mini reboot: in the
  window the owner names; no remote test is running or queued on the mini (`scripts/remote-test.sh` queue empty)
  and no Between VM is running (the setup runbook's warning); no job in flight. The service and the busy probe
  both come back by themselves (the busy file is fresh within 30 s) and the held jobs complete.
Undo: start the service, clear the busy file.

## 10. Watch and records
production.md "Mini inference: watch and enable record" is the watch step. The card records the three
attachment ids (never content), the commands, the dates, the key expiry and the rotation due date.

## Open points
- The active-call gate imports the installed host tool's own `active_calls` and `config`; step 0 proves that it
  runs on the host before any step depends on it.
- The enrollment image uses the tool's own verifier directly because `prepare` refuses a selected service with no
  running container; the digest and revision checks repeat what `prepare` does.
