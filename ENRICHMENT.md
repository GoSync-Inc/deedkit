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
| Audio transcription | faster-whisper, preinstalled model directory | OpenAI or compatible transcription endpoint |
| Image descriptions and visible text | Ollama vision model | OpenAI-compatible chat, Anthropic Messages, Gemini |
| Document title and summary | Ollama text model | Same text/vision API connectors |
| Optional document analysis/image description | — | Codex CLI with private ChatGPT subscription auth |

The Codex connector is implemented locally but not deployed or enabled. Read
[its isolation and rollout requirements](CODEX_ENRICHMENT.md) before using it;
it is cloud inference and shares subscription usage with other Codex work.

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
period. Fairness
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
(apply `messaging_service/enrichment_role.sql` as the database owner). Mount only
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
Select only one intended profile (`enrichment-local` or `enrichment-cloud`);
ordinary startup leaves both off. The local image includes hash-locked CPU
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
or selects a changed profile. Rate limits pause it for 30 minutes. Another
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
backlog; least-recently-started account/profile scopes alternate before older
work. The worker compares candidates across modalities instead of always
exhausting the first configured modality. This does not yet introduce a public
on-demand job-priority API. Local native children exit after every file; Ollama
requests already use `keep_alive=0`. No model, cloud profile or autoscaler is
enabled by this change.
