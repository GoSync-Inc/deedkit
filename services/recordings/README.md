# Recording integrations

An optional, independent HTTP backend for the DeedKit gateway. Version 0.1 reads
Omi conversation metadata, transcripts and structured overviews on demand. It
has its own Python 3.13 environment/lock. By default it has no database,
collector or durable content cache. The optional text archive below adds private
SQLite state and Omi/PLAUD polling only for explicitly configured archive accounts. The PLAUD adapter reads its official remote MCP; its distinct
authorization, data route and limitations are described below.

## Install and authorize

Install the package in its own environment:

```sh
uv sync --project services/recordings --locked
```

The owner creates a `conversations:read` Developer API key in Omi Settings →
Developer → Create Key. This is a separate owner action; installation does not
log in, activate a provider or fetch recordings. Store the key as one line in a
private file. Do not paste it into agent chat, Git, examples, process arguments,
environment variables or logs. Use a private directory outside the checkout,
mode 0700; configuration, token files and cursor secret must be regular files,
mode 0600, with absolute paths. The service only reads them.

Create a separate random backend key per agent, store its SHA-256 digest in the
configuration, and give the key itself only to that agent's gateway credential
file. Generate a random cursor signing secret of at least 32 bytes. The following
is a structural example; replace paths, labels, IDs and digest locally:

```json
{
  "version": 1,
  "cursor_secret_file": "/private/recordings/cursor.key",
  "accounts": [
    {"id": "omi-a", "provider": "omi", "label": "My Omi", "token_file": "/private/recordings/omi.key"}
  ],
  "principals": [
    {"id": "agent-a", "key_sha256": "<SHA-256 of the separate backend key>",
     "expires_at": "2026-12-01T00:00:00Z", "accounts": ["omi-a"],
     "permissions": ["recordings:read", "recordings:content"]}
  ]
}
```

Start the private service with the CLI (one worker):

```sh
uv run --project services/recordings deedkit-recordings --config /private/recordings/config.json
```

It listens on `127.0.0.1:13234` by default. Keep it private; bind only to the
explicitly configured private service network when the gateway runs elsewhere.
Do not expose the backend publicly or log request headers/bodies/URLs at an ingress.
The CLI disables HTTP client URL logging and Uvicorn access logs. Each process
admits four provider calls; run one process for the documented account budget.
The 12-second deadline includes body reading and cancellation releases admission.
On-demand reads have no provider retries. Without archive configuration there
are no background provider calls. Unauthenticated requests and
capabilities do not contact either provider.

## Gateway opt-in

Upgrade the gateway **before** adding recordings to a policy; old gateways reject
this module name. Configure its existing private policy with:

- `deployment.modules`: add `recordings`, and a private backend URL under `backends.recordings`.
- The profile's exact `tools`: `recordings_capabilities`, `recordings_list`, `recordings_read` as needed.
- The principal's `credentials.recordings`: the absolute private backend-key file and its SHA-256.
- `grants.recordings`: `recordings:read`, plus `recordings:content` for content access.

Backend permissions/account ceilings still apply. `recordings:content` alone does
not grant metadata or content. Existing profiles gain nothing automatically.
Use `/mcp/modules/recordings`, or select recordings on the common MCP route.
The source package is run explicitly. Production runs it as the published `recordings` image through
`deploy/compose.recordings.yaml` (docs/operations/production.md, "Recordings"); nothing there is enabled by
install or merge. No production configuration is changed by install.
Undo: remove the module/tools/grants/credential from the policy before gateway
downgrade, then stop only this owned service. Revoke the key in Omi separately if
needed. Configuration/key/grant removal is checked on the next backend request;
already admitted provider requests can finish. Rotate cursor secret to invalidate
continuations. Atomically replace configuration in its private directory; mount
that directory read-only, not a single file whose inode would stay stale.

## Omi read contract

The private backend uses authenticated `POST /v1/capabilities`,
`POST /v1/list_recordings` and `POST /v1/read_recording`; the gateway tools provide
the reviewed agent-facing schemas. All source content is untrusted data.

`recordings_list` takes `account`, `limit` (1–50, default 25), optional
`start_date`/`end_date`, and `cursor`. A date bound is a timezone-aware ISO instant
with a `T` (`2026-11-03T10:00:00+02:00`) or a plain `YYYY-MM-DD` (ANYM-754): a plain
`start_date` means 00:00:00 UTC of that day, a plain `end_date` its end
(23:59:59.999999 UTC). Anything else (a naive time, a date with a zone but no time,
a month alone) and a start after the end are `invalid_parameters`. Date bounds
are inclusive and compare **created_at**, not recording start.

