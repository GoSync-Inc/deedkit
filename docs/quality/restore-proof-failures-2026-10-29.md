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
So the failure was transient, not in the data or the images. The journal printed only `independent_restore_failed`, because `main()` at those
revisions printed that string for every exception. `last-failure.json` now holds a later
`receiver_busy` (10-28 18:57Z), not one of these four. Helper failure files are removed with the
payload. The journal records no R2 resume counts.

The many 1-second `independent_restore_failed` runs (10-27 07:32, 07:47, 08:04, 18:33, 18:49;
10-28 01:17-01:48, 11:18-13:08) are not mid-run failures. They failed before any stream, every
timer cycle, and printed the same generic string. Their codes are lost. They stop after ANYM-632,
which writes `busy` status for an upload's lock.

## What the evidence shows

1. **Exit 130 is the verifier's own cleanup, not the helper's failure.** The helper image is
   built `FROM` the official postgres image and keeps its `STOPSIGNAL SIGINT` (read back:
   `stop=SIGINT`). Python dies of an uncaught SIGINT with status 130. The verifier's `except`
   path runs `cleanup_resources`, which runs `docker container stop --time=10`
   (`backup_r2_verifier.py`, `Docker.remove`). Both helper deaths fall in the same minute as the
   verifier's failure line. So in those two runs the verifier had already raised, and the helper
   was still streaming. A helper that failed on its own exits 1. A kill exits 137. A forwarded
   SIGTERM from `process.terminate()` exits 143.
2. **No OOM, no daemon fault.** `journalctl -k` covers the whole window (since the 10-22 boot,
   234 lines from 10-27 to 10-28 03:00) and has no OOM or killed-process line.
   `docker.service`/`containerd.service` log no error or warning between 10-27 07:50 and 10-28
   02:15. `docker events` no longer holds those days.
3. **In the 045/046 wheels only two exceptions mid-stream gave the generic code.** That code
   means `failure_code` found no safe code (verifier at `ad46e79`, `failure_code`). During the
   stream, `restore()` called `r2.verify_object(receiver,client,receipt,Sink())`. That
   `verify_object` had no resume: a `body.read(MIB)` error from botocore/urllib3
   (`ResponseStreamingError`, `ReadTimeoutError`) propagated raw. The deadline
   (`TimeoutError`/`restore_deadline`), stop (`InterruptedError`), disk reserve
   (`restore_disk_capacity`), `stored_backup_corrupt` and a helper exit
   (`isolated_restore_failed`) were all safe codes. The two candidates left are:
   - (i) a broken R2 body read: an SDK read error;
   - (ii) a `BrokenPipeError` on the helper's stdin while the helper itself lived. This happens
     when the `docker container start -ai` client ended. The helper-death case is ruled out for
     the two exit-130 runs by point 1.
4. **No mid-run failure since the resume shipped.** The resume (bounded ranged GET, `STREAM_RESUMES`
   = 5, #332, merged 10-28 04:37Z) reached the receiver with the `recvupd` kit (10-28 15:07Z).
   Restore runs since then: 10-28 15:09 → 16:19 verified, 19:13 → 20:12 verified, 10-29 09:27 →
   10:15 verified (`last-success.json` `a7aac368…`). The only failures in that time are three
   pre-stream `receiver_busy` (10-28 18:27-18:57). This fits (i), but three runs prove little,
   and a run that a resume saved looks exactly like a clean one.

The card's other candidates: the resume limit could not fire, because no resume existed then.
The job deadline is 21,600 s, and a deadline or stop maps to its own code. Helper memory is ruled
out by point 2 and by the exit codes.

## Outcome: (c) cause not determinable from the evidence; this PR adds what was missing

Most likely cause: (i), a mid-stream R2 body read error that the verifier did not resume, at the
revisions installed then. #332 (ANYM-285) fixes it. The evidence cannot separate (i) from (ii),
because these runs predate the class diagnostics, and it cannot show whether R2 still breaks
under the resume. Missing evidence and where it now comes from:

| Missing | Now recorded by |
|---|---|
| The exception class and frames of a failed run | `diagnostic` (ANYM-285 #332, installed) |
| The helper's failure code after a broken pipe | `failure.json` → `error` (ANYM-603 #369, installed) |
| How many ranged GETs a stream needed: whether R2 still breaks, and the headroom to `STREAM_RESUMES` | `stream_resumes` in a verified `status.json`/`last-success.json`, and in a failure after the full stream (this PR) |
| How the helper ended: a stop (130), a kill/OOM (137), its own error (1), or a client end | `helper_exit` on a helper failure (this PR) |

Case (ii) will now show as `isolated_restore_failed` with `helper_exit` other than 1. Case (i)
under the resume shows as a growing `stream_resumes` on verified runs, or as `storage_unavailable`
with its SDK class in `diagnostic`.

Record for the window (criterion 2c): mid-run failures in the 14 days are read from
`last-failure.json`, `status.json` and the journal at the window's end. Any failure there carries
the fields above.
