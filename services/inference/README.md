# deedkit-inference

A small shared inference service for the Mac mini. Every DeedKit service calls it with its own key; it
never takes the mini away from Between or from the remote tests. This package is the shell: the API,
per-service keys, a bounded queue, admission, upload bounds, a graceful drain and temp-file hygiene. The real
engines (ANYM-683: speech with language routing, vision), the pinned models, `pull`, `verify` and the canary
are below ("Real engines"); a `fake` engine exists for tests only. The install, LaunchDaemon, sandbox,
Tailscale and the busy-file writer are ANYM-684.

Python 3.13, own `pyproject.toml` and `uv.lock`, no code shared with the other services. The service makes
no outbound connection and imports no outbound HTTP client (`test_service_imports_no_outbound_http_client`):
only `pull.py` (the model download), `canary.py` (a client of the running service) and `egress_probe.py` do, and
the serving path never imports them.

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
| `work_dir` | required | absolute path; per-request directories live here (a `.deedkit-inference-work` marker is created; a non-empty directory without it is refused; the service holds an exclusive lock on `.deedkit-inference-lock` for its lifetime, so a second start on the same directory is refused with `work_dir_in_use` before it removes anything) |
| `usage_file` | unset | absolute path; the daily job counters survive a restart when set (metadata only) |
| `release` | `unknown` | the git SHA the service runs; shown by `/v1/models` (env `DEEDKIT_INFERENCE_RELEASE` also works) |
| `queue_size` | 16 | jobs waiting for a slot |
| `max_wait_seconds` | 240 | longest estimated or actual wait in a queue |
| `retry_after_seconds` | 30 | `Retry-After` of 503 answers that have no better figure |
| `resident_cap_gb`, `resident_hard_cap_gb` | 6.0, 7.5 | admission cap and supervisor kill line |
| `min_free_gb` | 3.0 | headroom above a phase's `resident_gb` (the remote-test `MIN_FREE_MEM_GB`) |
| `max_load_1m`, `slot_max_load_1m` | 8, `{}` | 1-minute load cap; per-slot override, e.g. `{"gpu": 6}` |
| `keep_alive_seconds` | 300 | an idle loaded engine is unloaded after this (only an engine with `"engine_options": {"persistent": true}` stays loaded when idle; any other one-shot child exits with its phase and leaves the resident book at once; the real engines keep a warm worker, so they stay loaded until this time passes) |
| `max_inflight_upload_bytes` | 134217728 | pool of declared `Content-Length` of uploads in flight |
| `upload_seconds` | 120 | a body must arrive within this |
| `work_dir_max_bytes` | 536870912 | bytes reserved under `work_dir` |
| `audio_body_max_bytes`, `chat_body_max_bytes` | 32 MiB, 44 MiB | per-request body caps |
| `drain_seconds` | 900 | see SIGTERM drain |
| `model_dir` | `/Users/Shared/deedkit-inference/models` | the read-only store of verified model files (`deedkit-inference pull` fills it, run as the account that owns it) |
| `models_file` | unset | test mode only: another pins file; production always uses the packaged `models.json` |
| `aliases` | `{}` | alias table (below); an empty table serves the two aliases the package ships (`audio-default`, `vision-default`, see "Real engines") |

Alias table entry (the alias is the only name a client can use; never a repository id or a path):

