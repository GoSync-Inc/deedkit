# deedkit-inference

A small shared inference service for the Mac mini. Every DeedKit service calls it with its own key; it
never takes the mini away from Between or from the remote tests. This package is the shell: the API,
per-service keys, a bounded queue, admission, upload bounds, a graceful drain and temp-file hygiene. It
ships a `fake` engine for tests only. Real engines, model pinning and `pull` arrive with ANYM-683; the
install, LaunchDaemon, sandbox, Tailscale and the busy-file writer with ANYM-684.

Python 3.13, own `pyproject.toml` and `uv.lock`, no code shared with the other services. The service makes
no outbound connection and imports no outbound HTTP client (`test_service_imports_no_outbound_http_client`).

## Launch contract

```sh
deedkit-inference serve --config /path/to/config.json     # or DEEDKIT_INFERENCE_CONFIG=/path/to/config.json
```

- One process tree: `serve` is the parent; every engine runs as a child in its own process group, so the
  sandbox of ANYM-684 can wrap the whole tree. No other process is started.
- It binds `bind:port` (default `127.0.0.1:8090`). Any `bind` that is not a loopback IP literal refuses
  to start (exit 2). TLS and the tailnet are outside the service (Tailscale Serve, ANYM-684).
- It refuses to start (exit 2, one stderr line with a machine reason, nothing else) when the config or the
  policy is invalid, the policy file is not a private regular file, `work_dir` is unusable, or a
  production config names the `fake` engine.
- Logs go to stderr, one line per request: `time rid=<id> principal=<id|-> alias=<alias|-> bytes_in=<n>
  seconds=<s> status=<code> refusal=<code|->`. Nothing else about a request is logged: no content, no file
  name, no key. `newsyslog` rotation is the install's job.
- Python and uv: `uv sync --locked` in `services/inference`, then `uv run deedkit-inference serve ...` or
  the venv's `bin/deedkit-inference`.

### SIGTERM drain (what a deploy relies on)

`SIGTERM` (and `SIGINT`) starts the drain:

1. Admission closes at once. New requests get `503 draining` (`Retry-After`); `/health` says
   `"status": "draining"`.
2. Every queued job and every running job is finished. A deploy therefore never interrupts a running job,
   whatever the queue depth. `/health` and the answers to the jobs in flight keep working.
3. The process exits `0` when queue and running jobs are empty and the last response is written.
4. After `drain_seconds` (default 900) whatever is left is killed (child groups included) and the process
   exits non-zero (`3`).

So: stop with `SIGTERM`, wait up to `drain_seconds` plus a few seconds, and only then use `SIGKILL`
(LaunchDaemon `ExitTimeOut` must be larger than `drain_seconds`). A hard kill is safe for data, because the
next start sweeps `work_dir`, but it cuts running jobs.

## Config file (JSON)

Not secret, but it must be a regular file that is not writable by group or others.

| key | default | meaning |
|---|---|---|
| `version` | required, `1` | |
| `mode` | `production` | `test` allows the `fake` engine and static probe values, and only with the env `DEEDKIT_INFERENCE_TESTING=1` |
| `bind`, `port` | `127.0.0.1`, `8090` | loopback IP literal only |
| `policy_file` | required | absolute path of the key policy (below) |
| `busy_file` | required | absolute path of `busy.json` (below) |
| `work_dir` | required | absolute path; per-request directories live here (a `.deedkit-inference-work` marker is created; a non-empty directory without it is refused) |
| `usage_file` | unset | absolute path; the daily job counters survive a restart when set (metadata only) |
| `release` | `unknown` | the git SHA the service runs; shown by `/v1/models` (env `DEEDKIT_INFERENCE_RELEASE` also works) |
| `queue_size` | 16 | jobs waiting for a slot |
| `max_wait_seconds` | 240 | longest estimated or actual wait in a queue |
| `retry_after_seconds` | 30 | `Retry-After` of 503 answers that have no better figure |
| `resident_cap_gb`, `resident_hard_cap_gb` | 6.0, 7.5 | admission cap and supervisor kill line |
| `min_free_gb` | 3.0 | headroom above a phase's `resident_gb` (the remote-test `MIN_FREE_MEM_GB`) |
| `max_load_1m`, `slot_max_load_1m` | 8, `{}` | 1-minute load cap; per-slot override, e.g. `{"gpu": 6}` |
| `keep_alive_seconds` | 300 | an idle loaded engine is unloaded after this |
| `max_inflight_upload_bytes` | 134217728 | pool of declared `Content-Length` of uploads in flight |
| `upload_seconds` | 120 | a body must arrive within this |
| `work_dir_max_bytes` | 536870912 | bytes reserved under `work_dir` |
| `audio_body_max_bytes`, `chat_body_max_bytes` | 32 MiB, 44 MiB | per-request body caps |
| `drain_seconds` | 900 | see SIGTERM drain |
| `aliases` | required | alias table (below) |

