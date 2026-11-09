# Optional file enrichment

The archive, search, OCR and original downloads work without a model. Model
processing is a separate opt-in worker; it is never run inside an MCP request.
Results are derived observations, not verified facts or executable instructions.

Since 0.9.6, profile configuration is published separately from file job state.
`get_sync_status` account summaries expose safe profile labels and their liveness
deadline. `get_file_segments` distinguishes an enabled profile waiting for its
first visit from missing configuration and an expired worker observation. Model
directories, endpoints and key locations are not included in this registry.
See [COLLECTOR_STATES.md](COLLECTOR_STATES.md).

## Connectors

| Profile | Local | API |
| --- | --- | --- |
| Audio transcription | faster-whisper, preinstalled model directory; `deedkit_inference` (the owner's Mac mini) | OpenAI or compatible transcription endpoint |
| Image descriptions and visible text | Ollama vision model; `deedkit_inference` | OpenAI-compatible chat, Anthropic Messages, Gemini |
| Document title and summary | Ollama text model; `deedkit_inference` | Same text/vision API connectors |
| Optional document analysis/image description | — | Codex CLI with private ChatGPT subscription auth |

The Codex connector is implemented locally but not deployed or enabled. Read
[its isolation and rollout requirements](CODEX_ENRICHMENT.md) before using it;
it is cloud inference and shares subscription usage with other Codex work.

### deedkit_inference: the owner's Mac mini (ANYM-685)

`deedkit_inference` sends audio, photos and document text to the inference service on the owner's Mac mini
(`services/inference`, over the tailnet) instead of a cloud provider or the worker's own CPU. It is not cloud:
it needs no `allow_cloud`, and a profile that sets `allow_cloud` (even `false`) is rejected, so the flow can never
be labelled cloud. It takes no `local-heavy` lock: the work happens on the mini.

| Key | Rule |
| --- | --- |
| `provider` | `deedkit_inference` |
| `local_inference` | must be `true` (any other provider rejects this key) |
| `model` | `audio-default` for `audio`; `vision-default` for `image` and `document` |
| `base_url` | `https`, port 443 or none, a `*.ts.net` name (no IP literal, no credentials or query), path exactly `/v1` |
| `api_key_file` | absolute path to the service key, mode 0600 |
| `language` | optional ISO 639-1 code, `audio` profiles only; sent as the request's `language` and skips language identification on the mini |
| `timeout_seconds` | up to 900 (a 4-hour recording takes about 8.5 minutes); sent to the service as `X-Request-Deadline-Seconds`, its queue deadline |
| `backpressure_seconds` | cooldown when the mini says "not now" and gives no `Retry-After`: 1 to 900, default 120 |
| `audio_response_format` | `verbose_json` (default, with segments) or `json` |
| `max_input_bytes` | the largest original the worker copies: default 20 MiB for `audio` and `image`, 32 MiB for `document` (ANYM-702); the most a profile may set is 200 MiB for `audio` and 32 MiB for `image` and `document` (ANYM-697). What is sent is capped separately, below |

Audio uses `POST {base_url}/audio/transcriptions`; photos and documents use `POST {base_url}/chat/completions`, a
document sending its extracted text without an image (scanned-PDF page images are not part of this connector).
Redirects and environment proxies are off. The prompt is the usual one plus a sentence asking for `title` and
`summary` in Russian and the visible text in its own language; the description is stored with `prompt_version`
`file-description-v2-ru`. Agents search in Russian, and without that sentence a photo with no source language is
described in English.

**What waits and what fails.** When the mini cannot take the job right now the job waits and costs nothing:

- waits (`model_backpressure`): the mini is unreachable (connection refused, DNS or TLS failure, a connect
  timeout); HTTP 429; HTTP 503 carrying the service's own error (`queue_full`, `slot_busy`, `upload_capacity`,
  `admission_closed`, `draining`); and HTTP 502, 503 or 504 whose body is not the service's `{"error": {"code": ...}}`
  envelope (Tailscale Serve answers those itself while the service process restarts, e.g. for a deploy);
- fails (a normal attempt, `model_request_failed` / `model_http_error`, `retry_exhausted` after three): a read
  timeout once the request was accepted, 504 `engine_timeout`, 502 `engine_failed`, any other error the service
  itself answers with its envelope, 413, 422 and an invalid answer, so one file that hangs the engine cannot loop
  forever;
- 401 and 403 stay `model_auth_failed`, which pauses the scope until the operator clears it.