```json
"audio-default": {
  "kind": "audio",              // "audio" -> /v1/audio/transcriptions, "chat" -> /v1/chat/completions
  "scope": "audio",             // audio | image | document
  "engine": "fake",             // production: "speech" (audio) or "vision" (chat); "fake" is for tests only
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
`upload_timeout` (408), `invalid_request` (422), `audio_too_long` / `image_too_large` / `audio_unreadable` / `image_unreadable` (422, the precheck), `transcript_too_large` (422, a transcript over the client's 1,000,000 characters),
`deadline_too_short` (422), `quota_exceeded` / `concurrency_exceeded` (429, `Retry-After`), `queue_full`,
`slot_busy`, `upload_capacity`, `admission_closed`, `draining`, `engine_unavailable` (all 503 with `Retry-After`),
`engine_failed` (502), `engine_timeout` (504), `internal_error` (500), `client_disconnected` (499, logged; the client is gone and never sees it). A 503 from this service always has
one of those codes; a client treats `queue_full`, `slot_busy`, `upload_capacity`, `admission_closed`, `engine_unavailable` and
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
| `memory_low` | free plus inactive plus speculative memory (`vm_stat`, as `scripts/remote-test.sh` counts it) is below the phase's `resident_gb` plus `min_free_gb`; every phase is charged in full, also one of a warm worker. A running phase's full `resident_gb` is held back from the figure the next phase sees until that phase ends (a drop of the free memory is not taken as that phase's allocation: the probe cannot tell whose it is); a phase that has to wait for it waits in the queue |
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

### Yielding (ANYM-691)

Admission only closes the door for NEW work. A phase that is already executing is stopped (`SIGSTOP` on its process group)
by the `Yielder`, a task of its own (the memory supervisor is not touched), while the busy file is valid and fresh, a flag in
`pause_on` is true and the foreign CPU (the Between VM user and the remote-test user, per-process `ps` cputime deltas; the
service's own children and the load average are not counted) is at least `pause_min_foreign_cores`, on two consecutive
ticks of `supervise_interval_seconds`. It continues (`SIGCONT`) when no `pause_on` flag has been true for
`resume_after_seconds`, or at once when the file is missing, invalid or stale or anything in the yielder fails (back to Nice 20
alone). A stopped group keeps its memory: the pause buys CPU for Between and the tests and costs RAM-minutes.
**Off by default.** Known residual (ANYM-691 PR 3 closes it before anything enables the feature): a crash of the service right after
the stop, followed by the kill of the parent of a helper that started in the milliseconds between the ledger write and the
stop, can leave that one helper stopped and unowned by recovery (`pausestate.members`).

- Only the last phase of a job, from its spawn (a child) or from the moment its call is sent (a worker). Not paused:
  loading, model-hash preparation, idle warm workers, earlier phases of a multi-phase job, a phase with less than
  `min_remaining_seconds` of estimate left, and a worker that is decoding: a decode helper runs behind the marker
  `<scratch>/nopause` (made by `media.run_tool`), the service looks for it right after the stop and continues the group at once.
- Budget per job: the smaller of `max_pause_seconds` and the slack to the request's `X-Request-Deadline-Seconds`
  (`deadline - elapsed - max(deadline_margin_seconds, K * left) - left`, K = `deadline_safety_factor`, computed when the
  pause starts); no positive slack means no pause. A phase that is slower than K times its estimate can still miss the
  deadline: a `yield-job:` line with `paused>0`, `total` beyond `deadline` and an outcome other than `ok` is that loss.
- The phase timeout and `estimated_wait` exclude the stopped time. The response carries `X-Paused-Seconds`.
- Before every stop the group is written to `state/paused.json` (see `recover`); a drain continues every paused group first.
- Settings: the optional `yield.json` next to `config.json` (same owner, not writable by others; read at start; absent,
  unreadable or invalid means the defaults and one logged line, never a failed start): `enabled` **false (ships off:
  turning it on needs `{"enabled": true}` here, gated on ANYM-691 PR 3, below)**, `pause_on`
  `["between", "deedkit_ci"]`, `pause_min_foreign_cores` 1.0, `resume_after_seconds` 10, `min_remaining_seconds` 20,
  `max_pause_seconds` 300, `deadline_margin_seconds` 10, `deadline_safety_factor` 3. `config.json` is unchanged (it rejects
  unknown fields, and a rollback only switches the release). `/health` is unchanged.
- Log lines (counts and durations, never content): at each resume `yield: job=<id> paused=<s> flags=<..> foreign_cores_mean=<n>
  wasted=<s> budget_exhausted=<0|1>`; at each end of a job that was paused or ran under a flag `yield-job: job=<id> alias=<a>
  phase=<p> est=<s> deadline=<s|none> paused=<s> excluded=<s> total=<s> outcome=<ok|engine_timeout|client_gone|error>`.
  `job` is a random id for the log only; `excluded` is the time the job ran under a flag and could not be paused.

## Queue and deadlines

- At most `queue_size` jobs wait; a full queue is `503 queue_full`. A job is queued, and a free slot granted, in one step, so a burst cannot exceed the bound; a job that must wait for resident room counts too, and so does a running job that will wait again for a later phase.
- Two slots: `gpu` and `cpu`, one job each, arrival order. A job whose estimated wait exceeds
  `max_wait_seconds` is refused at once (`slot_busy`), so a client never waits past its own deadline.
- The deadline clock starts when the request arrives (upload time counts). With `X-Request-Deadline-Seconds`, a job whose wait plus own estimate exceeds the deadline is refused at
  once (`slot_busy`; `deadline_too_short` 422 when the job's own estimate alone exceeds it), and a queued
  job that waited past `min(max_wait_seconds, deadline - estimate)` leaves the queue with `slot_busy`
  before it starts; the same bound is checked before every slot grant, also for a later phase, so a job whose
  budget is over never gets a slot, also when the admission probes themselves were slow.

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

## Real engines (ANYM-683)

The package ships this alias table; a config with an empty `aliases` serves it. A custom entry with a real
engine must name phases that `models.json` pins (`<alias>/<phase>`), else the service refuses to start.

| alias | scopes | phases (slot, `resident_gb`) |
|---|---|---|
| `audio-default` | `audio` | `langid` (gpu, 0.3: Whisper base, first 30 s, **skipped when the request names `language`**), then one route: Russian -> `gigaam` (cpu, 1.6: GigaAM v3 e2e-RNNT, onnx-asr, its Silero VAD for audio over 60 s), any other language -> `whisper` (gpu, 2.7: Whisper large-v3-turbo, mlx-whisper) |
| `vision-default` | `image`, `document` | `vlm` (gpu, 4.5: Qwen3.5-4B MLX 4-bit, mlx-vlm; measured 3.90 GB peak on the mini 11-02, plus 15 percent); an image chat needs scope `image`, a text-only chat scope `document` |

`resident_gb` is the measured peak of the research run (local-inference-11-01) plus headroom; each phase is admitted
on its own slot and memory (above). GigaAM beside Qwen (1.6 + 4.5 = 6.1 GB) is over the 6.0 GB cap, so the two never
run together; the idle one is unloaded first. Each alias's `timeout_seconds` (1800 audio, 600 vision) bounds one phase.

**Warm workers.** Each loaded `<alias>/<phase>` is one child process in its own process group (`engines/worker.py`,
a line protocol over stdin/stdout). It loads its model from verified local paths, answers jobs, and exits on
unload (it clears its caches first), after `keep_alive_seconds` idle, on a timeout (504 `engine_timeout`), on a
failed job (502 `engine_failed`), or when it exceeds 1.5 times its phase's `resident_gb` (the total over all workers
stays under `resident_hard_cap_gb`). Its `HOME`, `TMPDIR` and every library cache directory are one scratch directory
under `work_dir` (`w-<id>`, removed with the worker; a warm worker outlives any one request, so the request directory
is not it); `HF_HUB_OFFLINE=1` and the telemetry switches are set before the interpreter starts and again before any
engine import.

**Inputs and outputs.** Audio duration comes from `ffprobe` (over 4 h: 422 `audio_too_long`; unreadable: 422
`audio_unreadable`); the mini needs `ffmpeg` and `ffprobe` (options `ffmpeg`/`ffprobe` name absolute paths, else
`/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`); a missing tool is 503 `engine_unavailable`. Audio is decoded in
10-minute 16 kHz windows, so memory does not grow with the recording. An image is checked from its header
(over 20 MP: 422 `image_too_large`; not png/jpeg/gif/webp: 422 `image_unreadable`). Transcripts are cut to the
client's limits (`segments.py`: at most 20,000 segments, 10,000 characters each, times 0..86400 and ordered); over
1,000,000 characters in total is 422 `transcript_too_large`, never silently cut. The content of a `vision-default`
answer is always the JSON object the enrichment prompt asks for: string `title`, `summary`, `document_type` (at most
80 characters, no control characters) and `text`; a model answer that is not such an object becomes `text`.
`/v1/models` shows the pinned revision of each phase's model.

**503 `engine_unavailable`** (`Retry-After`): a pinned package is not installed at its pinned version, `ffmpeg`/`ffprobe`
are missing, or the model files are missing or differ from `models.json` (checked in full before every worker load).

### Pinned models

`src/deedkit_inference/models.json` lists, per `<alias>/<phase>`, each artifact the loaders consume: Hub repository, a
40-hex revision, and size and sha256 of every file (weights, configuration, tokenizer and processor files, the VAD
model), plus the engine package versions (equal to `uv.lock`). The store layout is
`<model_dir>/<alias>/<phase>/<artifact>/<file>`.

- `deedkit-inference pull [--model-dir D] [--only alias[/phase]] [--min-free-gb 15]`: the only command that
  downloads. Run it as the account that owns the store (not the service user, which can read and never write).
  It fetches each file at its pinned revision into `.pull-*`, verifies the whole directory against the pins and
  publishes it with one rename; an interrupted or mismatching pull leaves nothing served. About 5.7 GB.
  It contacts `huggingface.co` and the redirect targets the Hub gives for large files (CDN hosts under
  `huggingface.co` and `hf.co`, e.g. `cas-bridge.xethub.hf.co`); a redirect to any other host or to plain http is
  refused. The sha256 pins decide the bytes; this limit decides who is asked.
- `deedkit-inference verify [--model-dir D]`: re-hashes everything, no network; exit 1 on any difference.
- The server never downloads. Loaders are given paths, never repository ids. Before every worker load every
  consumed file is re-hashed in full (no cache keyed on size or mtime). A file that differs, is missing or is extra
  makes the alias unavailable until the files verify again.
- Only safetensors, ONNX, NPZ (no object arrays, MLX reads them without pickle), JSON, text and Jinja files are
  allowed; a configuration naming `model_file`, an auto map, custom or remote code is refused at load and at `pull`;
  no engine call enables remote code, and no runtime has a download fallback (source tests).

### Dependencies

The engine packages are the optional group `engines` of `pyproject.toml`, exact versions with the marker
`sys_platform == 'darwin' and platform_machine == 'arm64'`, so `uv sync --locked --group dev` (Linux CI) installs none
of them. `[tool.uv] no-build = true`: wheels with locked hashes only. On the mini: `uv sync --locked --group engines`.
`mlx-whisper` declares `torch` as a dependency (never imported at run time); the supply-chain report is in the pull
request.

### Canary and egress probe

`deedkit-inference canary --key-file F [--url U] [--set DIR] [--generate]` generates the synthetic set of
`tests/canary/` (fixed seeds, no real data; needs Pillow, macOS `say` and ffmpeg), sends it through the live aliases
with the enrichment prompt, scores it against `tests/canary/expected.json` (character error per image, word error per
note, real-time factor, the 20-minute recording's segment bounds, the JSON shape) and exits non-zero outside the
bounds. `--egress` runs `egress_probe.py` as an engine child would (run it inside the service's sandbox profile): TCP
and UDP to a loopback port other than the service's and to a public address must all fail.
