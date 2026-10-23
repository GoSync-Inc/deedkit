# Local attachment text index

This module extracts searchable text from already downloaded immutable originals.
It does not contact Telegram, WhatsApp, URLs in documents, external OCR or AI
services. Filenames and extracted text are untrusted evidence, never instructions.

## Supported content and limits

| Input | Extraction | Limits/coverage |
| --- | --- | --- |
| UTF-8 text, UTF-16 with BOM, Markdown, CSV, JSON, XML, HTML | Standard library decoding | Raw text; HTML/XML markup is retained; invalid bytes are replaced |
| DOCX | Main document, headers, footers, footnotes, endnotes | Text nodes only; external relationships, macros and embedded files are ignored |
| XLSX | Shared/inline strings and stored cell values | Each worksheet is labeled; formulas are not evaluated, their cached values may be stale; dates remain stored numeric values |
| PDF | Local Poppler plus Tesseract for pages without a text layer | First 100 text pages; at most 5 OCR attempts, 1600-pixel render edge, shared 23-second OCR budget; explicit page spans and partial coverage |
| PNG/JPEG images | Local Tesseract, English and Russian | Up to 20 million pixels; OCR can misread content |
| Other formats | Filename and parent-caption search | `unsupported_format`; original remains available through normal attachment access |

An original may be at most 32 MiB for indexing, independently of the larger
download/archive limit. Index text defaults to a maximum of 1,000,000 characters (operator-configurable), with an
explicit `truncated` flag. The complete original remains immutable. There is no
claim that the entire content of a large document was indexed.

OOXML archives are read without filesystem extraction. The parser rejects
encrypted entries, duplicate names, parent paths, DTD/entity definitions,
non-UTF-8 XML encodings, more than 512 entries, more than 32 MiB uncompressed in
total, any entry over 8 MiB, and compression ratios above 200. These deliberate
limits can reject legitimate large/very compressible Office documents.

Parser subprocesses get a fresh environment without DB credentials or provider
sessions. They use constant program names and generated filenames, not shell
commands. Before parsing, the child opens the CAS original with `O_NOFOLLOW`,
checks regular-file size and SHA-256. Child CPU is limited to 32 seconds,
wall time to 40 seconds, files to 32 MiB each, descriptors to 64, and virtual
memory to 512 MiB on Linux. Native parser execution is additionally limited to
30 seconds. The worker kills the subprocess group on exit/timeouts and deletes
temporary files. Returned text is bounded independently of native output.

These are resource limits, **not an OS/network sandbox**. Production must use a
dedicated unprivileged worker container, read-only application/original volumes,
bounded temporary storage/cgroup memory/PIDs, no provider sessions, and only the
database backend network. Do not mount the Docker socket or host home directory.
Native parser packages must receive normal security updates.

## Retrieval contract

`search_files` uses the same account/chat ACL and `read` permission as message
search. It supports `query_mode=all|phrase|web|exact`,
`search_language=auto|simple|russian|english|indonesian`, scalar/list account,
chat and sender filters, `[from_time,to_time)` dates and direction. Lists are OR
within one dimension and AND across dimensions; empty lists match nothing.
Each item names its chat in `chat_title` (at most 64 characters) and the source message time in
`sent_at`, also in compact detail. `filename` is a literal case-insensitive substring, including literal `%`/`_`.
PostgreSQL stemming uses the same configuration for index and query. Auto tries
each complete query under all four configurations. There is no semantic search
or implicit spelling correction in file search.

`order=asc|desc` retains attachment-ID cursors; `order=relevance` returns an opaque
query/principal/scope-bound cursor and recomputes its authorized anchor and rank.
Query-centered `excerpt` is capped at 800 characters. Both raw extraction and
source-current model-derived titles/summaries/transcripts can match. A completed
independent description remains searchable during a refresh or profile change;
its published model provenance and refresh state remain explicit. Stale sources
never contribute text or rank.

