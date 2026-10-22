# Whatsmeow migration: capability parity and acceptance boundaries

Status: independent plan, source and offline acceptance review, 2026-10-15.
Executed evidence and resolved recovery findings are recorded below. A limited
read-only live smoke reported by the integration owner is recorded separately.
Live mutations, cutover and soak have not passed this review; implementation or
API feasibility alone is never counted as live evidence.

## Decision

A native process is compatible with the current architecture if it replaces only
the private provider boundary. The collector retains ownership leases, policy,
archive revisions, file admission, the outgoing/operation queue and the two IPC
lanes. The browser provider remains an explicit rollback selection. Existing
sessions are not portable and must not be overwritten.

The plan is sound with the following release gates:

1. Preserve source identity exactly. Canonical public IDs remain `c.us`, `g.us`
   and `lid`, with the existing `true|false_chat_stanza[_participant]` message
   representation. A verified PN/LID relation is an alias observation, not a reason
   to rewrite an old chat or message ID. Group participant suffixes and outgoing
   messages from other owner devices require explicit fixtures. A stable tie break
   is not proof of WhatsApp phone order for messages in the same second.
2. Close both crash windows: durable capture before provider ACK, and replay of
   that capture into the existing archive after a crash before/after commit. A
   persisted notification must remain pending until the referenced history has
   been durably handled; provider ACK alone does not prove archive completion.
3. Every catalog operation needs truthful, provider-specific support. A local
   cache write is not an address-book mutation; an empty uninitialized cache is
   not an observed empty list; a submission/server ACK is not delivered/read.
4. Recheck fresh policy immediately before every external mutation and before
   returning awaited read data. The independent action lane may not depend on a
   background media write, a history request, journal scan or stale session owner.
5. Unknown mutation results never enter an automatic retry path. Persist enough
   sent-message evidence for provider retry requests without resending application
   jobs. Bound that evidence by bytes/entries and make exhaustion visible.

## Audited sources

Current contract:

- `services/messaging/messaging_service/messenger_operations.py` (49 operations
  after adding explicit history requests; 45 marked supported for browser
  WhatsApp, 43 for native WhatsApp). These are dispatch capabilities, not a live
  parity score or a guarantee that an arbitrary archived target is retained.
- `whatsapp/provider.mjs`, `whatsapp/ipc.mjs`, `whatsapp/operations.mjs`,
  `whatsapp/capture.mjs`, `whatsapp/outgoing.mjs`, `whatsapp/receipts.mjs`.
- `services/messaging/messaging_service/whatsapp_collector.py` and
  `whatsapp_identity.py`.

