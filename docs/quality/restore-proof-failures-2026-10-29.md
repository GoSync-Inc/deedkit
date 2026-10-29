# Restore proofs failing after 6-28 minutes (ANYM-633)

Question: why did four messaging restore proofs on the backup server fail mid-run on 2026-10-27/28,
and what fixes it? Read on 2026-10-29 11:40-11:50Z, read-only (criterion 1's list). Evidence is
reduced to codes, classes, frames, revisions and timestamps.

## Criterion 1: installed verifier and helper (read back 2026-10-29 11:40Z)

- Receiver venv: `anymsg 0.9.19`, installed 11:35:04Z from `release-cbb894101864`. The installed
  `backup_r2_verifier.py`, `backup_restore_worker.py` and `backup_r2_receiver.py` have the same
  SHA-256 as main `cbb89410` (`3c63e6d5…`, `4b22743d…`, `d0cd08c1…`).
- `verifier.json`: `helper_revision` `cbb894101864fbe8e67845291319c2ca7f2e5cfc`, `helper_image`
  `sha256:9faf8810…`. The image's `org.opencontainers.image.revision` label is the same SHA.
- Both include the failure diagnostics. ANYM-285 #332 added `diagnostic` in `status.json` and
  `last-failure.json` and the stream resume. ANYM-603 #369 added the helper `failure.json` after a
  broken pipe. ANYM-285 #381 added `failure-detail.json`.
- Observation window: 2026-10-29 11:40Z to 2026-11-12 11:40Z.

## The failures

| Run (start → failure, journal) | Length | Unit CPU | Receiver wheel / helper pin | Helper exit (`docker events`, A8) |
|---|---|---|---|---|
| 10-27 08:20:03 → 08:34:11 | 14 min | 1 min 50 s | release-045 kit / `c3efbf1` | not recorded |
| 10-27 08:39:27 → 09:07:24 | 28 min | 1 min 2 s | release-045 kit / `c3efbf1` | not recorded |
| 10-27 19:05:01 → 19:11:46 | 7 min | 54 s | release-045 kit / `c3efbf1` | 130 at 19:11 |
| 10-28 02:02:56 → 02:11:51 | 9 min | 1 min 36 s | release-046 kit / `ad46e79` | 130 at 02:11 |

The pins are dated by the `verifier.json.before-<sha>` copies, which keep the old file's mtime:
`c3efbf1` from 10-27 06:56Z, `ad46e79` from 10-28 01:03Z, `c728894` from 10-28 15:08Z, and later
pins after that. Wheels by kit on the server: release-045 (10-27 02:32Z) and release-046
(10-28 01:02Z) have no stream resume (`STREAM_RESUMES`) and no `describe`. `recvupd` (10-28
15:07Z) is the first kit with both. The release workers' one-off diagnostic passes then verified
the same capture on retry: 045 at 10-27 09:49 (`ca36d92d…`), 046 at 10-28 02:58 (`93ea9813…`).
So the failure was transient, not in the data or the images.

The journal printed only `independent_restore_failed`, because `main()` at those revisions printed
that string for every exception, safe-coded or not (`ad46e79`, `main`). The line says nothing about
the failure's code. `last-failure.json` now holds a later
`receiver_busy` (10-28 18:57Z), not one of these four. Helper failure files are removed with the
payload. The journal records no R2 resume counts.

The many 1-second `independent_restore_failed` runs (10-27 07:32, 07:47, 08:04, 18:33, 18:49;
10-28 01:17-01:48, 11:18-13:08) are not mid-run failures. They failed before any stream, every
timer cycle, and printed the same generic string. Their codes are lost. They stop after ANYM-632,
which writes `busy` status for an upload's lock.

## What the evidence shows

Observations (read on the server, 10-29):

1. **The helper ended with 130 in two runs, in the minute the verifier failed.** The helper image
   is built `FROM` the official postgres image and keeps its `STOPSIGNAL SIGINT` (read back:
   `stop=SIGINT`). The helper runs as the container's PID 1 (`--entrypoint python`, no `--init`).
   PID 1 does not get SIGTERM's default action, and CPython exits 130 on SIGINT. The verifier's
   `except` path runs `cleanup_resources` → `docker container stop --time=10` (`Docker.remove`),
   which sends that SIGINT. Its `finally` sends SIGTERM to the attach client
   (`process.terminate()`); with sig-proxy that reaches the helper, which ignores it as PID 1. So
   130 fits a helper still running when the verifier failed and the cleanup stopped it. A stop by
   someone else, at that minute, is not ruled out. The minute match does not prove which side
   failed first.
2. **No OOM, no daemon fault, no service stop.** `journalctl -k` covers the whole window (since
   the 10-22 boot, 234 lines from 10-27 to 10-28 03:00) and has no OOM or killed-process line.
   `docker.service`/`containerd.service` log no error or warning between 10-27 07:50 and 10-28
   02:15. The verifier unit logs no stop, signal or timeout in that range, so systemd sent no
   SIGTERM. `docker events` no longer holds those days.
3. **No mid-run failure since the resume shipped.** The resume (bounded ranged GET,
   `STREAM_RESUMES` = 5, #332, merged 10-28 04:37Z) reached the receiver with the `recvupd` kit
   (10-28 15:07Z). Restore runs since then: 10-28 15:09 → 16:19 verified, 19:13 → 20:12 verified,
   10-29 09:27 → 10:15 verified (`last-success.json` `a7aac368…`). The only failures in that time
   are three pre-stream `receiver_busy` (10-28 18:27-18:57). Three runs prove little, and a run
   that a resume saved looked exactly like a clean one.

Hypotheses the evidence leaves open (the release-045/046 code, `ad46e79`):

- (i) A broken R2 body read. That `verify_object` had no resume: a `body.read(MIB)` error from
  botocore/urllib3 (`ResponseStreamingError`, `ReadTimeoutError`) propagated raw. Fits
  observations 1 and 3 and #332's analysis.
- (ii) A `BrokenPipeError` on the helper's stdin after the `docker container start -ai` client
  ended while the helper lived.
- (iii) A safe-coded verifier failure mid-stream, hidden by the generic journal line:
  `restore_disk_capacity` from the reserve check (disk free at that time is not recorded), or
  `stored_backup_corrupt`.
- (iv) Another uncoded error in the stream loop, for example an `OSError` from
  `shutil.disk_usage` in the reserve check.
- For the two 10-27 morning runs, with no exit recorded: a helper failure of its own as well
  (`isolated_restore_failed` or a stage code), though OOM is ruled out by observation 2.

Ruled out: a systemd stop (observation 2), the job deadline (21,600 s against 7-28 min), and
kernel OOM. The card's resume-limit candidate could not fire, because no resume existed then.

## Outcome: (c) cause not determinable from the evidence; this PR adds what was missing

Most likely is (i), which #332 (ANYM-285) fixes, but nothing recorded then separates (i)-(iv).
Missing evidence and where it now comes from:

| Missing | Now recorded by |
|---|---|
| The failure's own code (not the generic journal line) | `error` in the journal line and `status.json` (ANYM-285 #332, installed) |
| The exception class and frames | `diagnostic` (ANYM-285 #332, installed) |
| The helper's failure code after a broken pipe | `failure.json` → `error` (ANYM-603 #369, installed) |
| Whether R2 still breaks under the resume, and the headroom to `STREAM_RESUMES` | `stream_resumes`: the ranged-GET attempts of the full stream, in a verified run's status and stdout line, and in a failure after the stream (this PR) |
| Whether the helper was still running, exited on its own (code), or was OOM-killed when the verifier failed | `helper_state` from Docker's own container state, read before cleanup, on any failure (this PR; not the attach client's return code) |

Each failure's fields also go to the journal line, so a later failure overwriting
`last-failure.json` loses nothing. A verified run's stdout line keeps its `stream_resumes`. How the
cases will show: (i) as `storage_unavailable` with an SDK class in `diagnostic`, or as non-zero
`stream_resumes` on verified runs; (ii) as `isolated_restore_failed` with `helper_state.status`
`running`; (iii) as its own code; (iv) as its class in `diagnostic`; a helper failure as `exited`
with its `exit_code` and `oom_killed`.

## Record for the window (criterion 2c)

Due 2026-11-12 11:40Z, from the verifier journal for 10-29 11:40Z to 11-12 11:40Z (journald keeps
227 MB since the 10-22 boot) and `last-failure.json`: every mid-run failure with its code,
`diagnostic` classes and frames, `stream_resumes` and `helper_state`, and the `stream_resumes` of
each verified run. The new fields exist from the receiver wheel install that carries this change;
until then, failures carry `error` and `diagnostic` only. The record is appended here in one
docs pull request. A cause found in it gets its fix or follow-up card as outcome (a) or (b).