A deferral puts the job back to `queued` under its lease token, restores its `attempts`, gives back that claim's
`enrichment_budget` and `processing_budget` counts (on the day of the claim) and pauses the scope
`(account, kind, profile_hash)` with `pause_reason = 'model_backpressure'` until now plus the cooldown: the
service's `Retry-After` when present, else `backpressure_seconds`, at most 900 seconds. The pause never weakens
another one: an indefinite pause (such as `model_auth_failed`) stays, and of two bounded pauses the later expiry
wins. It is shown in the published profile state like any pause. A mini that is off therefore costs each worker one
claim and one refund per scope per cooldown, and no attempt or daily budget.

Readiness events for old attachments carry no message age. While a backlog drains, an agent that needs it looks
up the message's `sent_at`.

Data leaves the worker for the owner's mini only, over WireGuard and TLS: the audio (the original, or Opus when
transcoded), the image bytes (the original, or a scaled jpeg), up to `max_text_chars` of extracted text for a document, and the bearer key. Derived text comes
back; the mini keeps nothing after the job. Nothing leaves the tailnet, and no cloud provider is involved.

### What reaches the mini: audio and photos (ANYM-696)

The mini decodes any ffmpeg-readable audio by content and takes photos up to 20 MP, so the worker sends only the
bytes it needs. The original always stays in the archive; the metadata says what was done.

- **Audio, `deedkit_inference` only.** A voice note whose first audio stream is Opus in an ogg container with an
  average bitrate (bytes x 8 / duration) of at most 48 kbps goes **unchanged** as `recording.ogg`, with no ffmpeg run
  (chat apps send about 32 kbps; the margin covers Ogg page overhead). Any other source above 32 kbps (an Opus note
  above 48 kbps, mp3, m4a, wav, mp4 including video notes, an ogg that is not Opus) is transcoded to **Opus** (24 kbps
  voip, mono, 16 kHz, ogg): about 1.8 MB for ten minutes instead of 7.2 MB. A source that is not Opus and at or below
  32 kbps goes unchanged, an ogg/vorbis note included. The transcode keeps the duration check (0.25 s), the output size
  limit and the timeouts, and records `transcoded` and `transcode_lossy` true with `transcode_format` `opus`. On a note
  that was an mp3 or an mp4 that is expected: `opus` names the format sent, not the source's. Every other ASR provider
  keeps the mp3 transcode of ogg notes and `transcode_format` `mp3`.
- **Large recordings (ANYM-697).** The original cap (`max_input_bytes`, up to 200 MiB for audio) is separate from
  the upload cap: the file that is sent is at most 32 MiB less 64 KiB (the mini's body cap counts the multipart
  framing; `UPLOAD_LIMIT_BYTES`), whatever the profile says. A source that would go unchanged but is over that cap
  is transcoded to Opus too, so a 48 MiB mp3 of 50 minutes is sent as about 9 MB. By bytes a note of about 3 hours
  still fits; a longer one ends as `audio_transcode_size_limit` with nothing sent: the recording is too long for one
  upload and needs chunking (a later card), the original is kept, and the remedy is not a smaller file. A recording
  over `max_audio_seconds` (at most 4 hours) ends as `audio_duration_limit` before any ffmpeg run. The transcode time
  limit is `max(120 s, 60 s + duration / 20)` inside the profile's `timeout_seconds`: 120 s up to 20 minutes, 3 minutes
  for 40, 10 minutes for 3 hours. Time is the second ceiling: the worker's child (transcode, upload of the Opus bytes at
  the link rate, and the mini's own 0.04 x duration) must finish inside `timeout_seconds + 10`, 910 s for the mini
  (the parent's copy of the original runs before that clock, bounded by bytes, and uses part of the lease's
  50-second margin), and the mini refuses a body slower than its `upload_seconds`; in a bad link window a note that fits by bytes ends as
  `model_timeout` or `model_http_error` and is retried. A `video/mp4` note is transcribed from its audio track
  (`-vn`). The copy of a large original lives in the worker's `/tmp`: the `enricher-mini` tmpfs is 320 MiB; the other
  workers keep 128 MiB, so raise their `/tmp` in the same edit before raising a local profile above 100 MiB.
  `enrichment_failed` on a large original can mean the tmpfs is smaller than the profile's cap (an operator mistake);
  it is also the worker's code for any unexpected error (a missing file, a permission or I/O error), so read the mount
  size (`df /tmp` in the container) and the worker's error before concluding. Order on a host: the new image first (an older image refuses `max_input_bytes` above 32 MiB with
  `invalid_model_limit` and then the audio profile is not usable), then the profile line; on a rollback restore the
  line first. Editing the profile changes its hash, and the worker, which reads its profile once at start, picks every
  failed row of that kind again after its next restart, within the day budgets: check the budget before the edit,
  restart once, and track all of those rows.
