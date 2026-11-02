# Production enablement of local media understanding (ANYM-686 plan)

Status: plan for review (Astra r1, one recheck). No step runs without the plan's APPROVE.

What it does: the owner's voice notes, photos and document text are processed by `deedkit-inference` on the
Mac mini (`enricher-mini`, profile `enrichment-mini`) for the owner's messenger accounts. Authority: owner
11-01 «2 b» and «инфру сделал чтоб могли все пользоваться»; owner 10-19 (enable by default); AGENTS.md section 2
and harness process.md section 2 gates. No send, call, ACK, pairing, provider-session or allowlist change is
part of it, and none may be made to test it.

Rules for every step: no step during an active call (the read-only active-call check of production.md, before
the step and again before steps 6 and 8); the first failed check stops the plan and the step's undo runs;
secrets (the key, DSNs) are never printed, logged or put in a card; nothing runs with `-v`. `S` is the
production SHA, `F` the project's Compose arguments plus `-f compose.enrichment.yaml`.

**Gate G1 (owner's Tailscale token pending):** steps 4 onward need production to reach the mini on 443, which
needs the tailnet policy of inference-mini-setup.md section 10 (or the recorded "policy does not restrict the
mini"). Until G1 the plan stops after step 3.

## 0. Pre-check (read-only)
- `production.py status`: `S` and schema. Evidence on 11-02 22:06Z (deployed snapshot): `S` = b4a9ab5f, schema 64,
  an ancestor of the ANYM-685 squash d9866f94, so it lacks `deedkit_inference` and `enricher-mini`: step 1 is
  needed. Re-read it at execution time; if `S` already contains c4ccecbc (ANYM-688), skip step 1.
- ANYM-682 to 684 installed on the mini with their `[open: measured]` evidence on their cards; the 24-hour
  record of ANYM-684 criterion 10 meets its target (open at least 60% of a weekday, no closed stretch over 3
  hours) or its thresholds are adjusted from it.
- The `messaging_enricher` login and `private/enrichment-local.dsn` exist and `private/processing-locks` is
  writable by uid 10001: `ls -ln`, and the role read through the host's documented database access.
- The owner's messenger accounts: `SELECT id FROM accounts ORDER BY id` through the same access; the owner
  confirms the list is theirs only (card criterion 4c: no other account).
- Nothing already runs: `docker compose $F ps` shows no `enricher-mini`; `private/enrichment-mini.json` and
  `private/inference/` do not exist, or are the previous attempt's, which step 9's undo removed.

## 1. A release containing ANYM-685 and ANYM-688
Normal route only (`production.py`, autodeploy or the by-hand next green `main` SHA, production.md). Check:
`production.py status` shows a `current` SHA that contains c4ccecbc, the enrichment workers that run today
unchanged (`enricher-local`'s state as before; this plan starts none). The release does not need
`compose.enrichment.yaml` selected: `enricher-mini` starts only in step 8. Undo: the release's own rollback
(production.md "Roll back the current release"). No schema change is in this range; if one appears, the
migration risk tiers of production.md apply first.

## 2. Role and DSN (only if the pre-check found them missing)
Apply `messaging_service/enrichment_role.sql` as the database owner and create the login per ENRICHMENT.md
"Isolation and limits", with the backup and undo the section 2 gates ask for (fresh verified backup of the
affected data, `DROP ROLE` and removing the DSN file as undo). The login's DSN goes to
`private/enrichment-local.dsn` (mode 0600, owner uid 10001). Skipped when present.

## 3. Host rules and networks (ANYM-688, no mini reachability needed)
Steps 1 to 3 of production.md "Mini enrichment worker": `inference_egress_check.py rules --mini-ip <ip>` applied
as root and saved; `up --no-start` of `enricher-mini` (networks, stopped worker); `docker network connect --alias
db` for the database (no restart). Check: `iptables -S DEEDKIT-INFERENCE`, `iptables -t raw -S PREROUTING`, the
two networks exist, `db` has the third network, `docker compose $F ps db` shows the same container start time.
Undo: that section's "Undo (reverse)" (`rules --undo`, disconnect, remove networks and the stopped worker).
**Stop here until G1.**

## 4. Tailnet policy (inference-mini-setup.md section 10; the owner's token)
Executed by the owner's token holder or an agent with the token. Check: its after-matrix; production reaches
the mini's `/health` with a verified certificate (`curl --max-time 10 https://<mini>.<tailnet>.ts.net/health`,
a plain GET). Undo: the saved `policy.before.hujson`.

## 5. Key delivery (inference-mini-setup.md section 11)
Stage, hash-compare, probe the new file from production, keep `.prev`, rename; the directory is mode 0700 and
owned by uid 10001, `inference.key` mode 0600 (`ls -ln`). Check: `inference_probe.py --key-file
private/inference/inference.key` prints `reachable=yes` and `key_days_left=`. Record the key's expiry and the
rotation due date (30 days before) on the card. Undo: `mv inference.key.prev inference.key` or remove the
directory's key; the mini's copy is deleted only after step 8's read-back.

## 6. Egress check (ANYM-688 receipt)
Production.md "Mini enrichment worker", step 4: the check in the probe container, whose JSON, the `tcpdump`
evidence and the positive control are recorded. The database and the mini on 443 are open; the API container (by
alias and address), the Docker host, a public address on 443 and 53, another tailnet node, the mini's other
ports and an external name are blocked. A failure stops the plan. Undo: step 3's.

## 7. Config (disabled first)
`private/enrichment-mini.json`, mode 0600, owner uid 10001, from `deploy/enrichment-mini.example.json`, with
`"enabled": false`: `accounts` exactly the list of step 0, profiles `audio` (`audio-default`,
`timeout_seconds` 900), `image` and `document` (`vision-default`, 300), no `language`, `base_url` the mini's
`.ts.net` name (equal to `INFERENCE_MINI_NAME`), `api_key_file` `/run/secrets/inference/inference.key`,
`max_jobs_per_day` 2000, `profile_max_jobs_per_day` audio 500, image 1500, document 500, `backpressure_seconds`
120. `INFERENCE_MINI_NAME` and `INFERENCE_MINI_IP` go into the project's env file (backup first). Check:
`python3 -m json.tool`, no `allow_cloud` key, `ls -ln`. Undo: delete the file and restore the env file.

## 8. Enable and promote
1. Set `"enabled": true` (the moment the worker may claim work); `docker compose $F --profile enrichment-mini up
   -d --no-build --pull never enricher-mini` (enroll tag, as production.md describes). Check: the container
   runs with no restarts; the worker logs no `invalid_model_profile`.
2. Select it for promotion: `files` and `services` in `/etc/deedkit/production.json` and
   `/etc/deedkit/http-production.json` (backups first), then the by-hand apply of the next green `main` SHA (the
   autodeploy pause is expected), then remove the pause.
3. Read back: `SELECT account, kind, enabled, pause_reason FROM processing_profiles` shows the three profiles per
   listed account enabled, none paused; `processing_control` has no `model_auth_failed` row; delete the mini's
   copy of the key (section 11 of the mini runbook).
Undo: `"enabled": false` or move the config aside, `docker compose $F stop enricher-mini`, restore the
configuration backups, `rules --undo`. Nothing is sent, called, paired or allow-listed; queued jobs stay
queued and drain when the worker returns.

## 9. Evidence runs (card criteria 5 to 7)
Real archived attachments only; no message is sent. (5) A voice note, a photo and a document reach `ready` with
`model_generated` and provider `deedkit_inference`; the photo's title and summary are in Russian; on the mini
the log shows `messaging-enrichment`, alias, bytes, seconds and `200`, and `work/` is empty. (6) Offline drill:
the mini's service is stopped with its `launchctl bootout` (Tailscale stays up) and started again, once also by
a real `inference_deploy.py` restart: within one cooldown the three profiles show `model_backpressure`;
`enrichment_budget` and `attempts` unchanged; then the jobs finish by themselves. (7) `between: true` in the
busy file holds a job (`admission_closed`) until cleared; a reboot of the mini, at a time with no job running,
brings the service back. Only the mini's own service and busy file are touched; `production.py status`, the
collector heartbeats and the backup status are read before and after. Undo: start the service, clear the busy
file.

## 10. Watch and records
production.md "Mini inference: watch and enable record" is the watch step. The card records the three
attachment ids (never content), the commands, the dates, the key expiry and the rotation due date.

## Open points for the reviewer
- No standalone read-only active-call command exists outside `apply`; the plan uses "the read-only
  active-call check of production.md" before each step. A follow-up card could add one.
- Step 8.1 enables before step 8.2 promotes: between them the running worker is the enroll tag of the same
  image; the by-hand apply replaces it with the release image of the same target.
