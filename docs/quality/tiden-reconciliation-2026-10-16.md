# Tiden reconciliation — 2026-10-16

This is a dated audit receipt, not a work queue. Current priorities and delivery
state belong to the [DeedKit Tiden product](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1).
Agents follow the [product workflow](../operations/tiden-product-workflow.md).

## Audit scope

The source baseline is [main 27ec8bf](https://github.com/GoSync-Inc/deedkit/commit/27ec8bfc4d0a328dd4b639bab138cc21f6853ce7),
with successful [exact-main CI](https://github.com/GoSync-Inc/deedkit/actions/runs/38780132404).
The previous Tiden sync watermark was b86e9884c60750cbd99d6fbc9a95796f1f203599:
112 Git commits and 304 changed files behind. The first-parent range contains
20 delivered changes, PRs #18–39 excluding #26 and unmerged #34.

The review covers the 212 existing spec bodies, their 590 repository-file
anchors, current module contracts, changed implementations and the old backlog.
258 anchors already resolve at their recorded path; 332 resolve under the
messaging module prefix, across 143 specs. None is an unidentified source path.
Historical anchors are provenance for their recorded revision, not evidence that
a historical file path still exists at the current repository root.

This depth is a source/contract audit. It is not a rerun of every linked test and
not live provider, deployment, package publication or off-site recovery acceptance.
Assertion review is narrower and explicit below. A passing gate is reported only
for the branch/main and time actually read, not inferred from a green Git check.

## Material contract findings

- Schemas 2–32, upload quarantine and subscription delivery quarantine replace
  obsolete schema-27 limits; old SQL checksums remain immutable.
- Upload, subscription ACK and feedback permissions and client operations are
  represented. Ordinary 16 KiB wire limits retain the bounded upload-chunk exception.
- Native WhatsApp has its own implementation contract. Browser pairing, Chromium,
  browser media and browser operation details are scoped to the fallback provider.
  Native capability gaps, partial history and receipt uncertainty stay explicit.
- Conversation full, compact and transcript projections are distinct. Compact
  does not promise duplicate bodies, transcripts or every full provenance field.
- Conflict guidance names the actual get_context operation. Live capture liveness
  is independent of history backlog; self-chat receipts are not applicable.
- Compose originals remain read-only; the API uses a separate writable upload
  staging volume. Restricted database writes are not confused with read-only DSN files.
- Current CI timeout, tag-only npm publication and process-only authenticated calls
  health replace outdated statements. verify_only verifies publisher identity.
- Calendar source selection is an explicit owner decision; one installation's
  private binding choices are not a general product requirement.

## Existing branch review

| Existing branch | Source already delivered | Disposition |
| --- | --- | --- |
| intent/2026-10-15-ci-reports-an-unmapped-feature | #36 | Merged reviewed spec 154; authentic feature-branch routing retained. |
| intent/2026-10-15-the-production-service-runs | #27/#32 | Merged spec 215 and reviewed promotion assertion links. |
| intent/2026-10-15-agents-can-inspect-call | #28 | Merged specs 209/216 and reviewed links; live audio acceptance remains open. |
| intent/2026-10-15-agents-receive-consistent | #29/#31/#33 | Merged 13 reviewed modifications and specs 217–219 with subsequent #37/#38 boundaries. |
| codex/tiden-runtime-telemetry | Existing spec 173 | No spec delta; suite additions alone do not establish new behavior. Preserve history; disposition is DK-31. |
| intent/2026-10-14-agents-maintain-durable | Existing 182–186 | Old additions 54/68 duplicate maintained subscriptions; do not merge duplicate contracts. Disposition is DK-31. |
| intent/2026-10-15-add-an-isolated-minimal-prompt | Open #34 | Keep existing spec 220 as DK-40; no duplicate or merge. |
| intent/2026-10-16-deedkit-releases-admit-only | Draft #40 | Keep existing spec 221 as DK-41; no duplicate or merge. |

## Test-link review

147 proposals were checked against the actual synthetic test assertions and target
contract bodies. 135 establish a bounded part of their target; 12 do not establish
the signed-hint contract to which they were proposed. Those 12 cover event selection,
reply evidence or legacy permissions, without exercising hint staging/delivery.
Their valid links to consumption/wake requirements remain independent.

Several calendar tests have historical reporting identities with different module
prefixes. Their identities are preserved; this audit does not rename/reimport tests
to make counts look smaller. A linked passing test establishes its assertions,
not all clauses of a broad feature or a real deployment.

## Existing-spec inventory

“Retained” means no text correction was identified by this source review. It is
not a new execution result. “Current anchors” means adding current module paths
alongside original provenance. Separate branch changes are described above.

| Spec | Title | Review disposition |
| --- | --- | --- |
| 1 | Connect messenger accounts | Retained |
| 2 | Collect messages and history | Retained |
| 3 | Search and read the archive | Retained |
| 4 | Store and index files and media | Retained |
| 5 | Enrich content with AI processing | Retained |
| 6 | Control agent access and record reviews | Retained |
| 7 | Integrate agents through MCP, HTTP and client libraries | Retained |
| 8 | Back up and recover the archive | Retained |
| 9 | Deploy and operate the installation | Retained |
| 10 | Scoped agent credentials | Reviewed current source contract correction; current anchors |
| 11 | Issue an account-scoped agent key | Retained; current anchors |
| 12 | Narrow a principal's accounts, chats and permissions | Reviewed current source contract correction; current anchors |
| 13 | Revoke an agent key | Retained; current anchors |
| 14 | Authorize every request against current principal and key permissions | Reviewed current source contract correction; current anchors |
| 15 | Content-free append-only audit log | Retained; current anchors |
| 16 | Least-privilege gateway database role | Schemas 29–32 gateway grants; current anchors |
| 17 | Shared review marks | Retained |
| 18 | Record a review of a message revision | Retained; current anchors |
| 19 | Read reviews with visibility and staleness | Retained; current anchors |
| 20 | Immutable review storage | Retained; current anchors |
| 21 | MCP and REST agent gateway | Retained; current anchors |
| 22 | MCP tool catalog | Retained; current anchors |
| 23 | REST operations endpoint | Retained; current anchors |
| 24 | Discover the caller's capabilities | Retained; current anchors |
| 25 | Hardened HTTP boundary | Reviewed current source contract correction; current anchors |
| 26 | Safe, actionable error codes | Retained; current anchors |
| 27 | Compact and full response views | PR #35 compact/transcript projections; current anchors |
| 28 | Python client and command line | Retained |
| 29 | Python HTTP client | Upload/download and schema-32 client operations; current anchors |
| 30 | anymsg command line entry point | Retained; current anchors |
| 31 | Message search | Reviewed merged-intent correction |
| 32 | Full-text search with query modes and languages | Retained; current anchors |
| 33 | Fuzzy fallback for misspelled terms | Retained; current anchors |
| 34 | Filter messages by account, chat, sender, time and direction | Retained; current anchors |
| 35 | Scope-bound pagination cursors | Retained; current anchors |
| 36 | Read conversations | Retained |
| 37 | Page through a chat's messages | PR #35 compact/transcript projections; current anchors |
| 38 | Open a message with its surrounding context | PR #35 compact/transcript projections; current anchors |
| 39 | Resolve a provider chat/message reference | Retained; current anchors |
| 40 | Chat list and reply queue | Retained |
| 41 | List visible chats | Retained; current anchors |
| 42 | Chats awaiting the owner's reply | Reviewed merged-intent correction; current anchors |
| 43 | People directory | Retained |
| 44 | Search people by name, username or sender ID | Reviewed merged-intent correction; current anchors |
| 45 | Cross-script name matching | Retained; current anchors |
| 46 | WhatsApp phone-number / LID identity links | Retained; current anchors |
| 47 | Change feed for agents | Retained |
| 48 | Pull message changes for a named consumer | Reviewed merged-intent correction; current anchors |
| 49 | Acknowledge processed updates | Retained; current anchors |
| 50 | Sync status and coverage evidence | Retained |
| 51 | Get sync status per account and chat | PR #38 independent liveness/history evidence; current anchors |
| 52 | Collector operations observations | Reviewed current source contract correction; current anchors |
| 53 | Coverage evidence on search and chat results | Reviewed current source contract correction; current anchors |
| 55 | Attachment download | Retained |
| 56 | Request a download grant for an attachment | Retained; current anchors |
| 57 | Download bytes through a grant | Retained; current anchors |
| 58 | File content index | Retained |
| 59 | Search files by name and content | Retained; current anchors |
| 60 | Read extracted file text | Retained; current anchors |
| 61 | Read timestamped audio segments | Retained; current anchors |
| 62 | List where the same file was shared | Retained; current anchors |
| 63 | Media storage | Retained |
| 64 | Content-addressed immutable blob store | Retained; current anchors |
| 65 | Media storage admission and pause | Retained; current anchors |
| 66 | WhatsApp media download and decryption | Explicit browser-provider scope |
| 67 | Background file text extraction worker | Reviewed merged-intent correction; current anchors |
| 69 | Optional attachment enrichment worker | Retained |
| 70 | Configure enrichment profiles per media kind | Retained; current anchors |
| 71 | Budgeted, fair job claiming | Retained; current anchors |
| 72 | Isolated, resource-limited inference child | Retained; current anchors |
| 73 | Model output treated as untrusted derived text | Retained; current anchors |
| 74 | Fenced result publication and failure pause | Retained; current anchors |
| 75 | Publish processing profiles for status | Retained; current anchors |
| 76 | Single local-heavy processing slot | Retained; current anchors |
| 77 | Codex CLI subscription connector | Retained; current anchors |
| 78 | Enrichment deployment profiles and DB role | Retained; current anchors |
| 79 | Account registration and collection policy | Retained |
| 80 | Register an account with include/exclude/discover policy | Retained; current anchors |
| 81 | Resume a blocked collector connection | Retained; current anchors |
| 82 | Telegram account connection | Retained |
| 83 | Private Telegram collector configuration and handover permit | Retained; current anchors |
| 84 | Pair Telegram from the owner's terminal | Retained; current anchors |
| 85 | Verify Telegram identity on every connect | Retained; current anchors |
| 86 | WhatsApp account connection | Reviewed current source contract correction |
| 87 | Private WhatsApp collector configuration and permit | Explicit browser-provider scope; current anchors |
| 88 | Pair WhatsApp by QR with cold-reopen verification | Explicit browser-provider scope; current anchors |
| 89 | WhatsApp identity pin without session takeover | Explicit browser-provider scope |
| 90 | Export bundled WhatsApp Node sources | Explicit browser-provider scope; current anchors |
| 91 | Durable, idempotent message ingestion | Retained |
| 92 | Validate every captured message against a strict envelope schema | Retained; current anchors |
| 93 | Keep an immutable revision history for created, edited and deleted messages | Retained; current anchors |
| 94 | Record contradictory provider observations as version conflicts | Reviewed merged-intent correction; current anchors |
| 95 | Apply provider deletions as durable tombstones, even before the message is imported | Retained; current anchors |
| 96 | Single collector per account enforced by an epoch-fenced database lease | Retained; current anchors |
| 97 | Crash-safe bounded capture spool drained only after database commit | Retained; current anchors |
| 98 | Replay a JSONL envelope file into the archive from the operator CLI | Retained; current anchors |
| 99 | Normalise Telegram and WhatsApp messages into envelopes without network lookups | Retained; current anchors |
| 100 | Collectors run under a least-privilege database role | Retained; current anchors |
| 101 | Telegram history and live collection | Retained |
| 102 | Catalog Telegram dialogs before fetching history, resuming after rate limits | Retained; current anchors |
| 103 | Backfill and reconcile each Telegram chat with durable cursors | Retained; current anchors |
| 104 | Limit Telegram history import to a configured history_since window | Retained; current anchors |
| 105 | Capture live Telegram new, edited and deleted messages into the spool | Retained; current anchors |
| 106 | Pace Telegram requests and persist FloodWait cooldowns | Retained; current anchors |
| 107 | WhatsApp history and live collection | Reviewed current source contract correction; current anchors |
| 108 | Bounded independent private command lanes for the WhatsApp provider | Explicit browser-provider scope; current anchors |
| 109 | Capture live WhatsApp messages, edits and delete-for-everyone | Explicit browser-provider scope; current anchors |
| 110 | Backfill and catch up WhatsApp chat history with gap detection | Native history boundary; current anchors |
| 111 | Read WhatsApp history pages within time, size and concurrency bounds | Explicit browser-provider scope |
| 112 | Record cache-only WhatsApp phone-number and LID identity observations | Explicit browser-provider scope; current anchors |
| 113 | Collector lifecycle and operational health | Retained |
| 114 | Exclusive ownership of a provider session file for the collector's lifetime | Retained; current anchors |
| 115 | Record connection, live-capture and history phases with reason codes | Reviewed merged-intent correction; current anchors |
| 116 | Back off transient connection failures; block on failures that need the owner | Retained; current anchors |
| 117 | Preview a recent-first history import plan for a chosen depth | Retained; current anchors |
| 118 | Consistent archive snapshots and isolated restore verification | Retained; current anchors |
| 119 | Create a consistent snapshot with a per-table manifest | Reviewed current source contract correction; current anchors |
| 120 | Verify a bundle and a restored database against the manifest | Reviewed current source contract correction; current anchors |
| 121 | Quarantine a restored database so it cannot collect, send or accept old keys | Restore quarantine for uploads and event delivery; current anchors |
| 122 | Include cold provider sessions, settings and spools in a snapshot | Retained; current anchors |
| 123 | Scheduled encrypted off-site backup delivery | Retained; current anchors |
| 124 | Encrypt a verified bundle to an age recipient | Retained; current anchors |
| 125 | Capture and encrypt in an isolated backup worker container | Retained; current anchors |
| 126 | Run a resumable backup job that pauses collectors only during capture | Retained; current anchors |
| 127 | Accept ciphertext into an append-only SSH forced-command dropbox | Retained; current anchors |
| 128 | Prune off-site backups only behind a verified restore anchor | Retained; current anchors |
| 129 | Install, migrate and serve the archive | Retained; current anchors |
| 130 | Apply checksummed, serialized schema migrations | Current schema 32; current anchors |
| 131 | Initialise with synthetic demo accounts for a no-data trial | Retained; current anchors |
| 132 | Serve the gateway with conservative network defaults | Reviewed current source contract correction; current anchors |
| 133 | Run administration only from a local operator container | Retained; current anchors |
| 134 | Hardened container deployment | Retained |
| 135 | Least-privilege, resource-bounded services on segregated networks | Reviewed current source contract correction |
| 136 | WhatsApp collectors run Chromium under a seccomp profile with a verified sandbox | Explicit browser-provider scope |
| 137 | Pinned, purpose-specific container images | PR #30/#32/#37 native deployment paths |
| 138 | Content-free diagnostics from libraries and the reverse proxy | Retained; current anchors |
| 139 | Verified build, test and release pipeline | Retained |
| 140 | Release artifacts carry one version and no secrets | Retained |
| 141 | Continuous verification on real PostgreSQL, Node and installed packages | Reviewed current source contract correction |
| 142 | Publish to PyPI and npm from a version tag with trusted publishing | Reviewed current source contract correction |
| 143 | Run an end-to-end synthetic network smoke drill | Retained |
| 144 | JavaScript (npm) HTTP client and CLI | Retained |
| 145 | Reject unsafe server base URLs before any credential is sent | Retained |
| 146 | Validate client configuration and keep the token private | Retained |
| 147 | Validate operations and arguments locally before calling the server | Current JavaScript methods and wire bounds |
| 148 | Authenticated POST with redirect refusal, timeout and cancellation | Retained |
| 149 | Bounded, strict JSON response parsing | Retained |
| 150 | Map server errors to a redacted AnyMsgError | Retained |
| 151 | Typed convenience methods for every operation | Retained |
| 152 | anymsg npm CLI with private token-file authentication | Retained |
| 153 | Dependency-free ESM package published to npm | Retained |
| 154 | Publish CI evidence to an explicitly verified Tiden branch | Reviewed merged-intent correction |
| 155 | Publish original attachment readiness with occurrence provenance | Reviewed merged-intent correction; current anchors |
| 156 | Expose observed sender identity provenance | Retained; current anchors |
| 157 | Read cached native formatting evidence without provider lookups | Retained; current anchors |
| 158 | Scoped, opt-in outgoing messages | Retained; current anchors |
| 159 | Enqueue an exact scoped send intent idempotently | Retained; current anchors |
| 160 | Dispatch once and report uncertainty separately from delivery | Reviewed merged-intent correction; current anchors |
| 161 | Preview bounded provider-specific formatting and preserve reviewed wire data | Retained; current anchors |
| 162 | Explicit provider-specific messenger operation catalog | Reviewed current source contract correction; current anchors |
| 163 | Deploy the subscription worker separately with bounded privileges | Retained |
| 164 | Durably capture provider delivery and read observations | Retained; current anchors |
| 165 | Scoped durable messenger operations | Retained; current anchors |
| 166 | Discover and preview scoped intent before enqueueing an operation | Retained; current anchors |
| 167 | Recheck command access and preserve ambiguous outcomes without retry | Retained; current anchors |
| 168 | Bounded Telegram messenger operations | Retained; current anchors |
| 169 | Read bounded live Telegram observations | Retained; current anchors |
| 170 | Send registered originals and structured Telegram messages | Retained; current anchors |
| 171 | Manage Telegram messages, chats, groups and account data | Retained; current anchors |
| 172 | Observe private-peer Telegram read boundaries without fetching data | Retained; current anchors |
| 173 | Opt in to bounded content-free runtime diagnostics | Retained; current anchors |
| 174 | Set an individual credential's permission ceiling | Retained; current anchors |
| 175 | Opt-in idempotent text sending | Retained; current anchors |
| 176 | Preview and persist formatted message intent | Retained; current anchors |
| 177 | Explicitly permitted reviewed messenger operations | Retained; current anchors |
| 178 | Independent provider delivery and read evidence | PR #38 self-chat receipt applicability; current anchors |
| 179 | Attribute archived outgoing messages to confirmed agent sends | Retained; current anchors |
| 180 | Read formatting evidence for the exact message revision | Compact projection does not promise full formatting payload; current anchors |
| 181 | Avoid repeating file processing for content-identical revision changes | Retained; current anchors |
| 182 | Durable key-owned passive subscriptions | Reviewed merged-intent correction; current anchors |
| 183 | Pause, resume or close a subscription with version checks | Retained; current anchors |
| 184 | Bounded restartable exact-event matching | Retained; current anchors |
| 185 | Least-privilege subscription matcher database role | Retained; current anchors |
| 186 | Independent file readiness and processing failure events | Retained; current anchors |
| 187 | Repair confirmed Telegram short-send sender identity | Retained; current anchors |
| 188 | Opt into bounded fresh Telegram media priority | Retained; current anchors |
| 189 | Capture WhatsApp native-wire formatting evidence without inferred entities | Explicit browser-provider scope |
| 190 | Send bounded WhatsApp text using the existing authenticated client | Explicit browser-provider scope |
| 191 | Durably capture passive WhatsApp delivery, read and playback observations | Explicit browser-provider scope |
| 192 | Execute explicit policy-gated WhatsApp operations with honest outcome evidence | Explicit browser-provider scope |
| 193 | Owner read observations and explainable reply queue filters | Retained |
| 194 | Credential-local unanswered hint dismissal | Retained; current anchors |
| 195 | Message kinds, media labels and service-event filtering | Retained; current anchors |
| 196 | Qualified conversation ordering and bound traversal | Retained; current anchors |
| 197 | Actual authorized chat head and qualified turn hints | Retained; current anchors |
| 198 | MCP conversation defaults and legacy REST compatibility | PR #35 compact/transcript projections; current anchors |
| 199 | Read versioned conversation views with qualified ordering and media labels | PR #35 compact/transcript projections; current anchors |
| 200 | Search with an explicit conversation view | PR #35 compact/transcript projections; current anchors |
| 201 | Read uncertainty-qualified actual conversation head | Retained; current anchors |
| 202 | Export a complete stable Tiden migration baseline without registry writes | Retained |
| 203 | Reject unintended changes to existing migration identities, links and anchors | Retained |
| 204 | DeedKit staged module architecture and verification | Retained |
| 205 | Selected MCP methods and nested messenger actions | Reviewed merged-intent correction |
| 206 | Original file delivery with bounded revocable tickets | Retained |
| 207 | Preserve messaging feed and subscription identities through gateway cutover | Retained |
| 208 | Family calendar visibility and incomplete availability across one MCP | Reviewed merged-intent correction |
| 209 | Voice task ownership and scoped preparation | PR #24/#39 owner authentication and process health  merged-intent correction also reviewed |
| 210 | Independent module locks and explicit durable calendar state | Retained |
| 211 | Calendar operation safety and family assignment versions | Reviewed merged-intent correction |
| 212 | Gateway load isolation with measured latency and resource budgets | Retained |
| 213 | Authentic module CI and stable messaging test identities | Retained |
| 214 | Explicit bounded original-download requests | Retained |

## Reconciliation readback

Five reviewed spec branches were merged normally: the four existing branches
above, then [codex/tiden-primary-reconciliation](https://app.tiden.ai/1dc18e09-4a09-4d74-8300-cd4302622357/products/75bd0580-2af4-4d5a-b236-c5a9d7ff7ce1/branches/f359906f-8929-4034-9ba3-293458557143).
The final merge preview had 39 additions, 163 modifications, no deletions and
no conflicts. No force/auto-resolution or unreviewed-proposal bypass was used.

The actual main readback contains **256 specs: 220 Active, 30 Backlog, 6 Review**.
The 36 imported future/acceptance specs plus the two existing Review specs in
open PR branches preserve all **38 open legacy identities**. The 19 historical
delivered source increments are indexed against maintained contracts in spec 260;
no duplicate implementation stories were created merely to show Done rows.
All **57 historical work IDs** are represented exactly once in that index.

The 212 original specification identities and all 933 historical source anchors
were checked against the complete baseline. Updated anchors keep the original
fields, line ranges and original metadata. Tiden regenerates source-record IDs
on update/merge and drops nested reverse_pr metadata; the affected sources carry
historical_source_id and historical_metadata_json receipts preserving the original
identity and metadata. This is not a claim that source-record UUIDs stayed equal.
Parent references were checked after resolving branch-copy IDs to main IDs.
The merge also exposed missing component assignments on specs 115, 155, 182 and
205; all four were restored to their original component IDs before the final
full inventory export. The General index received an explicit gate-interpretation
note after the measured main verdict.

Native provider contract 258 and upload contract 259 close two source-documentation
gaps. Seventeen already reported tests were linked after reviewing their actual
assertions (ten native, seven uploads); no tests were renamed or ingested.

At 2026-10-16 05:50 UTC, drift readback reported **clean**, with both watermark and
Git main at 27ec8bfc4d0a328dd4b639bab138cc21f6853ce7: zero commits/files behind.
This is the reviewed 112-commit synchronization, not an empty watermark reset.

The measured branch gate was **NOT VERIFIED**. The measured main gate was
**BLOCKED**: its coverage failures name exactly the 36 queued/acceptance specs
and General index 260. The server includes Backlog/Review/General in this gate.
Pass-rate and freshness criteria passed; these coverage gaps do not assert a
runtime regression. No artificial test links, status changes or risk acceptance
were used to turn this whole-product plan into a green release verdict.

The standard six Tiden skills were confirmed locally. generate-tests and
triage-issues were installed through the official CLI alongside the four already
present skills. A project runbook supplies DeedKit-specific policy without
forking the vendor skills. Global hooks/trust settings were not changed.

## Preservation review details

The intended changes include 60 corrected existing bodies, retained historical
sources plus current anchors, and new child counts from imported work. Original
spec component assignments were preserved, including the four restored above.
The product and all 15 components are unchanged.

The four older merges bring 445 existing branch-local records into main: 350 test
cases and 95 suites. Their identities and source signatures match the reviewed
merge previews. The original 2,472 test records retain their IDs, names, paths,
signatures and parent identities. For 123 accepted proposals, the server adopts
an existing main test; the review matches its unique signature, file path and
external ID, rather than copying an old branch's display title over it.

There are 151 additional accepted links; all 1,952 old link objects remain exactly
unchanged. One reviewed pair was already linked. The complete main link inventory
contains 2,103 links, including the 17 native/upload links.

Concurrent authentic CI updated the latest-execution metadata of 2,219 existing
tests: 12 cite run 375 and 2,207 cite run 376. Both were read back. They identify
source d9329d75ceffebd9fcc64b71b17f93b3d07650c7 and
[GitHub run 38853804153](https://github.com/GoSync-Inc/deedkit/actions/runs/38853804153).
Run 376 explicitly reassociates those existing reports with the release-admission
intent; it is not a new execution or proof for Git main. The exact-main source
audit reference remains run 38780132404 above.

Server aggregate child/descendant counts changed on 24 test suites during branch
merging. Each suite's actual main child-ID set was checked and remains unchanged;
no test hierarchy was renamed or rewritten to match an aggregate counter.
These observed metadata differences are separately enumerated in the private
exact before/after digest allowlist, not silently ignored by the comparator.

## Maintenance procedures

Moved here from docs/operations/tiden-product-workflow.md (process v2, 2026-10-19). They matter
only for bulk Tiden maintenance.

**Snapshots and preservation.** Before a bulk change, export complete specs, sources, test
identities and links with `scripts/tiden_snapshot.py`; keep private snapshots outside Git. Its two
complete reads must agree; a network or pagination failure is an incomplete export, not
permission to claim preservation from equal counts. Afterwards compare exact object and link
identities and historical anchors against a reviewed allowlist of intended changes with
`scripts/verify_tiden_preservation.py`. Report source coverage, assertion review and live
acceptance as distinct audit depths. Leave experiments with open code PRs on their own branches;
an old recovery branch may duplicate the baseline: record its disposition instead of merging
duplicates or deleting requirements.

**Provenance.** The CLI 0.33.0 source update and merge path regenerates source-record IDs and
does not round-trip nested `reverse_pr` metadata. Keep each affected original ID in
`metadata.historical_source_id` and its complete original metadata as serialized JSON in
`metadata.historical_metadata_json`, alongside the original fields; read them back before a bulk
update and verify them after merge. Never overwrite an existing provenance receipt.

**Drift watermark.** The drift workflow advances the watermark after every merge with
`--reason empty_sync` once every changed code file is anchored (TIDEN-CI.md). `empty_sync` is a
compare-and-set on `--from`: on 2026-10-18 a stale `--from` returned `"advanced": false` and the
current one advanced (receipts on ANYM-355); only the drift workflow uses it. A manual advance
follows an audit that checked every spec anchored to the files changed since the previous audit,
anchored or explained every unanchored changed file and merged its edits on Tiden main; it merges
an empty drift-sync branch (`tiden branch create --sync-base <W> --sync-target <SHA> --sync-repo
github.com/GoSync-Inc/deedkit`, recorded as `sync_merge`). `baseline` only sets a first watermark
and `bootstrap` is never used. A commit after the audited target is fresh drift.

**History.** The gate observed on 2026-10-16 counts queued `Backlog`/`Review` specs and the
General index in coverage, so a branch reads `NOT VERIFIED` and main `BLOCKED` for those gaps even
when implementation evidence passes; do not add placeholder tests, link unrelated tests or change
statuses to suppress it. The initial import placed future requirements on main; in CLI 0.33.0 no
main-to-proposal relocation preserves the spec identity (`spec update --branch` makes a
copy-on-write proposal and leaves the original), so those records stay and the scope debt is
reported. The release spec sync's first semantic baseline was
69486f43b7140632c5eeed6a27690683e3a82c68 (the ANYM-314 catch-up audit); the daily delta audit
replaced that sync on 2026-10-19. An old `Active` baseline is not evidence of a running task.