- **Documents up to 32 MiB (ANYM-702).** A `document` profile without `max_input_bytes` takes 32 MiB, the validator's
  maximum, not 20: for a document the limit only bounds the worker's disk copy (digest-checked in its `/tmp`) and the
  child's file-size limit, while the request of every provider (`deedkit_inference`, the OpenAI-style ones and the
  optional `codex_cli`, which opens a file only for an image) carries just the extracted text, at most
  `max_text_chars`, never the file. A document over 32 MiB ends as `original_size_limit` and no profile can raise the limit.
- **Final limit codes (ANYM-702).** Three codes mean the file can never pass the profile as it is, so the job is
  attempted once, the event says `retryable` false, and `attempts` stays 1 (the number of attempts made). The code
  is what an agent sees as `enrichment_failure` on a file read:
  - `original_size_limit`: the original is larger than the profile's `max_input_bytes`;
  - `audio_duration_limit`: the recording is longer than the profile's `max_audio_seconds`;
  - `audio_transcode_size_limit`: the Opus form does not fit one upload; the recording needs splitting (a later card).

  These rows are final for their profile: the worker takes them again only after the profile changes (a different
  hash), which restarts the attempts as for any failed row. `audio_transcode_size_limit` depends on the worker image's
  transcode settings, so a new image alone does not reopen it. A file whose size differs from the recorded one is not
  a size limit: it fails as `original_integrity_error` and stays retryable.
- **Photos, `deedkit_inference` only.** A picture whose longer edge is over 2048 px or whose file is over 700 KB (700,000
  bytes, so about 930 KB as base64) is scaled by ffmpeg to a longer edge of at most 2048 px (never enlarged, EXIF
  orientation applied, one frame) and sent as jpeg, also when the source is png, webp or gif; the EXIF block, GPS
  included, is not carried over. A result still over 700 KB is recoded once at lower quality; if it is still over, it is
  sent as it is. The stored metadata carries `image_downscaled` and `image_over_budget`. A small picture goes
  unchanged. A picture ffmpeg cannot read as png, jpeg, webp or gif (a corrupt file, svg, a playlist), a timeout or an
  oversized output ends the job with `invalid_image` and nothing is sent. The ffmpeg run takes a fixed list of image
  demuxers, `-protocol_whitelist file,pipe`, no stdin, the worker's time and file-size limits.

## Retained descriptions and video (schema 34)

`messaging_service.file_understanding` is a separate optional worker for a
descriptive title and a factual search summary. It does not replace extracted
PDF/Office/spreadsheet text, OCR, full audio transcripts, or timestamp segments.
Audio summaries consume the existing current transcript; document summaries
consume existing current extraction. A current image description may be reused,
with its original provider/model and an explicit `reused_description` label.
These steps never retranscribe audio or silently choose a different model.

The independent `attachment_understanding` table retains descriptions and coverage
with the source digest, message revision and a hash of the actual input detail.
Replacing media, deleting its message and actual transcript/extraction changes
invalidate old descriptions in the same transaction. A caption or formatting edit
with the same active CAS occurrence carries completed extraction, ASR segments
and descriptions to the new message revision without reopening the original.
The content hash excludes that caption revision; the separate source revision
and cancelled lease still reject results from an old in-flight attempt. Unchanged
file jobs also retain attempt counts, errors, profile identity and retry backoff.
For model jobs, cancelling the third in-flight attempt marks the job `failed` /
`retry_exhausted`; the parser applies its configured limit on its next queue poll.
Cancellation does not grant new attempts or refund daily budgets. A profile
change, an in-flight refresh, worker shutdown or a failed refresh preserves the last good description and its
published provider/model. Search can label that artifact as belonging to an older
profile; changing configuration cannot erase the only description of an evicted
video. Job state and last successful publication are separate: `indexed_at` and
`published_*` describe the retained artifact while `state`, `profile_hash` and
`error_code` describe the current attempt. Worker liveness expiry does not delete
retained content. Original retention and explicit pins are described in
[MEDIA_STORAGE.md](MEDIA_STORAGE.md).

Coverage is explicit for every modality:

| Kind | Retained detail | Description coverage |
| --- | --- | --- |
| Audio | Existing full ASR text and any real timestamp segments | `transcript_summary`, retained/input character counts, timestamp availability |
| Document | Existing PDF pages, Office text, workbook sheet/cell spans | `extracted_text_summary`, retained/input character counts; parser limits still apply |
| Image | Existing OCR/description remains separate | `image_description`, including whether a prior description was reused |
| Video | A description of bounded sampled frames | `sampled_frames`, exact sampled times, frame count and duration; `complete=false`, `audio_transcribed=false` |