Native API pin: `go.mau.fi/whatsmeow v0.0.0-20261014094007-f16f9ca30d6a`, built from
the fork `github.com/GoSync-Inc/whatsmeow` at fa2ca87d4bd8 (upstream plus
`whatsapp/whatsmeow/fork.patch`, ANYM-441; see `docs/whatsapp-native.md`, License).
The map below was checked against its `send.go`, `receipt.go`, `group.go`,
`user.go`, `appstate/encode.go`, `store/store.go` and event definitions. A named
upstream API proves feasibility, not AnyMsg implementation or live behavior.
Public references: [API](https://pkg.go.dev/go.mau.fi/whatsmeow),
[upstream](https://github.com/tulir/whatsmeow).

## Private provider contract

| Method | Current behavior to preserve | Native acceptance evidence required |
| --- | --- | --- |
| start | Exact expected account, no capture before verified ready, no automatic QR in collector | Fake connected wrong account is rejected before spool; saved-session restore; lease retained by child |
| policy | Short-lived private file, independent of occupied IPC; default send disabled and operations empty | Expired/disabled/excluded policy stops both lanes and callbacks; 0600/no-follow file handling |
| dialogs | Bounded inventory, account/chat filters; missing discovery is not absent history | Persisted inventory and partial discovery marker; configured chat not in inventory remains diagnosable |
| page | At most 100 messages, bounded frame, cursor suffix, pending distinct from exhausted | Same-second IDs, late overlap, frame truncation continuation; partial cache cannot claim full history |
| media | Bounded chunks; exact requested message/descriptor; secret keys/paths stay private | SHA/size validation, MIME normalization, cancellation/no partial publish, expired URL and disk quota paths |
| send_text | Independent action lane; exact target, 4000 UTF-16 units, quote and link-preview policy, no implicit read | Background blocked/backpressured while receipt returns; exact wire text/ID; uncertain receipt is unknown/no retry |
| messenger_operation | Allowlisted parameters/scopes, exact revision/preflight, bounded result | Every row below has routed behavior or explicit unsupported result; mutation approval remains acting-agent duty |
| request_history | Native addition; explicitly ask the phone for older history | Captured oldest-message anchor; 1–50 messages; durable per-account one-minute throttle; unknown outcome never auto-resubmitted; eventual partial coverage only |
| owner pairing | Owner terminal, expected phone account, deadline, retained ownership, QR stays local to operator | Failure/timeout releases owned process/lease; success includes saved-session reopen, not only pairing event |

## Complete operation map

`Yes` below means the **existing browser catalog** marks the operation supported.
`No` means the current catalog rejects it. Native mapping is a design target,
not a PASS result.

| Operation | Browser | Native API / required local evidence | Acceptance caveat |
| --- | --- | --- | --- |
| chat_info | Yes | Group `GetGroupInfo`; local chat settings/contact/history snapshot for DMs | Unknown unread/lock/read-only fields stay null; state basis and age are explicit |
| list_participants | Yes | `GetGroupInfo().Participants` | Group only, pagination/result cap, no out-of-scope identity exposure |
| get_reactions | Yes | Persist reaction events/target keys and replay | No direct full-history getter; unknown/partial baseline cannot return observed-empty |
| get_message_info | Yes | Persist `events.Receipt`, exact outgoing key and actor evidence | No direct historical getter; server/delivered/read/played distinct; group aggregate is not every member |
| get_pinned_messages | Yes | Persist observed pin/unpin messages and history pin snapshot | No direct getter; coverage unknown/partial until snapshot proven; expiry must be honored |
| list_topics | No | None in this contract | Remains unsupported; WhatsApp groups are not Telegram topics |
| get_profile | Yes | Device self ID/PushName; bounded cached fields | Full-account permission, missing status/platform not inferred |
| list_contacts | Yes | `Store.Contacts.GetAllContacts` | Cache, not complete address book; account + chat filters, deterministic paging |
| get_contact | Yes | `Store.Contacts.GetContact` | Cache miss stays unknown/unavailable; no guessed PN/LID |
| list_blocked | Yes | `GetBlocklist` | Filter results by policy; bounded paging and post-await policy check |
| send_file | Yes | `Upload` + typed media message + `SendMessage` | Registered accessible original only, exact SHA/bytes/name/MIME, 32 MiB bound, no arbitrary path/URL |
| forward_message | Yes | Clone stored original payload/context into `SendMessage` | Both chats allowed, exact source revision, no hidden stale/deleted source or unintended quote/private metadata |
| send_poll | Yes | `BuildPollCreation` + `SendMessage` | Preserve question/options/multiple semantics and result IDs |
| send_contact | Yes | Typed ContactMessage/ContactsArrayMessage + `SendMessage` | Full-account grant, trusted contact IDs, never synthesize a phone from a numeric LID |
| send_location | Yes | Typed LocationMessage + `SendMessage` | Finite/ranged coordinates and bounded description |
| edit_message | Yes | `BuildEdit` + `SendMessage` | Own exact outgoing ID/revision, plain text, no fake new message instead of edit |
| delete_message | No | `BuildRevoke` + `SendMessage` | Useful addition: global revoke only, never local-delete fallback; Python manifest/validation must become provider-aware |
| send_reaction | Yes | `BuildReaction` + `SendMessage` | Empty emoji removes, exact target author/chat/key; no paid action |
| vote_poll | No | `BuildPollVote` exists upstream | Separate scope/parameter/crypto contract; keep unsupported in this slice |
| pin_message | Yes | Typed PinInChatMessage + `SendMessage` | Pin-for-all, bounded duration; not merely pin-chat app-state |
| unpin_message | Yes | Typed PinInChatMessage UNPIN_FOR_ALL + `SendMessage` | Exact target, no pin-chat substitution |
| mark_read | Yes | `MarkRead` + explicit app-state choice | Current contract says whole_chat; native `MarkRead` is explicit IDs. Advertise changed scope or implement whole-known-chat carefully |
| mark_unread | Yes | `SendAppState(BuildMarkChatAsRead(false,...))` | Separate UI unread from peer read receipt; passive reads must never call either |
| archive_chat | Yes | `SendAppState(BuildArchive(true,...))` | Correct known last-message range; archive may unpin (upstream semantics) |
| unarchive_chat | Yes | `SendAppState(BuildArchive(false,...))` | Same external mutation checks, no local-only setting change |
| mute_chat | Yes | `SendAppState(BuildMute/BuildMuteAbs)` | Preserve duration and unmute, no integer/time overflow |
| unmute_chat | Yes | `SendAppState(BuildMute(false,...))` | Provider state submission, not local-only cache update |
| pin_chat | Yes | `SendAppState(BuildPin(true))` | Chat pin differs from message pin; account/provider limits stay errors |
| unpin_chat | Yes | `SendAppState(BuildPin(false))` | Explicit mutation, same target checks |
| star_message | Yes | `SendAppState(BuildStar(true))` | Exact sender/from-me key, group participant preserved |
| unstar_message | Yes | `SendAppState(BuildStar(false))` | Exact same semantics and ownership fence |
| create_group | Yes | `CreateGroup` | Never auto-send a private invite on rejected participant; suppress created ID outside current scope |
| rename_group | Yes | `SetGroupName` | Group only, existing title bound, permission failures truthful |
| set_group_description | Yes | `SetGroupDescription` | Current 255-char contract; no accidental clear/duplicate after timeout |
| add_participant | Yes | `UpdateGroupParticipants(ParticipantChangeAdd)` | No automatic invitation send; per-member provider failure not promoted to effect confirmed |
| remove_participant | Yes | `UpdateGroupParticipants(ParticipantChangeRemove)` | Explicit target, bounded exact participant set |
| promote_participant | Yes | `UpdateGroupParticipants(ParticipantChangePromote)` | Admin changes remain separately granted/approved |
| demote_participant | Yes | `UpdateGroupParticipants(ParticipantChangeDemote)` | Same, no synthetic success on member error |
| leave_group | Yes | `LeaveGroup` | External irreversible action requires exact owner instruction |
| get_invite_link | Yes | `GetGroupInviteLink(reset=false)` | Sensitive read/export remains mutation-classified, not groups-read |
| revoke_invite_link | Yes | `GetGroupInviteLink(reset=true)` | Never retry unknown rotation; returned invite stays scoped/private |
| add_contact | Yes | No reviewed high-level address-book API at this pin | `PutContactName` is local only. Requires verified sync patch/provider round trip, otherwise explicit unsupported |
| delete_contact | Yes | No reviewed high-level address-book API at this pin | Local cache delete is not address-book delete; PN-only condition remains |
| block_contact | Yes | `UpdateBlocklist(BlocklistChangeActionBlock)` | Policy includes affected contact, full-account grant; reconcile dhash correctly |
| unblock_contact | Yes | `UpdateBlocklist(BlocklistChangeActionUnblock)` | Same safety and uncertainty contract |
| update_profile | Yes | `SendAppState(BuildSettingPushName)`; `SetStatusMessage` | Preflight all fields, partial effect is unknown; status input requires matching text/duration semantics |
| request_chat_history | No | `BuildHistorySyncRequest` + `SendPeerMessage`; durable notification/download handling | Native addition with `read-live` grant and explicit mutation review; 1–50 messages, one request/account/minute, retained anchor required. Phone availability and older history remain unguaranteed; request submission is not download completion |
| get_labels | Yes | Persist `LabelEdit` app-state snapshot/events | No direct getter; Business availability and unknown baseline explicit |
| set_chat_labels | Yes | `SendAppState(BuildLabelChat)` | Existing operation **replaces** label set; applying only adds is incorrect. Persist/read current set, add and remove delta, uncertain multi-patch result |

## Prioritized test evidence

These are requirements for the candidate, **not a claim they already pass**.
Existing tests are useful seeds; exercising only Node with a fake client is not
native regression coverage.

### P0: data preservation and authority

- Native normalized fixtures cover private incoming, phone outgoing/self-chat,
  group incoming/outgoing, PN/LID/device suffixes, ephemeral/device-sent/view-once
  wrappers, unknown payload, reply/mention owner, forward, duration, same-second
  ordering. Compare the exact existing Python validator output and archive stable
  IDs, not a separately invented native schema.
- Capture failure prevents ACK; crash after journal write/before spool, after spool/
  before DB commit and after DB commit/before deletion all replay once without
  message loss, resurrected revokes or duplicated attachments. Include partial
  history notification progress and receipt/reaction ordering.
- Two account states cannot share auth, locks, queue paths or LID mappings. A
  stale collector epoch cannot publish media, mutate, ACK/advance app checkpoints
  or release the new owner's lease. Child retains inherited file lock if parent
  dies; child must terminate on closed action/control lanes.
- Fresh policy rejects disabled sends, stale expiry, removed operation and target
  exclusion after queued jobs/preflight and after awaited read. Forward source
  scope checked too. Native errors expose safe codes only.
- Submitted send/action timeout, malformed receipt, process restart and multipart
  profile/group failure stay unknown without automatic application retry.

Existing coverage seeds: `tests/test_whatsapp_identity.py`, `tests/test_whatsapp.py`,
`tests/test_collectors.py`, `tests/test_coverage_whatsapp.py`,
`whatsapp/tests/capture.test.mjs`, `whatsapp/tests/identity.test.mjs`,
`whatsapp/tests/operations.test.mjs`, `whatsapp/tests/coverage-contracts.test.mjs`.

### P1: actual IPC, media and operator behavior

- Native process offline fixture (fake client dependency), not just helper-unit
  tests: both anonymous lanes, request IDs/frame limits, unknown method rejection,
  policy rotation independent of occupied media pipe. Block and backpressure the
  background writer and prove action response arrives first. Cancellation closes
  synchronized ownership cleanly; do not silently reuse a half response.
- Media max bytes and disk reserve checked before allocation/download and again
  before publication. Deadline covers continuous trickling, cancellation removes
  partial file, descriptor changed/deleted mid-download cannot publish ready.
  Expired media remains unavailable unless separately configured bounded retry.
- History request persisted throttle survives restart; new data no longer makes
  existing per-chat checkpoints jump; page starvation across chats tested. Empty
  local history is pending/partial, never provider exhaustion.
- Receipt exact ID linkage, event basis and group/private audience remain truthful;
  receiving read-self/played-self is not a peer read. Reactions do not become a
  new textual message in the conversation turn model.
- Native Docker image runs without Chromium, as unprivileged UID, read-only root,
  bounded state/temp paths; packaged binary/required Go source/license included
  in intended distribution; Python wheel alone does not claim to install binary.

Existing seeds: `tests/test_whatsapp_lanes.py`, `tests/test_receipts.py`,
`tests/test_coverage_send_receipts.py`, `tests/test_collector_operations.py`,
`whatsapp/tests/media.test.mjs`, `whatsapp/tests/history.test.mjs`,
`whatsapp/tests/outgoing.test.mjs`, `whatsapp/tests/read-state.test.mjs`.

### Live/cutover evidence (separate gate)

The linked WA2 pilot previously established login/restore, sampled history and one
fresh outgoing self-message. It does not establish all operation parity or soak.
The new implementation needs read-only live restore/capture/media/receipt checks
on its independent saved device. Real send/admin/read-state actions need exact
owner approval. Switching production also needs per-account auth, archive alias/
ID dedup proof, reversible provider/config selection, queue drain snapshot and
rollback instructions. The repository migration may proceed independently.

## Useful additions kept narrow

- Enable reliable global revoke only for the native provider after its exact
  outcome/authorization fixture passes; retain browser denial.
- Add bounded persisted history requests using `BuildHistorySyncRequest`; maintain
  honest partial status and no unbounded fetch loop.
- Persist provider retry-message evidence so recipient retry requests can work
  after restart, with bytes/entries retention and encryption/private permissions.
- Consider explicitly configured, rate-limited `SendMediaRetryReceipt` for expired
  files later. It asks the phone to reupload and must not be a hidden infinite
  retry or clear an unavailable state before new evidence arrives.
- Newsletter/community management, join requests and poll voting are useful
  future features but require new scoped contracts. Raw `DangerousInternals`,
  arbitrary RPC, automatic logout, payment/business-commerce methods and arbitrary
  file/URL transfer are not part of this provider replacement.

## Independent integration review — 2026-10-15

The Python candidate preserves the previously reviewed owner boundary: the native
selector changes the executable only; both anonymous IPC lanes, per-lane locks,
last-moment database authorization, outbox uncertainty handling and session-lock
inheritance remain in the same path. Native setup does not resolve browser assets,
clear browser lock files, convert a Chromium profile or fall back to Node after a
launch failure. Provider observations are written under the collector database
fence before the connection becomes `connected`.

Native identity provenance was added to the existing source-message validator.
This does not weaken account-local one-to-one conflict handling or authorize
rekeying archived messages. It only permits the reviewed native observation
sources; an observation for an unrelated sender/chat remains rejected.

The account-specific manifest is the authoritative availability surface. The
candidate explicitly disables native address-book add/delete and business labels;
it does **not** establish full feature parity with the 45 browser operations.
Global revoke becomes native-only, with outgoing ownership and exact revision
checks. Rich URL previews remain unsupported by the native sender. These are
release limitations rather than successful implementations.

### Evidence actually executed by this reviewer

`tests/test_whatsmeow_contract.py`: **25 passed**, 4.07 seconds, local Python 3.13
and an actual locally built pinned Go adapter. All fixtures are synthetic, and the
offline Go entrypoint initializes neither a client nor an authentication store.

- Native config accepts no Node/Chromium executable; invalid provider or relative
  native executable fails; an existing config remains browser without rewriting
  the file.
- Private native auth-file existence/mode/type checks reject missing, public,
  symlink and directory targets without opening a database.
- The production Python launcher communicates with a synthetic native executable
  over both inherited lanes. A send returns while the background media pipe is
  deliberately blocked; the subsequent background result remains synchronized.
  Cleanup terminates only the owned child. Failure does not invoke the browser.
- Actual Go `captureEnvelope` output passes the production Python
  `validate_capture` and `CaptureSpool` drain for text, outgoing, image, document,
  voice and service messages. The assertions include exact serialized IDs,
  direction, type, MIME, duration and timestamp precision.
- A group capture retains the LID namespace/participant suffix and source-bound
  PN↔LID evidence, and recognizes the authenticated owner's LID for mentions and
  reply attention. Repeating a normalized capture preserves the archive content
  fingerprint.
- History-origin output uses `backfill`, which the archive accepts. The initial
  source audit caught a real `history`/`backfill` incompatibility; the runtime
  owner corrected it before this passing run.

An earlier broader command obtained 100 passing non-DB checks but did **not** run
33 PostgreSQL cases because no test DSN was supplied. Its one subprocess failure
was an unactivated PATH missing `python`. These are not counted as passing DB
acceptance. The integration owner runs the shared isolated PostgreSQL suite
sequentially; this reviewer did not reset or read a production database.

The above is not proof of actual native network-lane backpressure, ACK-loss
recovery, provider mutation effects, production attachment delivery or soak.
Those retain the independent Go/security and live gates listed above.

### Handover and rollback requirements

1. Before changing the code/provider, finish or explicitly cancel queued commands.
   Switching providers invalidates the affected saved previews and queued
   intents. Native previews carry an adapter binding; browser-only previews keep
   their legacy shape, so this update alone does not invalidate those previews.
   Terminal receipts remain readable; an unknown action must never be resubmitted
   automatically with a fresh request ID.
2. Preserve original server-side auth, encrypted backup and archive state. Stop
   only the selected session owner, verify lease/lock release, and select new
   private native directories without authentication conversion. Pairing must
   present a fresh QR through the supported owner-terminal flow and verify
   restoration before success.
3. Before cutover, compare actual same-account chat/message IDs, including group
   participant and PN/LID variants, against existing archive records. A synthetic
   serialization assertion cannot prove that two different linked clients choose
   identical namespaces on every real message. Any drift requires explicit alias/
   dedup handling, not bulk rewriting existing IDs.
4. Re-create previews and inspect the account manifest after an identity-verified
   native connection. Confirm fresh incoming/outgoing observations, media and
   truthful partial coverage. No existing API-key grant or account opt-in expands.
5. On rollback, stop the native collector, retain its state privately, restore the
   original browser config/image/session and wait for safe ownership release.
   Reconnect updates the provider observation and invalidates native previews.
   Keep both captured history and terminal receipts; do not replay uncertain
   mutations. No SQL migration/checksum changes are needed.

## Final independent implementation review, 2026-10-15

The reviewer read the Go runtime/capture/store/history/send/media/operations and
the Python provider selector, pairing, capability and queue integration. The
following findings were corrected by the implementation owners and re-inspected:

- Pairing, restore-proof and normal clients now share synchronous ACK and durable
  decrypted-event buffering. This avoids ACKing initial messages during pairing
  and closes the decrypt-ratchet advance / pre-capture crash window.
- Passive history downloads reserve durable attempts, resume interrupted work
  after persisted backoff, and stop after three tries. Failure remains partial;
  a new phone request needs explicit action. A completed request is not a claim
  that every chat or all dates are present.
- History download has private temporary storage and a 32 MiB compressed bound;
  zlib expansion is validated against 64 MiB before upstream decoding of the same
  immutable bytes. The upstream storage effects for mappings/secrets/names remain.
- Provider retry payloads persist separately from the archive with 1000-message,
  32 MiB and seven-day bounds. Retry retains the original stanza and is gated by
  current identity, readiness and policy; uncertain application commands are not
  automatically submitted again.
- Group participant changes preserve individual refusal and invitation-required
  observations. They neither expose invitation credentials nor auto-send private
  invitations. Expired observed pins no longer appear as current pins.
- A full export spool no longer blocks provider startup and its own consumer.
  Only `spool_full` is recoverable backpressure: captures remain in the durable
  journal, callback failure withholds ACK, and a lifecycle worker retries export
  after a fresh enabled policy check. Identity verification still precedes ready;
  other capture/replay errors cancel the provider. Startup does not erase or
  raise quotas. The production 10000-file restart boundary is exercised below.
- Replay now selects at most 32 unpublished entries / 16 MiB per invocation,
  with a 512 KiB envelope bound and an index of unpublished rows. The mutex is
  released between worker ticks; the complete pending journal is not loaded
  into memory. A single spool measurement supplies a conservative shared budget
  for every publication in the batch. A committed file disappearing between
  directory listing and per-entry stat is tolerated; symlinks and other stat
  failures remain errors.
- Latest history-batch diagnostics now reach the existing public sync status.
  Python accepts only bounded enum/numeric fields and a normalized timestamp
  from a private no-follow JSON file, then updates health under the collector
  lease. Provider replacement clears the observation. The report explicitly says
  `latest_native_history_batch`, `partial`, and `full_history_verified: false`;
  it does not imply all-chat coverage or disclose raw history/file contents.

Independent commands actually executed after those fixes:

- `go test -race -count=1 ./...` in `whatsapp/whatsmeow`: PASS, 8.170 seconds
  after the initial full-spool recovery change. Tests are synthetic/local, with
  real private SQLite and production helper functions. This includes
  `TestActuallyFullSpoolAllowsReadyAndRecoversAfterConsumerDrain` with 10000
  existing export files, a pending durable row across reopen, consumer drain,
  and exactly one recovered export with no missing/duplicate journal entry.
- After the final bounded-replay/concurrent-unlink changes,
  `go test -race -count=1 -run 'Test(ActuallyFullSpoolAllowsReadyAndRecoversAfterConsumerDrain|ReplayPublishesBoundedOldestBatches|ReplayBatchSharesConservativeSpoolQuota|SpoolMeasurementToleratesConcurrentConsumerUnlink)$' ./...`:
  PASS, 6.719 seconds. Besides the real-limit restart, these verify 65 pending
  rows drain oldest-first in 32/32/1 batches, a two-file shared quota is never
  exceeded, and the actual list/unlink/stat window is nonfatal while a symlink
  still fails. The runtime owner separately reports all 54 Go tests with race,
  vet and build passed; this reviewer did not independently repeat that full
  suite after the last two changes.
- `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_whatsmeow_contract.py -q`:
  **25 passed**, including actual compiled Go output through production Python
  validation/spooling and both inherited IPC lanes through a synthetic child.

### Recovery finding resolved offline

The previous P1 prevented an actually full export spool from recovering after a
database outage because the Python consumer waited for native readiness while
native startup waited for spool capacity. The final runtime change breaks that
cycle without acknowledging uncaptured data or dropping journal entries. Source
review and the real-limit offline regression above verify this correction.
Native reconnect, server-side redelivery and a sustained live archive outage are
still rollout checks; the synthetic test does not claim those have run.

The new Python diagnostics path and its privacy/fencing tests were inspected.
The parent integration owner reports the targeted PostgreSQL tests passed; this
reviewer did not run the shared database suite concurrently or count that report
as an independently executed test.

### Limited live smoke reported by the integration owner

On the designated pilot account, two cold saved-session restores completed in
0.950 and 0.701 seconds. A fresh outgoing self-message was captured under its
observed LID with the exact same source message ID as the existing browser
collector. Both observations use the same source second; this cannot establish
true delivery latency in milliseconds. This was a read-only collector check,
not an agent send. The reviewer did not perform that live run. Incoming events,
group/other-contact IDs, media, mutations and soak were not tested by this smoke.

### Explicit remaining limits

- Four browser capabilities remain deliberately unsupported natively:
  address-book create/delete and Business label read/replacement. Local cache
  updates cannot substitute for those remote effects.
- Rich link previews are not fetched; an explicit request for one is rejected.
  Plain URLs remain ordinary text.
- Exact quote/edit/revoke/forward/media actions require a message retained in the
  native cache. An older message existing only in PostgreSQL is still readable
  from the archive but may not be a native operation target until captured there.
- Contacts are a cache; reaction, pin and receipt reads are bounded observed
  evidence. Unknown baselines and missing history remain unknown/partial.
- Same-second ordering has a deterministic tie break, not an invented phone
  sequence. PN/LID identities retain observed namespaces. The reported self-chat
  ID match does not establish group/other-contact identity parity. Exclusion-policy
  behavior across actual aliases, native network
  backpressure/ACK redelivery, media delivery, actual mutations and soak remain
  rollout gates. Offline unit/contract passes do not satisfy those live gates.


### Linux CI timezone regression

The first native CI run exposed a test-only comparison bug: `time.Time !=`
compared location representations after JSON replay, rather than the timestamp
instant. It reproduced under `TZ=UTC`. The regression now uses `Time.Equal` and
explicit UTC / UTC+8 source cases. The runtime implementation is unchanged.
The implementation owner reran all 54 Go tests with race detection under UTC
(6.631 seconds), the two source-zone cases under Asia/Makassar, and vet: PASS.

The final static Linux candidate also restored twice on the VPS (0.801 / 0.921
seconds), read the retained self-message on both runs and exited normally.
All owned native pilot processes were stopped; both production browser collectors
were still running with restart count zero. No production provider cutover is
claimed by this acceptance record.