The bounds go to Omi as `start_date`/`end_date` but are not trusted: the service
drops every row whose `created_at` lies outside them and keeps a row whose
`created_at` is missing, naive or unparsable (it cannot be placed, so it is never
hidden by a guess). The promise is "drops what it can place outside the dates", not
"only rows inside". A dated result adds
`date_filter: {requested: true, filtered_locally: <n>, pages_read: <k>}`. When a
page leaves no row, the service follows the provider's continuation itself, at most
5 provider pages per call and never into a provider call that could outlast the
gateway's deadline, until a page leaves a row or the provider has no more. No order
of Omi's pages is assumed. `next_cursor` and `pagination` are the last page read, so
continue exactly as without dates; an empty `items` with a `next_cursor` means "keep
going". `filtered_locally > 0` means the provider did not apply the dates. Without
dates the result is unchanged: one provider page, no `date_filter`. Inspect
`pagination`: unknown or malformed evidence stays null, truncated pages remain
incomplete even when empty. `complete` concerns this provider traversal's end,
not account/archive coverage. Provider order can change and lists can be stale
for ten seconds. Apart from the dated follow above, no hidden auto-pagination occurs. Follow `next_cursor` with the
same filters and limit. Missing provider continuation means no safe next page;
it never means that an unknown traversal is complete.

`recordings_read` takes `account`, `recording_id`, `kind` (`transcript` or
`summary`), `page_chars` (100–10000, default 4000), and optional `cursor`.
Transcript text is newline-delimited JSON segments preserving supplied offsets
(negative ones too: device-buffered speech before the live pin) and speaker labels
(`speaker_name`, else `speaker_id`), split at character boundaries. A segment with
Omi's unplaced sentinel `start == end == -1.0` has `start`/`end` null and
`"placed":false`. A page may split one JSON
segment; concatenate pages from the same digest before parsing it. The overview
is provider-authored summary text. No extra transcription/model is invoked.
`content_state` distinguishes `available`, `empty`, `pending`, `unavailable`. An Omi
summary with an empty title and overview under 24 hours old is `pending` (Omi's
lazy processing; its GET omits `processing_deferred`), older it is `empty`.
Omi raw audio is `unsupported`: its Developer API documents no download route.
The service does not invent one.

Continuations last 15 minutes, are authenticated and bound to credential,
provider account/token identity, operation and exact arguments. They are encoded,
not encrypted; do not log or share them. They confer no access on their own.
Content is fetched again on each page; a different content digest returns
`source_changed` (409), requiring a fresh read. No cached transcript is mixed with
a newer one. Provider responses over 2 MiB fail `response_too_large` without
partial content. Request bodies are capped at 16 KiB. Errors contain fixed codes,
never provider response bodies or credentials.

`invalid_provider_response` (HTTP 503, not retryable) adds `detail_code`, one word
from a closed vocabulary that names the rejected part of an Omi body, never its
value (ANYM-753). PLAUD and local-archive failures keep the bare code. The gateway
relays only these words:

| `detail_code` | Rejected part; what the operator checks |
|---|---|
| `not_object` | The body (or a list row) is not a JSON object; route or contract change. |
| `id_invalid` | The recording `id` is missing or not a provider ID. |
| `id_mismatch` | The read returned another recording than asked. |
| `metadata_field` | `created_at`, `started_at`, `finished_at`, `language` or `source` is not a string of at most 128 characters. |
| `segment_shape` | A transcript segment is not an object. |
| `segment_text` | A segment has no string `text`. |
| `segment_speaker` | A segment speaker is not a string or integer of at most 256 characters. |
| `segment_offset` | A segment `start`/`end` is not a finite number in ±1e10. |
| `list_shape` | The list body is not an array, or holds more rows than asked. |
| `list_pagination` | The page holds more rows than the provider's own effective limit. |
| `encoding` | The provider sent a compressed (`Content-Encoding`) body. |
| `not_json` | The body is not valid JSON. |
| `projection_error` | The body parsed, but its values could not be projected or encoded (for example an unpaired surrogate). |

The Omi adapter uses only documented HTTPS GET routes at `api.omi.me`. Credentials,
recording IDs, bounds, limits and provider cursor go to Omi; metadata, requested
content and continuation tokens go to the authenticated gateway/caller. Redirects,
environment proxies and encoded provider responses are refused. No arbitrary URL,
upload, OAuth/login, deletion, model call or telemetry exists. On-demand requests
do not persist content; only the separately enabled archive collector does.
An agent consuming the returned text may put it in its own model context.