Video descriptions cover selected frames only. They do not claim speech
transcription, continuous viewing or events between samples. The default samples
four frames at 0%, 25%, 50% and 75% of a finite known duration. ffprobe and ffmpeg
accept only an explicit list of local video containers with `file,pipe` protocols;
playlists and network inputs are rejected. Original size and SHA-256 are checked
before sampling. Decoder dimensions are capped at 8192 per side and 16,777,216
pixels; generated frame edges default to 384 pixels. The contact sheet is bounded
to 8 MiB. Every native process runs inside the isolated child's process group,
CPU/address-space/file bounds and overall timeout. Missing native tools or invalid
media report a failure, without claiming an empty successful description.

The video profile accepts `video_frames` (1..8), `video_frame_edge` (64..512),
`video_max_seconds` (1..14400) and `max_input_bytes` (up to 1 GiB; default 128 MiB).
Larger originals remain available but unprocessed. Other model profiles retain
their existing 32 MiB input ceiling and 100,000-character summary-input ceiling.
The full already extracted/transcribed detail stays in the database when a
summary uses a shorter prefix. Partial input, parser truncation or partial ASR
metadata produces `complete=false` and `truncated=true`; such a summary cannot
turn incomplete source processing into permission to evict the original.

Apply `deploy/file_understanding_role.sql` as the database owner after migration
34, and give a dedicated login membership in `messaging_file_understanding`.
The role reads current source/detail and residency but cannot change transcripts,
messages, sessions, original files, agent keys or permissions. Mount originals
read-only, the private configuration/key files, and the same precreated
`PROCESSING_LOCK_DIR` used by extraction/ASR. The worker inherits the shared
`local-heavy` host lock and durable resource lease for local models and all video
sampling, including video processing with an explicitly selected cloud model.
Use container CPU/memory/process limits; a separate Ollama daemon also needs its
own limits and cloud-disabled configuration.

Create a mode-0600 JSON configuration with `enabled`, an explicit account list,
`max_jobs_per_day`, and `profiles` keyed by `audio`, `document`, `image`, or
`video`. Audio/document profiles select a text model; image/video profiles select
a vision model. For example, after the operator has installed actual local
models and disabled cloud features on the Ollama daemon:

```json
{
  "enabled": true,
  "accounts": ["operator-selected-account"],
  "max_jobs_per_day": 20,
  "profiles": {
    "audio": {"provider": "ollama", "model": "installed-text-model", "local_only_daemon": true},
    "document": {"provider": "ollama", "model": "installed-text-model", "local_only_daemon": true},
    "video": {"provider": "ollama", "model": "installed-vision-model", "local_only_daemon": true,
      "video_frames": 4, "video_max_seconds": 600}
  }
}
```

Run `python -m messaging_service.file_understanding --config /run/secrets/understanding.json`
with `DATABASE_URL_FILE` pointing at that worker's private DSN and `PROBE_FILES`
pointing at read-only originals. Models are never installed or downloaded by the
worker. Cloud text/vision profiles require the existing explicit `allow_cloud`
opt-in and private API-key file; no automatic fallback or subscription CLI is
used. An empty `profiles` object enables observation without enabling a model.
Missing profiles remain `not_configured`; configured audio/document jobs waiting
for retained detail report `queued` with `waiting_for_transcript` or
`waiting_for_text`, and unsupported source extraction reports `unsupported` /
`source_unsupported`. Waiting does not consume a model attempt.

Understanding shares `enrichment_budget` with ASR and existing enrichment.
Configure the same global UTC-day cap on all workers. Optional
`profile_max_jobs_per_day` (including `video`) and `account_max_jobs_per_day`
provide additional caps; profile changes do not reset the day's counters.
Failures count, retries stop after three attempts for an input/profile, and
authentication/rate-limit pauses use the existing durable controls. On SIGTERM the
enrichment worker ends its child process group, requeues the job as `queued` /
`worker_stopping` without consuming an attempt (the daily budget stays spent),
releases its local-heavy lease and unpublishes its profile within the 90 s grace
period. A transient database error in a pass (a statement or lock timeout, a deadlock, a busy,
full or restarting server, a lost connection) does not end the enrichment worker: it logs one
line naming only the exception type, waits 5 s, doubled per consecutive failure up to 60 s, and
tries again. A claim that rolled back spent nothing. A claim whose commit reached the server
but whose answer was lost keeps its attempt and budget, and that job stays under its lease until
that expires. Such an error while writing a good result records no failure: the write is tried
again each second for up to 30 s while the lease lasts, under the same token, so a retry after a
commit whose answer was lost changes nothing. If it still fails, or the worker is stopping, the
job stays under its lease until that expires. Exit 78, a failed requeue while
stopping, a refused login, a bad DSN option and any other error still end it. Fairness
between modalities/accounts prevents a continuous audio backlog from hiding
video work. All of this is source functionality; production enablement requires
the verified promotion and applicable live-session authority in
[docs/operations/production.md](docs/operations/production.md).