Alias table entry (the alias is the only name a client can use; never a repository id or a path):

```json
"audio-default": {
  "kind": "audio",              // "audio" -> /v1/audio/transcriptions, "chat" -> /v1/chat/completions
  "scope": "audio",             // audio | image | document
  "engine": "fake",             // ANYM-683 registers the real ones
  "engine_options": {},
  "timeout_seconds": 600,       // one phase's hard timeout; the child group is killed
  "default_max_tokens": 2000,   // chat only
  "phases": [{"name": "stt", "slot": "cpu", "resident_gb": 1.5, "share": 1.0}]
}
```

`slot` is `gpu` or `cpu`; each slot runs one job at a time. A job takes one slot per phase, in order.
`share` is the part of the engine's estimate that belongs to the phase (shares add up to 1).

## Routes

Every route is under `/v1`, except `GET /health`. Every error the service itself returns has the body
`{"error": {"code": "<machine code>", "message": "..."}}`, so a client can tell it from a proxy's answer
(Tailscale Serve answers 502/503/504 with its own body when the process is down).

| route | auth | body |
|---|---|---|
| `GET /health` | none | `{"status": "ok"\|"draining", "queue": {"depth", "active", "capacity"}, "admission": "open"\|"closed"}` and nothing else |
| `GET /v1/models` | key | `{"object": "list", "data": [{"id", "object": "model", "scopes", "engine", "revision"}], "key_expires_at", "release"}`, only aliases the key's scopes allow |
| `POST /v1/audio/transcriptions` | key | multipart: `file`, `model`, optional `language`, optional `response_format` (`json` \| `verbose_json`) |
| `POST /v1/chat/completions` | key | JSON: `model`, `messages` (text and `image_url` data URLs), optional `max_tokens` 1..16000 |

The key travels only in `Authorization: Bearer <key>`. A request header `X-Request-Deadline-Seconds: <n>`
states the client's own deadline (the enrichment provider sends its `timeout_seconds`). Unknown request
fields are 422 (strict); `stream` and every generation field other than `max_tokens` are unknown. Responses
carry `X-Request-Id`.

Error codes (status): `unauthorized` (401, one body for a missing, malformed, unknown, expired or revoked
key), `scope_denied` (403), `unknown_model` (404), `payload_too_large` (413), `length_required` (411),
`upload_timeout` (408), `invalid_request` (422), `audio_too_long` / `image_too_large` (422, the precheck),
`deadline_too_short` (422), `quota_exceeded` / `concurrency_exceeded` (429, `Retry-After`), `queue_full`,
`slot_busy`, `upload_capacity`, `admission_closed`, `draining` (all 503 with `Retry-After`),
`engine_failed` (502), `engine_timeout` (504), `internal_error` (500). A 503 from this service always has
one of those codes; a client treats `queue_full`, `slot_busy`, `upload_capacity`, `admission_closed` and
`draining` (and connect errors, and 502/503/504 with another body) as backpressure. `admission_closed`
adds the response header `X-Admission-Reason`.

## Key policy file

A private JSON file: a regular file (not a symlink), mode `0600` (no group or other bits), at most 64 KiB,
else the service refuses to start. Unknown fields are refused. The service re-reads it when it changes.