Original `filename`/`status` fields remain. `source_filename` preserves provenance;
`display_name` decodes only explicit valid MIME encoded words in known charsets
and removes presentation controls/path separators. It never guesses mojibake or
percent encoding. Recognized native WhatsApp fallback names (`true_`/`false_`
plus the exact current provider reference and `.bin`) on WhatsApp photo, video,
audio, voice or sticker occurrences receive a generated presentation and
`download_filename` extension for known media MIME types. The account platform,
current provider ID and observed media kind must all agree. `display_name_basis`
identifies this case; the original `filename` and `source_filename` never change.
Document names, uncertain legacy occurrences, other `.bin` names and unknown MIME
types retain their source names.
Model `title` and `summary` remain separate. Responses report
`download_status`, `extraction_status`, and `enrichment_status` independently;
`understanding` separately reports a generated title, summary, coverage, published
provider/model, `profile_current` and `refresh_state`. It never replaces detailed
extracted text or the ASR transcript. Model derivatives carry an explicit
untrusted/generated label. Optional enrichment
is a separate disabled-by-default worker; this extraction worker invokes no model.

`get_file_text(attachment_id, offset=0, limit=4000, content=None)` returns a
character page, `next_offset`, source provenance and overlapping PDF page / XLSX sheet and cell spans.
An omitted `content` reads a voice note (`audio/*` or `application/ogg` without extracted
text) through its model transcript and reports `content='enriched'`; any other file
reads `content='extracted'`. When that transcript is not ready, the reply has empty text,
`enrichment_status` and an `empty_reason` from the timestamp-segment vocabulary
(`processing_pending`, `processing_paused`, `processing_worker_unavailable`,
`processing_failed` with `failure_reason`, or `processing_not_configured`, which only
the owner can change). Explicit `content` values keep their meaning.
Message rows mark a voice note whose transcript is ready with `transcript_ready: true`.
In `get_messages`, `search` and `get_context` (every detail, and the transcript's file list)
an attachment whose default `get_file_text` text is ready carries `text_excerpt` (ANYM-490):
at most 300 characters of that text (photo OCR, PDF and other extracted text, or a voice
note's transcript), `source` (`extracted` or `transcript`), `excerpt_chars`, `total_chars`,
`complete`, `model_generated`, `untrusted: true` and `read_more`, the `get_file_text`
call that continues at `offset` (null when the excerpt is the whole text). Excerpts in
one decorated result share a 3,000-character budget; an attachment past it gets
`omitted: "response_budget"` with `read_more` at offset 0. Image model descriptions are
not excerpted, and an excerpt follows `get_file_text`'s presence rule: no excerpt when the
original is missing without a completed managed eviction.
`content='enriched'` pages the model transcript/text up to 1,000,000 stored characters;
raw extraction has the same configurable ceiling. Each response page is at most 8,000 characters.
`get_attachment` and `request_file_download` expose the same current extraction
coverage (character count, parser limit, truncation and bounded format metadata).
They retain that evidence when the registered original is missing, without
issuing a link or treating a stale derivative as current. Span arrays remain in
the paged text interface.
Enrichment coverage states whether timestamps exist; timestamp segments use seconds,
not character offsets, and are not repeated in every text page. Queued, failed,
unsupported or unconfigured content returns empty text and its actual status.
Deleted/inactive/stale/inaccessible attachments are `not_found`. A completed,
journaled managed eviction keeps authorized text and timestamp segments readable,
with `original_available=false`, `original_evicted` and no original download URL.
`request_file_download` can request the same bytes again, subject to provider and
storage limits; recovery is best effort. Unexpected missing or wrong-size
originals remain `attachment_unavailable`. Presence checks do not rehash on
every read; storage integrity checks must detect post-index bit rot.

`get_file_occurrences(attachment_id, after='', limit=20)` resolves a readable source
occurrence to other current readable occurrences of identical bytes. There is no
raw-digest lookup, hidden occurrence count, or leakage of hidden filenames/chats.

All query paths join the current parent message before filtering/pagination and
compare the current digest, revision, filename, MIME and byte count. A revoked
key or narrowed chat scope therefore cannot retrieve previously indexed text.
These functions must only be called inside `Store.authorized`; they do not
authenticate standalone callers. Search/read operations do not acknowledge feed
events or mark source-provider chats as read.

Search examines at most `limit+1` DB candidates. Managed-evicted candidates remain
searchable; unexpectedly missing originals are omitted. `next_cursor` can
therefore accompany a short/empty page.
Clients should continue while a cursor exists. Cursor IDs are not portable
snapshots and must still be visible when used.

## Worker and integration

The worker API is:

```python
worker = FileIndexer(dsn, files, lease_seconds=90, retry_seconds=300, max_attempts=3)
result = worker.run_once()
# {'claimed': bool, 'attachment': str | None,
#  'status': 'idle'|'ready'|'unsupported'|'failed'|'retry'|'superseded',
#  'error_code': str | None}
```

Run `python -m messaging_service.file_index` with `DATABASE_URL_FILE` and
`PROBE_FILES` (default `/data/files`). Explicit `--dsn-file`, `--files`, `--once`
and `--interval` are available for operators/tests. The loop logs only state and
fixed error codes. SIGTERM stops after the current bounded extraction. Configure
a shutdown grace period of at least 50 seconds.

Immutable migration 009 adds one derived table, `attachment_text`, two queue/invalidation
trigger functions, and two indexes. Initial migration queues existing files;
ingestion thereafter queues/invalidate in the same DB transaction. Trigger
functions have a fixed `pg_catalog` search path, fully qualified `public` table
names, no dynamic SQL, and no PUBLIC execute grant. They must be owned by the
trusted migration owner, never the gateway/collector/indexer role.

Claim uses `FOR UPDATE SKIP LOCKED`, allowing multiple workers. Extraction occurs
outside DB transactions. Completion requires the current unexpired lease token,
source digest/revision and active parent. It acquires the same account advisory
lock before row locks as archive ingestion. With lifecycle tracking, workers first
take the shared lifecycle lock, then admission/account locks and source rows.
A crashed worker can be reclaimed
after 90 seconds; an old worker cannot publish after reclaim, edit or deletion.
Transient missing originals/time/resource failures retry up to three attempts;
unsupported formats and invalid documents terminate explicitly. The operator can
requeue a terminal job after correcting a parser/storage problem; no remote
administration tool is exposed.

With schema 33/34, a matching prepared/completed managed eviction pauses an
original-dependent claim without changing its attempts, text or budget. Verified
re-publication makes queued processing eligible again. Unexpected missing bytes
still use the failure/retry path. Install the current indexer role's read-only
residency/journal grants before starting this worker.

Since schema 37 the indexer has no direct `events` or `feed_counter` writes. It publishes
text readiness only through `publish_file_text_event`, a fenced function that accepts the
text kinds for an active attachment of the current message revision and a snapshot equal to
the stored message. Apply migration 037 first, then deploy the new code, then reapply
`indexer_role.sql`; the template revokes the earlier direct grants. Until then the new code
publishes with the earlier grants directly. The template runs in one transaction that refuses
below schema 37 before any revoke, and closes the publishers to `PUBLIC` again (a restore
without ACLs reopens them; `Store.migrate()` repeats that revoke).

Migration 013 adds language vectors, source-display and extraction metadata,
processor profile/version, and occurrence indexes without another table. It does
not re-extract historical ready documents automatically. A raw extraction may be
reused across occurrences only for the same SHA-256, byte count, parser dispatch
profile and processor version. The worker rehashes the current bounded original
before reuse. It copies context-free parser text/coverage only, never a source
filename, chat identity, model title, summary or transcript. Serving always checks
the destination occurrence ACL. Migration 015 holds optional per-occurrence model
derivatives independently and invalidates them with the source.

Required integration outside this module:

- Gateway: `SELECT attachment_text, attachment_enrichment`; map file search/text/occurrence tools to existing
  `read` permission, audit operation constraints, REST/MCP schemas and metadata.
- Indexer: `SELECT messages, attachments`; `SELECT, UPDATE attachment_text`.
  It does not need API keys, principals, provider sessions, or raw feed write rights.
- Migration/bootstrap: execute 008 before immutable migration 009, register its
  checksum and schema version normally. Do not silently modify applied migrations.
- Backup: schema 9 manifest includes `attachment_text` keyed by `attachment`.
  `pg_dump` also preserves both trigger functions/triggers. Restore into quarantine
  with the original worker disabled; preserve queue state and reclaim leases only
  when an intended indexer resumes. The source bundle includes both Python modules.
- Container: install Poppler and Tesseract with `eng` and `rus` data in the indexer
  image. Native integration tests run in that image; missing binaries return an
  explicit `extractor_unavailable`, not pretend-successful empty indexing.

`tests/test_file_index.py` and `tests/test_file_search_v2.py` cover source hashes, path/symlink safety, Unicode,
truncation, archive/entity bombs, real PDF and OCR, scope/pagination, text pages,
edits/deletion, lease fencing/retry, unsupported formats and missing originals.
PostgreSQL tests require the existing isolated `messaging_probe_test` fixture.

## Docling evaluation boundary

Docling is a reasonable candidate for a separate structured-document adapter,
especially when table/layout fidelity matters. An isolated synthetic trial is
recorded per installation; it is not a production dependency. Its official advanced options describe local execution, model
asset prefetching and explicit remote-service enablement:
https://docling-project.github.io/docling/usage/advanced_options/ .
Its own low-resource discussion identifies OCR and table structure as expensive
pipeline stages: https://github.com/docling-project/docling/discussions/245 .
These sources do not establish latency or memory on this service's VPS.

Before replacing the bounded Poppler/Tesseract path, run an isolated offline
benchmark with pinned dependencies and model hashes, network disabled and the
same synthetic/approved public fixtures: digital PDF, mixed text/scan PDF,
Russian/English scans, tables, invalid documents and oversized inputs. Compare
cold/warm wall time, peak RSS, p95 per page, token recall, table cell accuracy,
timeout behavior and exact coverage metadata under fixed CPU/RAM/page limits.
Keep it opt-in unless measured quality justifies its additional model footprint.
No private documents or production workloads were sent to an external service.

## Schema 16: retained text and passages

### Version 0.9.5: current message captions

File search also checks the current parent message body with the same query mode
and language configurations. This uses existing message indexes; no provider
request, model invocation or duplicate caption index is needed.
`matched_content` identifies the excerpt source (`extracted`, `enriched`, or
`message_context`); `match_sources` lists the matching sources. `extracted`
includes indexed filenames as well as parser output. File passages are preferred
for the excerpt when both a file and its caption match. A voice note without
extracted text shows its ready transcript as the excerpt when the query is empty
or matches only the name; such hits carry `excerpt_source: "transcript"` and keep
their `matched_content`.

A caption match includes `message_context={message_id,revision,field:"body"}`
in both compact and full replies. It does not mean that OCR, transcription or
description has completed. Queued/unsupported extraction keeps its own status.
Only downloaded or journaled managed-evicted originals with a current active
authorized occurrence are returned; identical bytes do not make another chat's
caption searchable here.
Message changes/deletion and scope revocation invalidate the match immediately.

Ranked cursors from earlier file-search semantics are invalidated; restart the
query after `invalid_cursor`. File byte/text retrieval continues through the
existing attachment ID tools. Unexpectedly missing originals remain outside file
results; message search can still identify their parent and pending attachment.
Schema 034 preserves completed extraction, ASR segments and descriptions across
caption/formatting edits when the active bytes are unchanged. Real media
replacement, deletion and access revocation invalidate the corresponding result.

### Passage boundaries

Raw and model text are indexed in 32,000-character passages with 1,000-character
overlap. `search_scope=passage` means all query terms must match a single passage;
terms far apart in a document need separate queries. Literal `exact` mode examines
the complete retained text. Filename/title/type headers participate in each
passage. This bounds PostgreSQL vectors even for near-million-character documents.

`get_file_segments(attachment_id, offset=0, limit=100)` pages model timestamp
segments, at most 100 segments and 64,000 text characters per response. Follow
`next_offset`; unavailable timestamps are explicit. Text pagination returns only
intersecting page/sheet/cell spans, capped at 200 with an explicit truncation flag.
DOCX has no invented page numbers; spreadsheets use actual workbook sheet names
and stored cell values. A model document type is generated metadata, not a verified
classification. `complete=false` identifies bounded or partial extraction.

`FILE_TEXT_CHAR_LIMIT` / `--text-char-limit` accepts 1,000..1,000,000, default
1,000,000. The processor version is bounded-v3; cached extraction reuse requires
the same configured cap. Migration 016 backfills two derived chunk tables and
adds their scoped gateway read grants and backup manifest entries. It does not
reparse old originals automatically: an operator may selectively requeue old
truncated rows. Parser size/page/time limits still apply.