Choose an actual model that supports the requested modality. Compatibility is
tested with mocked protocol responses, not every hosted model. There is no
automatic provider fallback or model download. API credentials are mounted from
private files, never included in Git or MCP arguments.

For local-only Ollama, disable cloud features on its daemon
(`OLLAMA_NO_CLOUD=1`) and set `local_only_daemon: true` in the worker profile. A loopback URL alone does not prove local inference: Ollama also supports
cloud models. Cloud processing requires explicit `allow_cloud: true`.

## Isolation and limits

Give the worker its own database login with membership in `messaging_enricher`
(apply `messaging_service/enrichment_role.sql` as the database owner). Since schema 37
the enricher has no direct `events` or `feed_counter` writes: it publishes readiness only
through `publish_file_enrichment_event`, which accepts transcript/description kinds for an
active attachment of the current message revision. Reapply the template after the schema-37
code runs; it revokes the earlier direct grants (until then the new code writes directly with
them), in one transaction that refuses below schema 37 before any revoke. Mount only
the original blob directory read-only, a private profile file and the required
model/key files. Do not mount messenger sessions, operator credentials or Docker
socket. Set memory, CPU and process limits on its container. Install ffmpeg for
audio inspection/conversion, and faster-whisper only if local ASR is selected.

Start with one permitted account and a small `max_jobs_per_day`. This bounds
attempts, including failures; it is not a monetary spending cap. Input bytes,
audio duration, text input, output tokens and runtime have separate limits.
Provider-side spending limits remain useful for API deployments.

For CPU-local faster-whisper, an operator may set `timeout_seconds` up to 3600;
the default remains 120. API and other model requests retain a 600-second ceiling.
The lease extends the configured deadline by 60 seconds, and the native child
and its descendants are terminated on expiry. Select a deadline appropriate for
the model and admitted audio duration; medium on CPU can need more than ten
minutes for a long recording. This does not remove input, memory or audio limits.

An original is verified by its size and SHA-256 before processing. Results are
committed only while the worker still owns its lease and the original digest and
message revision remain current. Replaced media or a deleted source invalidates
its derived result; caption-only revisions retain completed file artifacts and
cancel old processing leases. Failed jobs have bounded retries. The API reports
extraction and enrichment states independently and labels model-derived content.

## Launch

Create a private JSON configuration (mode `0600`) with `enabled: true`, an
explicit `accounts` list, `max_jobs_per_day`, and `profiles` keyed by `audio`,
`image`, or `document`. Each profile chooses `provider`, `model`, optional
`base_url`, and its limits. Cloud profiles also require `allow_cloud: true` and
an absolute `api_key_file` path. Local Whisper's `model` is an absolute path to
an already installed model directory.

Run `python -m messaging_service.enrichment --config /run/secrets/enrichment.json`
with `DATABASE_URL_FILE` pointing at the dedicated worker DSN and `PROBE_FILES`
pointing at the read-only originals. Keep configuration disabled until the
operator has chosen the account scope, model and resource limits. Search never
implicitly enables this worker.

## Protocol references