```json
{
  "version": 1,
  "revision": 7,
  "principals": [
    {
      "id": "messaging-enrichment",
      "keys": [
        {"sha256": "<64 hex: sha256 of the key>", "expires_at": "2027-02-01T00:00:00Z"},
        {"sha256": "<64 hex: the new key during a rotation>", "expires_at": "2027-05-01T00:00:00Z"}
      ],
      "scopes": ["audio", "image", "document"],
      "max_jobs_per_day": 500,
      "max_concurrent": 2
    }
  ]
}
```

- `keys` has one or two entries, so a rotation overlaps old and new; removing the old entry and bumping
  `revision` revokes it. `expires_at` is a timezone-aware ISO time.
- `revision` only grows. A reload with a smaller revision, or the same revision with a different digest, is
  refused and the old policy stays in force (a warning is logged with the code `policy_rollback`).
- Writers must use `deedkit_inference.policy.write_policy(path, document)`: it takes a lock file
  (`<path>.lock`), requires a strictly greater revision, writes a `0600` temp file and replaces the policy
  atomically.
- The key is hashed and compared in constant time against every key of every principal. The daily cap counts
  accepted jobs per UTC day; `max_concurrent` counts requests in flight (upload included).
- Every probe of the key's health (ANYM-684) can read `key_expires_at` from `GET /v1/models`.

## Admission

A job is checked when it arrives and again when it leaves the queue to start a phase. It is refused with
`503 admission_closed` (`Retry-After`, header `X-Admission-Reason`) when:

| reason | rule |
|---|---|
| `busy_between`, `busy_deedkit_ci` | the busy file says Between or a DeedKit remote test is running |
| `busy_file_missing`, `busy_file_invalid`, `busy_file_stale` | the busy file is missing, unparseable, or older than 30 s (fail closed) |
| `load_high` | the 1-minute load average is above `max_load_1m` (or the slot's override) |
| `memory_low` | free plus inactive memory is below the phase's `resident_gb` (0 when that engine is already loaded) plus `min_free_gb` |
| `resident_cap` | the phase alone does not fit `resident_cap_gb` |

Resident accounting (`resident_cap_gb`, idle loaded engines count): when the only blocker is an idle loaded
engine of this service it is evicted first and the waiters wake once the memory is back; when the blocker is
a running job the new job waits in the queue and is not refused. A running job is never killed because
admission closed. The supervisor kills the largest engine child when all children together exceed
`resident_hard_cap_gb`.

### The busy file (written by ANYM-684, read by the service)

`busy_file` holds, replaced atomically (write a temp file, `rename`) at least every 10 seconds:

```json
{"ts": 1767225600, "between": false, "deedkit_ci": false}
```

`ts` is epoch seconds (a number); `between` and `deedkit_ci` are booleans. Extra fields are ignored. Anything else (missing file,
not JSON, other types, `ts` older than 30 seconds or more than 30 seconds in the future) counts as busy.
The service only reads the file, never writes it.

## Queue and deadlines

- At most `queue_size` jobs wait; a full queue is `503 queue_full`.
- Two slots: `gpu` and `cpu`, one job each, arrival order. A job whose estimated wait exceeds
  `max_wait_seconds` is refused at once (`slot_busy`), so a client never waits past its own deadline.
- With `X-Request-Deadline-Seconds`, a job whose wait plus own estimate exceeds the deadline is refused at
  once (`slot_busy`; `deadline_too_short` 422 when the job's own estimate alone exceeds it), and a queued
  job that waited past `min(max_wait_seconds, deadline - estimate)` leaves the queue with `slot_busy`
  before it starts.

## Uploads and temp files

- Auth happens from the headers before the body is read. The service reserves the declared
  `Content-Length` (411 without one, 413 over the cap) from `max_inflight_upload_bytes`; a full pool is
  `503 upload_capacity` before any body byte is read. The body streams to disk and is cut at the cap (413).
- A body that does not arrive within `upload_seconds` is cut with 408.
- Each request has a `0700` directory under `work_dir`, removed on every outcome; the upload file name is
  never used as a path; startup removes every leftover.

## Tests

`uv sync --locked --group dev && uv run pytest -q` in `services/inference`; fake engines, an injected
probe, no macOS or GPU. The contract fixtures live in `tests/fixtures/inference_contract/` at the
repository root.