Official contract: [Omi conversations API](https://docs.omi.me/doc/developer/api/conversations).
Synthetic tests verify the implementation, not live account authorization,
provider coverage, raw audio availability or a production deployment.

## PLAUD (ANYM-649)

Use `provider: "plaud"` and a private `token_file` containing a bearer access token
issued specifically for the official remote MCP audience. Obtain this through the
vendor's supported remote-server OAuth connection with the owner's hands. This
package does not implement login, refresh, token extraction or device pairing;
CLI/device tokens are not assumed interchangeable. A missing/expired token returns
`provider_auth_required`; replace the private file after owner authorization.
Source acceptance uses synthetic fixtures only: remote OAuth issuance, actual
server version and live response shape still require separate activation evidence.

DeedKit sends OAuth authorization, protocol metadata, fixed tool names and the
requested recording/page identifiers only to `https://mcp.plaud.ai/mcp`. The vendor
[documents US-hosted HTTP transit](https://docs.plaud.ai/plaud-mcp-cli/mcp), governed
by its privacy policy. Its remote MCP can resolve linked transcript/note bodies
and has its own logging/telemetry policy; this adapter cannot disable or verify
vendor internals. DeedKit itself makes no linked-body/audio fetches, model calls or request/content
logs. On-demand PLAUD reads do not write content. No Node subprocess/package is installed.

The adapter supports MCP protocol `2025-06-18`, JSON and bounded SSE responses,
structured tool objects or the exact JSON wrapper published in `@plaud-ai/mcp`
0.3.14. Unexpected shapes fail closed; a vendor change may require an update.
Each operation creates and initializes a short-lived remote protocol session;
cleanup shares the overall deadline and gets at most 200 ms. Failed cleanup does
not replace the result. No retry, reconnection, background reader or session state
is retained. Omi and PLAUD share the four-active-read budget in this process.

- `list_recordings`: PLAUD limits 10–50; no date filters in this slice. Metadata is
  restricted to id/name/created_at/start_at/duration (vendor duration is ms).
  Request one explicit page per signed cursor. The unfiltered API has no pinned
  completion schema, so `complete`/`has_more` stay null, even on an empty page.
  A nonempty page offers another explicit page; it never scans a full archive.
- `read_recording`: transcript calls `get_transcript` once with the transaction
  block and a 500-segment limit. A provider continuation returns
  `transcript_window_exceeded`, with no partial text. Summary calls `get_note` and
  projects all bounded resolved note bodies, dropping link metadata. Linked-body
  failures are `unavailable`; unknown text shapes are errors. A missing transcript
  block is unavailable. Local character pages reuse Omi's digest check; source
  changes require restarting. Concatenate pages only when digests match.
- `audio` / gateway `recordings_audio`: requires **both** `recordings:read` and
  **`recordings:audio`** in the backend principal and gateway grant, with the tool
  in the gateway profile and the account in the backend principal. Returns only
  the validated HTTPS `get_file.presigned_url` plus its vendor-reported 24-hour
  lifetime. Missing links are unavailable; Omi audio stays unsupported.
  **This is a bearer download link. DeedKit revocation does not revoke an already
  returned link before vendor expiry.** The authorized caller handles download and
  storage; no audio bytes flow through this service. Metadata/content operations
  do not return the audio inventory or signed link.

No existing profile gets this new audio scope/tool. Remove the gateway tool/grant
and backend account access to stop new link issuance; an issued link's lifetime
is controlled by PLAUD. For rollback, remove PLAUD accounts/audio configuration
before downgrading the recordings package, and remove the audio tool from gateway
profiles before downgrading its catalog. Keep Omi configuration unchanged.


## Optional private text archive (ANYM-651)

Add `archive` to the private configuration and an explicit `archive_namespace`
to each account to be archived:

```json
{
  "archive": {
    "directory": "/private/recordings/archive",
    "poll_seconds": 300,
    "max_bytes": 1073741824,
    "free_reserve_bytes": 1073741824
  }
}
```

This is a fragment, merged with the configuration above. In the container image the configuration
directory is `/run/deedkit/recordings` (read-only) and the archive directory is `/data/archive`; the
`/private/...` paths in these examples are for a source run (`deploy/recordings-archive.example.json`). `archive_namespace`
is 32 lowercase hexadecimal characters, generated once per provider identity.
Keep it across token rotation. **When reusing a local account ID for another
provider identity, generate a new namespace.** The service cannot infer whether
a replacement token belongs to the same person. Provider, account ID and this
namespace jointly isolate stored history. An account without this field has no
background collection. PLAUD uses the same opt-in collector and stable-namespace isolation. No new
account gains archive collection automatically.

Use a canonical absolute directory path with no symlink components. Its existing
parent must be private operator-controlled storage outside the checkout. The
service creates the archive directory as 0700 and SQLite/lock files as 0600;
it rejects unsafe existing modes, foreign ownership, symlinks, unknown schema,
corruption and a second writer. Run exactly one worker. Archive-only configuration validation, startup and
collector errors stay isolated from the existing on-demand HTTP operations.
Stopping this owned process cancels its collector, waits for bounded SQLite work
and releases its writer lock; no other service or provider session is stopped.

One serial collector uses at most one of the four aggregate provider-call slots.
Polling revisits the newest page and advances a persisted history traversal.
Discovered IDs and continuation commit atomically; restarting can repeat a page
but cannot skip queued IDs. New, failed/pending and previously saved records each
receive a bounded share of work, interleaved with persisted rotating priority
across interrupted passes. Discovery failures or a saturated queue do not
prevent processing already queued records; rejected discovery leaves its
checkpoint unchanged. Provider coverage always remains `unknown`;
a traversal ending is not proof of a complete account archive. Checks repeat
before every provider call and commit; removing an account, changing namespace
or rotating its token discards any in-flight result. Disabling `archive` stops
new collection. Changing its directory requires a service restart.

Only whitelisted metadata, provider IDs, transcript speaker/offset text and the
vendor's overview are stored locally. No audio, bearer download URL, token or raw
vendor inventory is stored. No transcription, model API, paid processing or
inbound webhook runs. A ready transcript can be accepted while summary processing
is pending. Empty, pending, malformed or failed observations never erase an
accepted nonempty text part. Each part keeps its last accepted digest/time and
its latest attempt state/error; these may differ. Only actual usable text changes
produce reference-only journal events. Replays do not duplicate records/events.
Latest accepted text is retained indefinitely; provider deletions do not purge it.
No content or journal retention deletion is automated; the owner's deletion is the operator command below (ANYM-730).

Limits: 60 seconds per record, 60 seconds per Omi-only pass (300 seconds when
PLAUD is configured), 2 MiB combined accepted text per record,
10,000 not-yet-attempted queued records per namespace, 100 ms SQLite busy wait and
a 2-second SQL progress deadline. Poll interval is at least 60 seconds. Archive
byte ceiling defaults to 1 GiB (minimum 128 MiB); filesystem free-space reserve
defaults to 1 GiB (minimum 256 MiB). Admission counts SQLite/index/WAL files and
reserves 64 MiB transaction headroom; SQLite pages are capped at one third of
the configured archive ceiling to leave WAL space. At capacity or SQLite FULL,
writes and checkpoint advancement stop with `archive_capacity`; accepted text
and prior checkpoints survive rollback. Freeing space or raising the limits
allows a later pass to resume. This archive does not reserve disk for other
services beyond the configured filesystem reserve.

The full-text index and reference-only journal commit with text and revision.
The local search/context tools below are separately opted into gateway profiles.
Outbound subscriptions remain a separate follow-up slice; no public ingress is added.
The private authenticated backend `POST /v1/archive_status` accepts an explicit
`account` within the current principal ceiling and requires `recordings:read`.
It returns counts, last success and fixed durable/runtime errors without reading
provider tokens or contacting a vendor. In particular, `archive_capacity` stays
visible even when no error-state write is possible, and clears after recovery.

### Omi full backfill (ANYM-724)

For an Omi account the collector downloads the whole conversation history inside Omi's published hourly limits
(per credential, fixed windows that start at the first counted request; conversation reads 60/h shared by list, detail and
transcript, transcript fetches an additional 25/h). Enumeration is cheap: history pages of up to 200 IDs with list metadata
only (never a transcript), then transcripts one detail call at a time, newest first, with conversations of the last 24 hours
ahead of the older backlog.

```json
{"archive": {"max_queued": 10000, "omi_budget": {"transcript_per_hour": 18, "reads_per_hour": 45}}}
```

- `omi_budget` is the collector's own share (validated 1..25 and 12..60). A reservation is written before each provider call, so
  the collector never starts a call that would take its own use above it. Its window slides: at most N reservations in any
  hour plus a 75 s drift margin, which holds for every fixed window Omi may have started (a crash or a failed connection can
  leave our reservation earlier than Omi's first counted request). On-demand reads (`recordings_list/read`) draw on the
  same windows: they are counted in the same ledger but never refused by it, so agents may spend the rest (and more; Omi then
  answers 429). The ledger is keyed by a hash of the credential, so two local accounts with one token share it. If the ledger
  is unreadable or the clock moved back by more than a minute the collector stops calling that credential and reports
  `ledger_uncertain`; on-demand reads and local search are unaffected. Usage by other tools on the credential is unknown to
  the ledger: the defaults leave headroom and the 429 handling is the final control.
- A 429 pauses the collector for that credential for `Retry-After` seconds from its arrival (missing or invalid 3600, at most
  6 h; every reservation checks the pause, also one set by an on-demand read meanwhile): no call, no
  error observation, no record's `failures` increases. Another credential and PLAUD keep running. On-demand reads answer
  `provider_rate_limited` with `retry_after_s`.
- The head (newest pages) is separate from the history traversal. It is read at most every 5 minutes, may use all reads of the
  budget, and 6 reads per hour plus a queue slice of 100 IDs (`max_queued`, default 10,000, at most 200,000) are reserved for
  it: a full queue pauses only the history traversal. The head page is limited to the free slice and resumes from its own cursor
  when more new IDs wait (`head_backlog`; `head_slice_full` after an hour), so no ID is skipped. A truncated page is read
  again, never taken as the end, and only affirmative evidence (no more pages, no `X-Omi-List-Truncated: true`, an effective limit) ends it. Omi sends that
  header only as `true`, so its absence is not truncation; a malformed value proves nothing. A
  finished history is walked again once a day (list rows only) so a changed title or overview of an older record is noticed.
  `reached_end` is a fact about the traversal only; `coverage` stays `unknown`.
- A saved record is read again only when its list row changed (overview presence/digest or the deferred flag), or while a part
  is pending: every 3 hours during the first 24 hours after creation, then daily until 14 days, then only on a change. Nothing
  is re-read hourly. A title-only change updates the title and index without a provider call. Failed reads keep the bounded
  backoff and share the budget; `response_too_large` backs off a day.
- An Omi record that answers 404 twice in a row (merged away, deleted or locked) becomes `provider_missing`: its stored text
  stays, it is checked at most weekly with `include_transcript=false` (no transcript slot), and it is due again once a list
  row carries its ID or that check finds it.
- `archive_status` (read scope; no titles or text) adds `traversal` (`state`, `discovered_total`, `passes_completed` = history
  list pages committed, `last_page_at`), `with_text`, `queued`, `pending`, `failed`, `provider_missing`, `throttled_until`, `usage` per bucket
  (`used`, `limit`, `resets_at`: when the oldest counted call leaves the sliding hour), `db_bytes` (all SQLite files incl. WAL), `bytes_ceiling`, `projected_db_bytes`
  (the larger of the measured pages and 3 x the accepted text bytes per text-bearing record, times `discovered_total`, plus
  the measured empty-schema size; an estimate),
  `capacity_warning` (projection above one third of `max_bytes`) and `eta` (only after an hour of observed rate).
- Duration is documented, not promised: at 18 transcript calls per hour the backfill takes about conversations / 432 days
  (1,000: 2.3 days; 5,000: 11.6; 10,000: 23.1), longer when agents use their share or Omi throttles. Size `max_bytes` as
  3 x `projected_db_bytes` (all SQLite pages count toward the one-third page cap); the free-space reserve applies on top.
  The production example uses 4 GiB (about 1.33 GiB of pages); set the real value from the first discovery count.

### Consistent local snapshot and restore probe

The archive is **not automatically enrolled in the existing backup system**.
Create a consistent private snapshot with the installed CLI while the writer runs:

```sh
deedkit-recordings --config /private/recordings/config.json --backup-to /private/snapshots/recordings.sqlite
```

The canonical destination parent must already exist as a private 0700 directory;
the destination must be new and outside the live archive directory. The command
uses SQLite Online Backup with a 5-second deadline, creates a 0600 file, never
reads provider tokens and never contacts vendors. Do not copy an active SQLite
file or its WAL separately. This snapshot is local; it is not a delivery receipt.
For a restore probe, copy that snapshot into a separate private scratch directory
as `archive.sqlite`, use a synthetic configuration and run `PRAGMA integrity_check`
and synthetic text/event readback. Production restore/backup enrollment is a
separate operation under the existing backup policy. Rollback of this feature:
remove top-level `archive` and every account's `archive_namespace` from
configuration, then stop only this owned service before package downgrade; keep the private state intact for re-enable or recovery.

### Automatic PLAUD text ingestion (ANYM-654)

The collector uses only `list_files`, `get_note` and `get_transcript` at the fixed
official remote MCP origin. It never calls `get_file`, follows a linked body URL
itself or obtains audio links. The vendor's US transit and internal linked-body
resolution disclosure above still applies. Discovery polls unfiltered pages,
revisits the head and resets traversal after an empty page; empty pages still
leave coverage `unknown`. Pending/error work receives bounded retries.

PLAUD transcript ingestion follows contiguous 500-segment windows, checks file,
block, offsets, totals, cursor progress and final coverage, and accepts at most
10,000 segments / 2 MiB combined accepted text. It reads the entire transcript
twice and requires identical canonical segment content and total. Accepted text
is labelled `observed_equal_reads_not_atomic_snapshot`: equal observations detect
many mutations but cannot prove an atomic vendor snapshot. A changed, looping,
incomplete or oversized observation never replaces previously accepted text.
The existing on-demand one-window 500-segment refusal stays unchanged.

Each part commits immediately after its own complete attempt. Vendor notes are
attempted separately first, so a failed/long transcript cannot discard ready
notes; a transcript does not wait for notes to become ready. Note requests have
a 12-second budget, full transcript verification 40 seconds, within a 60-second
record budget including commits. This service creates neither transcript nor
summary; it reads the vendor's existing text and makes no paid processing call.

### Local archive tools (ANYM-652)

Explicitly add `recordings_search`, `recordings_context` and/or
`recordings_archive_status` to the chosen gateway profile. No existing profile
gains these tools or scopes. The module must be deployed and both the gateway
and backend principal must grant `recordings:read`; search/context additionally
require `recordings:content`. Every call checks the current backend key, expiry,
account ceiling and namespace before selecting data and again before returning
it. The backend manifest must explicitly implement each operation. Upgrade the
recordings backend and gateway catalog before adding tools to a private profile;
remove the tools before downgrading the gateway. These calls read no provider
token and make no provider, model or embedding request.

- `recordings_search({account, query?, limit: 10, created_after?, created_before?, speaker?, order?})`: searches the
  local FTS5 index of titles, transcripts and summaries. The index holds plain text: the spoken words, not the stored
  JSON keys, and not the speaker labels (ANYM-725). A query is 1–256 characters, containing 1–16 Unicode literal words of
  at most 64 characters each, joined with AND. Operators, column selectors and wildcards are not an FTS language
  exposed to the caller. Optional filters, all ANDed with the query:
  `created_after` / `created_before` (timezone-aware ISO instants, inclusive, compared in UTC with the provider's
  `created_at`; a reversed or malformed range is `invalid_parameters`), `speaker` (one literal label, at most 64
  characters, case-insensitive exact match: the label is whatever the provider sent, such as `SPEAKER_01` or a name
  the owner set in Omi; there is no identity resolution or voice recognition) and `order` (`relevance`, the default, or
  `newest`). Without a query, at least one filter is required (`query_or_filter_required`, 422) and the call lists the
  newest matching recordings (browse; the snippet is the start of the summary or transcript). Records without a usable
  `created_at` are returned by calls without a date filter (after the dated ones in newest order) and excluded by a date
  filter; `recordings_archive_status` counts them as `undated`. Returns at most 50 hits with a snippet of at most
  320 characters, stable `recording_ref`, revision and per-part digest, freshness and attempt state, plus
  `created_at` (fixed-width UTC, microseconds), `started_at`, `duration_s` (whole seconds; Omi `finished_at - started_at`,
  PLAUD `duration` in milliseconds; null when missing, unparsable or negative) and up to 8 `speakers` labels (at most
  256 labels per recording are indexed, 64 characters each). PLAUD numeric timestamps have no documented unit: below
  10^11 they are read as Unix seconds, from 10^11 up as milliseconds (a heuristic), accepted only in the years 2000–2100.
  Everything in a hit, labels included, is untrusted source data inside the `source_data: true` envelope. Relevance
  order is a title hit, then a summary hit, then a transcript hit, then the newer revision (no corpus statistics, so
  other accounts cannot influence it). No total count or cross-account search is returned. This is lexical search over
  observed local text; an empty result is not proof of vendor history completeness.
- `recordings_context({account, recording_ref, kind: "transcript", page_chars: 4000})`:
  reads the accepted transcript or summary in 100–10,000 character pages,
  retaining original speaker/offset text where the vendor supplied it. Results
  are labelled `source_data: true`; they are evidence, never agent instructions.
  `part` separates the last accepted digest/time from the latest failed or
  pending attempt. Continue with identical arguments and `next_cursor`. The
  signed 15-minute cursor binds key, account/provider/namespace, reference,
  kind, exact arguments, revision and digest. Changed revisions return
  `source_changed` (409); revoked grants, replaced namespaces and foreign
  references are refused without content or existence details.
- `recordings_archive_status({account})`: read-only metadata counts, coverage,
  freshness and fixed archive errors, with no titles or text. Content scope is
  not needed. Missing archive settings return `archive_disabled`.

#### Schema 5 and owner-requested deletion (ANYM-730)

A new archive starts at schema 5; a published schema-4 archive gains one table at the writer's start (additive, no record
rewritten): `tombstones(provider_hash TEXT PRIMARY KEY)`, the SHA-256 of `namespace NUL provider_id` (no content; the same
value `backup_recordings_continuity.provider_hash` computes). A schema-3 archive reaches 5 at the first start after its
index is published. Older packages refuse schema 5 (`archive_schema_unsupported`); rolling back means disabling archive
collection and keeping the state files. The online backup and the backup tooling accept schemas 1-5.

`deedkit-recordings --config <file> --delete ...` is an operator command (run as the data-deletion gates of process.md section 2
require, on the owner's request; not an HTTP route or an agent tool). The service must be stopped: the command takes the
single-writer lock. Select by `--ref <recording_ref>` (repeatable) or by `--account <id>` with `--created-from` and/or
`--created-to`; both together, or neither, is refused. Without `--yes` it only previews (the receipt's `selected`). With `--yes` it
deletes in batches of 200 records, one transaction each: the rows, their full-text entries, speaker labels and journal events,
and records one tombstone per provider ID. The first batch also raises the archive `incarnation` by one: a bundle sealed before the
deletion then carries a lower incarnation and the verifier refuses it (`archive_incarnation_lower`), so a pending pre-deletion
bundle is cancelled. After the last batch it rebuilds and merges the full-text index, runs `VACUUM` with `secure_delete` on (a
temporary file stays in the private archive directory), truncates the WAL, and reports `sanitized` only when no free page is left,
`quick_check` is `ok` and the WAL is empty. `--snapshot-to <new absolute path>` is required with `--yes`: the command writes the post-deletion snapshot and the receipt describes that file. The first batch also sets a durable `sanitize_pending` mark in `meta`, cleared only after the rewrite is verified (no free page, `quick_check` ok, `wal_checkpoint(TRUNCATE)` not busy, empty WAL); while it is set, every Online Backup (`--backup-to`, the snapshot) is refused `archive_sanitization_pending`, because `secure_delete` alone leaves deleted text in FTS5 storage. A crash or a pinned reader (`deletion_sanitization_incomplete`) is finished by repeating the command with `--yes`. It needs
free space of about twice the archive plus the reserve (`archive_capacity` otherwise) and refuses while the index builds
(`archive_index_building`) and while an event feed exists (`deletion_feed_unsupported`: the feed owns the journal identity, ANYM-726).

The receipt is one JSON line: `selected`, `deleted`, `batches`, `tombstones`, `tombstones_added`, `sanitized`, the new
`incarnation`, the identity of the snapshot file it wrote (`archive_id`, `incarnation`, `sequence`, `records`) and `backup_replacement`, which is
`backup_replacement_pending` after a deletion: the protected point on the backup server still holds the old text until the next
verified bundle replaces it (BACKUP_R2.md; one protected point plus room for one candidate). Run the operator procedure of BACKUP_R2.md "Owner-requested deletion" around it (drain, delete, `--revoke-below`). Confirm it with the verifier's next
proof (`verification.archive` shows the new incarnation and a lower record count) and by the single object under the R2 prefix.
The collector never archives a tombstoned provider ID again (`discover` skips it, the receipt of a pass reports nothing about
it). `--lift-tombstone --account <id> --provider-id <id> [--yes]` allows one to be archived again; the next backup then needs a
reasoned `--rebaseline` (the continuity rule `archive_tombstone_dropped`). A provider-side deletion is still never mirrored.

#### Schema 4 and the index build (ANYM-725)

A new archive starts at schema 4. An existing schema-3 archive is upgraded additively at the writer's start (new
columns `created_at`, `duration_s`, `part_digest` on `records` and the derived tables `texts4`, `speakers`,
`index_rows`, `index_skips`); no record, ref, revision, part digest or event is changed, and the build emits no events.
The new index is then filled in bounded batches (at most 4 MiB of stored text or 500 records per write transaction, up
to 10 s per collector pass), tracked by `index_cursor` in `meta`, so an archive of any size converts across passes and a
crash resumes from the cursor. While it builds, schema 3 stays fully usable: plain queries read the old index, and
`observe()` also writes the new index for every record at or behind the cursor; date and speaker filters and browse
answer `index_building` (409, retryable) until publication. When the cursor reaches the end, one short transaction
verifies against `records` the row count and, row by row, the revision and the digest of the accepted parts of each
indexed row (a mismatch rebuilds exactly those rows and continues; it never publishes), then sets
`PRAGMA user_version=4`. A later transaction drops the superseded `texts` table. A stored row whose metadata or parts
cannot be read (malformed, or metadata over 64 KiB) is counted in `recordings_archive_status` as `index.skipped` and
does not stop the build. `recordings_archive_status` shows `index.state` (`building` or `ready`), `built`, `total`
and the index error code, if any. Older packages refuse schema 4 (`archive_schema_unsupported`); rolling back means
disabling archive collection and keeping the state files. The online backup and the backup tooling accept schemas 1–4
(ANYM-714 accepts 1–5).

### Event feed for the module event router (ANYM-726)

With an `event_router` object in the configuration (`key_sha256`: one or two SHA-256 digests, so a rotation can overlap;
`expires_at`; example in `deploy/recordings-archive.example.json`, digest `1111...`, which no token matches), the backend serves
`GET /v1/events?after=<seq>&limit=<1-200>` and `GET /v1/events/head` in the producer contract of SUBSCRIPTIONS.md "Module events".
Without that object both routes answer 404 and nothing reads the feed (ships dark). The router token must differ from every
principal key (the configuration is refused otherwise), has its own expiry, is checked on every call and opens no other route;
an agent key on these routes is `unauthorized` (401). An older package refuses a configuration that has `event_router`
(unknown field): remove the object before a package rollback.

- Identity. `seq` = `incarnation` x 10^9 + the offset inside the incarnation's epoch; `id` = `recordings:<seq>`. The incarnation
  is a time epoch (minutes since 2026-01-01 UTC), `meta.incarnation`; the epochs are `meta.feed_epochs`
  (`[[first local sequence, incarnation], ...]`, a key/value row: the schema stays 4). A marker file `journal.marker` in the archive
  directory (outside the SQLite file) holds `{incarnation, high_water}`. It is written and fsynced when the feed is served or
  maintained (before a page covering those rows leaves the process), never inside the transaction that stores recorded text, so a
  marker failure makes the feed unavailable (`archive_unavailable`) and can never stop text from being archived. At the first use,
  and whenever the marker is missing or unreadable, its high-water is above the database's, its incarnation differs from the
  database's, or an epoch reaches its capacity (below 10^9 offsets, and never past seq 2^53-1), a new epoch starts with incarnation
  `max(database, marker, last epoch, clock minutes) + 1`, before the next journal row is written (an existing backlog is split
  into capped epochs). The feed answers `feed_not_ready` (503) until the wall clock has reached the new incarnation's minute (so
  up to a minute after a start or bump, and until the clock catches up after a clock regression): two restores inside one clock
  minute can therefore never publish the same incarnation twice. Events keep the identity they were first served with. A restored
  older database or a marker that does not match the database can therefore never reuse or skip an identity, and a page serves only
  rows the fsynced marker already covers. Every marker-driven bump on a journal that already has epochs (a lost or damaged marker, a marker older than or ahead of the
  database: a new volume, a restore, a crash between a bump and its marker) leaves the clock as the only watermark, because two
  restores of one snapshot with the clock behind pick the same next incarnation; that bump therefore also gives the journal a new
  `journal_id`: the router reports a source reset and waits for its operator (`set-module-cursor`) instead of reading a possibly
  reused id as new. Ordinary growth, cap bumps and the first use keep the `journal_id`. A crash between a journal commit and the marker fsync is indistinguishable from a restore and rotates it too: one router operator step, rare, and the safe side. The feed refuses to serve with `sequence_space_exhausted` when no incarnation with
  room is left (about 2043).
- Events (references only, at most 1 KiB, no title, text, label, provider ID, token or URL): `recording.text.archived`
  (`subject` and `data.recording_ref` the opaque ref, `data.account_ref`, `data.revision`; `urgency` `batch`) once per accepted
  usable text revision, and `recording.access.changed` (`data`: `account_ref`, `principal_ref`, `reason`
  `permission_removed|account_removed|namespace_replaced`; `urgency` `now`; `subject` the account). `scope_ref` is
  `recordings:<account id>.<first 16 hex of the account's namespace>`: replacing an account's identity changes the scope.
  `account_ref` and `principal_ref` are the configured identifiers exactly when they are valid references
  (`[A-Za-z0-9][A-Za-z0-9._:-]{0,127}`), otherwise `h-` plus the first 24 hex characters of the identifier's SHA-256; that is the
  encoding every consumer (link creation and lookup, ANYM-653) must use for the same identifier.
  Text events of an account that is gone or whose namespace was replaced are not served; access-change events are, with the OLD scope.
- Access changes are detected when the configuration is re-read (each collector pass and each feed request) against the
  entitlements stored in `meta.access_view`, and written to the same journal (`ref` = `access|reason|principal|account`, `revision` 0).
  Key expiry and key replacement emit nothing. The detection window is the collector interval or the router's poll, whichever
  comes first; revoke the link in messaging first (ANYM-653).
- Isolation. Reads use the four-slot admission and the 2-second SQL deadline (`archive_busy`, `archive_unavailable`); the feed
  makes no provider, model or network call. While the feed is enabled a failed marker write rolls back that one observation (the
  marker must never be behind the journal); with no `event_router` the marker is never touched.

Local operations admit at most four simultaneous requests per process, returning
`archive_busy` instead of building an unbounded queue. SQLite keeps its 100 ms
busy wait and 2-second SQL progress deadline in worker threads. A busy/corrupt
archive returns a fixed `archive_unavailable`; existing on-demand operations and
capabilities remain independent. Status reports the archive's capacity errors.

Archive schema 2 adds title to the derived index. Starting the single writer
atomically rebuilds a schema-1 index from accepted local records under the same
capacity/progress limits; refs, records, revisions and the event journal are
preserved. A failed rebuild rolls back. Title-only changes update the index in
the same transaction without emitting a text-change event. Online backup accepts
schema 1 to 3. Older packages refuse schema 2: disable archive collection and
retain its state before a package downgrade.

Archive schema 3 (ANYM-724) is additive: the `ledger` and `meta` tables (`meta` holds key/value rows: random `archive_id`,
`incarnation` starting at 1 as an integer, `journal_id`; the backup tooling reads the first two), new columns on `accounts`
(throttle, traversal, head and text-size state) and one nullable `records.fingerprint` column.
Starting the single writer applies it in one short transaction (no index rebuild, no row rewritten); a failed upgrade rolls back
to a usable schema 2. Packages older than this one refuse schema 3 (`archive_schema_unsupported`): disable archive collection
and keep the state before a downgrade. There is no down migration or
automatic archive deletion, and no production archive activation in this slice.