- [OpenAI transcription](https://developers.openai.com/api/reference/resources/audio/subresources/transcriptions/methods/create)
- [Anthropic vision](https://platform.claude.com/docs/en/build-with-claude/vision)
- [Gemini image understanding](https://ai.google.dev/gemini-api/docs/image-understanding)
- [Ollama chat](https://docs.ollama.com/api/chat), [cloud configuration](https://docs.ollama.com/faq#how-do-i-disable-ollama-cloud-features)
- [faster-whisper](https://github.com/SYSTRAN/faster-whisper)

For timestamp-capable transcription models, set `audio_response_format` to
`verbose_json`; models that support only plain JSON should keep the default
`json`. Missing segment times are reported as unavailable, never fabricated.
Local Whisper returns timed segments. Provider timestamp support depends on the
selected model.

Examples: [local profiles](enrichment.local.example.json),
[API profiles](enrichment.api.example.json). Both deliberately start disabled.
`memory_limit_mb` bounds each isolated native child on Linux; also use the
container memory limit. Models and account IDs in examples must be selected by
the operator.

The optional Compose overlay is `deploy/compose.enrichment.yaml`.
Select only one intended profile (`enrichment-local`, `enrichment-cloud` or
`enrichment-mini`); ordinary startup leaves all off. `enrichment-mini` runs
`enricher-mini` for `deedkit_inference` (the owner's Mac mini) on its own
networks, with no route to the API or the host and egress only to the mini on
443 (ANYM-688); its config (`deploy/enrichment-mini.example.json`) holds only
`deedkit_inference` profiles, its key is the directory `private/inference/`, and
the apply and rules are in `docs/operations/production.md`. Never run it together
with another profile: two workers would claim the same jobs. A worker with a
`deedkit_inference` profile also proves its own confinement before it claims and while it
runs, by trying to reach public addresses, an external resolver and other ports of the
mini, before every claim and every 15 seconds; if anything answers it claims nothing and exits with code 78
(`messaging_service/inference_confinement.py`). `enricher-mini` has restart policy `no`, so Docker never starts it
by itself; `deedkit-inference-worker.service` does when Docker starts, behind its gates (production.md "Stopped worker"). The local image includes hash-locked CPU
faster-whisper dependencies, with PyAV 18.1.0 pinned for decoder compatibility.
Neither image downloads models. Native smoke tests use synthetic speech and
network-disabled containers. The local profile has only the internal DB network.

The Ollama loopback examples require a daemon reachable in the worker's network
namespace; the overlay does not start an Ollama daemon. Configure that separately
or use an HTTPS endpoint. Do not assume host localhost is container localhost.
Cloud keys in the example map to files inside the overlay's read-only
`private/enrichment.keys` directory; configuration paths are container paths.


## Admission, scoped budgets and recovery (schema 20)

`max_jobs_per_day` retains its original meaning: a shared UTC-day request-attempt
ceiling across enrichment workers, including failures. Migration 020 preserves
`enrichment_budget`; it does not reset or multiply that ceiling. Optional
`profile_max_jobs_per_day` maps `audio`/`image`/`document` to additional daily
caps, and `account_max_jobs_per_day` maps configured account IDs to caps. Missing
subcaps impose no additional restriction. All workers sharing the database must
configure the same global ceiling; their separate modality/account allowances
belong in the subcaps rather than different global ceilings. Scoped counters start at migration;
pre-migration attempts remain counted by the global ceiling. Changing a model
or its limits does not reset either account or modality daily totals. These are
attempt limits, not token or monetary budgets.

Controls are independent for `(account, kind, profile_hash)`. An auth error,
missing local model, unavailable CLI or required CLI upgrade pauses that scope
until an operator fixes the configuration and explicitly clears its control,
or selects a changed profile. Rate limits pause it for 30 minutes. A mini that cannot take work
(`model_backpressure`, see `deedkit_inference`) pauses it for its cooldown and gives the attempt and budget back. Another
account/profile may continue within its own subcaps and the shared ceiling.
Worker restart does not clear controls. A paused or resource-blocked job is not
leased and does not consume an attempt. Safe `pause_reason`/`paused_until` labels
are separate from enabled/configured state and from source history coverage.
To resume a corrected profile, the operator can update its matching
`processing_control` row to `pause_reason=NULL, paused_until=NULL`; this is not
an MCP tool, and no automatic fallback sends private data to another provider.

Completed source-current ASR/image/document artifacts are reused when a profile
changes. Automatic retries target failed or unsupported work; they do not erase
a usable transcript or image description to try another model. Independent file
understanding keeps its last successful title and summary during refreshes and
reports the current refresh state and published model separately. This also
preserves useful content when the original has been deliberately evicted.

Unprocessed originals may also be deliberately evicted under the explicit
[retention policy](MEDIA_STORAGE.md). A matching prepared/completed eviction
journal pauses original-dependent ASR/image/document claims without consuming
attempts or inference budget. Verified re-publication resumes the queued work;
unknown missing/corrupt files retain normal failure diagnostics. The current
enrichment role needs read-only residency/journal grants, without mutation rights.
Independent understanding can still summarize available retained source text.

Whisper, Ollama requests and native PDF/image OCR share one `local-heavy` slot.
The database lease records ownership and expiry. All local worker containers
must additionally bind-mount the **same existing host directory**,
`./private/processing-locks`, at `/processing-locks`, set `PROCESSING_LOCK_DIR`,
and run under the same UID. Precreate the directory as UID/GID 10001 with mode
0700; its lock file is created as 0600. Missing, symlinked or unsafe directories
fail closed. The enrichment Compose overlay includes this mount; the indexer
must mount exactly the same host path. Different directories or separate hosts
are not a supported one-slot deployment.

An OS flock is inherited by the bounded Python child and native descendants.
Closing the parent's descriptor after a crash does not unlock a surviving
child; an expired DB lease alone cannot permit a second heavy process. Once all
children exit and the lease expires, another worker can recover the slot.
Light text/Office parsing and cloud inference may proceed while the slot is
occupied. File magic is checked for admission so a PDF named `.txt` cannot bypass
OCR serialization. Source digest/revision and attachment activity fences still
apply when committing either result. This is not a distributed GPU scheduler.
An independently administered Ollama daemon still needs its own CPU/memory and
request cancellation limits: worker subprocess limits do not bound that daemon.

Within each modality, fresh messages from the last 24 hours precede historical
backlog (audio also counts a note whose original became resident or was touched in
the last 24 hours as fresh, so an old note downloaded on request is not starved, ANYM-455); least-recently-started account/profile scopes alternate before older
work. Historical backlog may spend only `backlog_share` (a number from 0 to 1, default
0.7) of each daily cap: `max_jobs_per_day`, the modality's `profile_max_jobs_per_day` and the
account's `account_max_jobs_per_day`. Fresh work may spend the rest of the cap, so a large backlog
cannot use up the day before newer files arrive (production 11-06..08: the image backlog used the
whole image cap by mid-afternoon each day, and newer images waited for midnight UTC). Old attempts
are counted apart from fresh ones: in `processing_budget` an old job's attempt is stored under a
fixed backlog key in place of its profile hash, so every attempt is still in one row and the hard
caps read the same sums. Fresh work done first therefore does not use up the backlog's share. The
share is a whole number of jobs per cap: rounded down, at least 1, and below the cap when the share
is under 1, so fresh work keeps at least one slot of any cap of 2 or more. The backlog drains in any
hour with no fresh work due, up to its share. `1` restores the old shared cap and `0` stops old work.
The share and the caps count model calls only: a duplicate whose ready result is copied (ANYM-912) spends no
attempt, so an old duplicate is still copied after the backlog's share is spent.
The worker compares candidates across modalities instead of always
exhausting the first configured modality. This does not yet introduce a public
on-demand job-priority API. Local native children exit after every file; Ollama
requests already use `keep_alive=0`. No model, cloud profile or autoscaler is
enabled by this change.

Audio lane (ANYM-838): a worker configured for audio and at least one other kind,
none of them a local heavy profile (`faster_whisper`, `ollama`), claims audio in a
second thread, so a voice note never waits behind an image or document job (on the
Mac mini, speech and vision run on separate slots). At most two jobs run at once, one
per lane; budgets are counted in the same claim transaction as before, and only the
main lane publishes and unpublishes the profiles. The idle audio lane reads the newest
`attachment.ready` event id every second and runs its lock-free candidate read only
when that id moved, after a job, or every 10 seconds; it takes the residency and
admission locks only to claim what that read found. A stop requeues the jobs of both
lanes; an error in either lane ends the worker through the same stop path.

### On-request chats (ANYM-805)

Owner 2026-11-06: files of personal chats are recognized by themselves; files of groups and channels
only when an agent asks, unless an agent's subscription names the chat (the chat kinds and the
exception: MEDIA_STORAGE.md, "Group and channel chats: download and recognition on request"). The
enrichment worker's claim and the indexer's claim (text extraction and OCR) skip a withheld file: a
file of an on-request chat whose media job carries no agent request. A request is any accepted
`request_file_download` for the file; it sets the job's `request_key_digest`, which nothing clears.
A file without a media job (a legacy or restored original) is never withheld, since no request could
be recorded for it.

A withheld row that is not leased is never changed, leased, counted against a budget or marked
failed; the indexer's attempt-ceiling sweep skips it too, and `enrichment_health` does not count it,
so withheld work raises no stall alarm. A lease taken before the rule took effect ends as before. The
row becomes claimable when an agent asks or its chat becomes subscribed, so group and channel rows
queued before the rule leave the queue without a failure. The enricher and the indexer read only the
columns this needs (chat kind, the subscription's state and chats, the job's request digest) through
column grants in their role files.

`get_sync_status` (ANYM-812) reports recognition by the same rule: `files.enrichment.queued` counts
only due rows; withheld and evicted ones are `available_on_request`, documents without text
`not_recognizable`, and kinds with no configured profile stay `unconfigured`. A configured kind is a `processing_profiles` row
observed within 7 days, enabled or not, so a stopped worker's files stay due (`coverage.state:
behind`, `enrichment_no_worker`) rather than reading as recognition switched off. A row failed or
unsupported under an older profile reads as failed or unsupported until the worker with a changed
profile takes it again.

### New images only (ANYM-887)

Owner 2026-11-09: «картинки только новые ... только новые обрабатывали в реал тайме» and «отсеять из очереди совсем не
нужное». An image is described by itself only while it is new. An image whose message was sent more than
`IMAGE_DESCRIPTION_DAYS` (7, media_policy.py) days ago, or a sticker (message metadata `media_kind: sticker`), waits
for an agent's request whenever its media job carries none. The request is recorded like ANYM-805's: any accepted
`request_file_download` sets the job's `request_key_digest`. For these images this now also happens in personal chats,
even when the original is already on disk. A requested image counts as fresh for the claim order and the backlog share.

The rule is description only. Text extraction and OCR keep their behaviour, and audio and documents are unchanged.
Nothing is changed, failed or deleted: a waiting row keeps its state and becomes claimable once an agent asks. An
attachment without a media job keeps the old behaviour. `get_attachment` and message reads show `enrichment_status:
available_on_request` with `recognition_reason` (`image_outside_description_window` or `sticker`) and
`recognition_next_action: request_file_download`; ANYM-805's reason wins in an unsubscribed group. No ANYM-806
retention applies. `get_sync_status` counts these images under `available_on_request`, and `enrichment_health`
raises no alarm for them. The window is a module setting, not a worker config key, because the API, the health
check and the worker must read the same window. A group's or channel's images are described only when a
subscription names that chat; a dynamic (all-chats) subscription lists no chat, so it does not count (ANYM-805).

### Reuse of a ready result for a duplicate file (ANYM-912)

Owner 2026-11-09: «отсеять из очереди совсем не нужное». At claim time, before a model call, the worker looks for a
ready image, audio or document result of the same account with the same file (`source_digest`, read through the
attachments digest index) and the same `profile_hash` (provider, model and profile settings), whose source attachment
is still current. If it finds one, it copies title, summary, text, truncation and metadata into the claimed row under
a normal lease and publishes the same readiness event. It counts no model attempt, daily budget, processing budget or
start time and takes no local resource lease. The copy's metadata adds `reused: true` and `reused_from` (the first
source attachment id; a copy of a copy names the original). Agents see only `enrichment_coverage.reused` in
`get_file_text`; `reused_from` is operator evidence for SQL and never leaves the database through an agent read or an
event. `enrichment_health` counts the mini's ready copies of the last 24 hours per kind (`reused_24h`, a count only).
A reuse lease of a row whose attempts belong to an older profile starts at 0 attempts, so an expired reuse lease is
claimed again and never ends as `retry_exhausted` without a model attempt. No match means a normal model call. Reuse never crosses an account or a profile, and happens only for work the
worker claims: a copy that waits for a request (ANYM-805, ANYM-887, ANYM-890) stays on request and gets the copy at
once after the request. The daily cap check still runs first, so at the cap a duplicate waits like other work. Text
extraction and OCR are unchanged; nothing is deleted, and a source that is superseded later leaves its copies as they
are.

### Voice notes only in groups and channels (ANYM-890)

Owner 2026-11-09: «отсеять из очереди совсем не нужное»; on groups and channels: «их лучше не надо». In a group or
channel chat (ANYM-805's chat kind, subscribed chats included) an audio file (`audio/*` or `application/ogg`) whose
message metadata names a `media_kind` other than `voice` (music and recordings sent as `audio`, or audio sent as a
`document`) waits for an agent's request exactly like ANYM-887's images: the same request evidence, statuses
(`available_on_request`, `recognition_reason: audio_not_a_voice_note`, `recognition_next_action:
request_file_download`), sync-status bucket and health exclusion; a requested file counts as fresh. Voice notes
(Telegram voice, WhatsApp `ptt`) are transcribed by themselves in every chat. Personal chats, chats of unknown kind
and audio whose message names no media kind (legacy or imported rows) are unchanged. Video, round video notes
included (both adapters record them as `video`), is not read by this rule. ANYM-805's reason wins in an unsubscribed
group. Only work that has not started waits: a queued non-voice group audio row reads `available_on_request` after
the release without anyone asking, and becomes `queued` again once an agent asks.
