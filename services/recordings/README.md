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
The source package is run explicitly; the Compose launcher has no recordings
manifest/image in this slice. No production configuration is changed by install.
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
`start_date`/`end_date` as timezone-aware ISO instants, and `cursor`. Date bounds
are inclusive and compare **created_at**, not recording start. Inspect
`pagination`: unknown or malformed evidence stays null, truncated pages remain
incomplete even when empty. `complete` concerns this provider traversal's end,
not account/archive coverage. Provider order can change and lists can be stale
for ten seconds. No hidden auto-pagination occurs. Follow `next_cursor` with the
same filters and limit. Missing provider continuation means no safe next page;
it never means that an unknown traversal is complete.

`recordings_read` takes `account`, `recording_id`, `kind` (`transcript` or
`summary`), `page_chars` (100–10000, default 4000), and optional `cursor`.
Transcript text is newline-delimited JSON segments preserving supplied offsets
and speaker labels, split at character boundaries. A page may split one JSON
segment; concatenate pages from the same digest before parsing it. The overview
is provider-authored summary text. No extra transcription/model is invoked.
`content_state` distinguishes `available`, `empty`, `pending`, `unavailable`.
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

This is a fragment, merged with the configuration above. `archive_namespace`
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
No content or journal retention deletion is automated.

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

- `recordings_search({account, query, limit: 10})`: searches the local FTS5
  index of titles, transcripts and summaries. A query is 1–256 characters,
  containing 1–16 Unicode literal words of at most 64 characters each, joined
  with AND. Operators, column selectors and wildcards are not an FTS language
  exposed to the caller. Returns at most 50 hits with a snippet of at most
  320 characters, stable `recording_ref`, revision and per-part digest,
  freshness and attempt state. Ranking uses SQLite bm25. No total count or
  cross-account search is returned. This is lexical search over observed local
  text; an empty result is not proof of vendor history completeness.
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
schema 1 or 2. Older packages refuse schema 2: disable archive collection and
retain its state before a package downgrade. There is no down migration or
automatic archive deletion, and no production archive activation in this slice.
