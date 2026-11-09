# Native WhatsApp adapter

whatsmeow is the primary implementation for new connections. Browser sessions
remain an explicit rollback provider. This does not imply that any existing
production account has already been switched.

## Build and configure

```sh
(cd whatsapp/whatsmeow && go test -race ./... && go vet ./... && go build -trimpath -o /tmp/anymsg-whatsmeow .)
docker build -f services/messaging/Dockerfile --target whatsapp-native -t anymsg-whatsapp-native:0.9.19 .
```

`deploy/whatsapp.example.json` selects `provider: whatsmeow` and an absolute
`native_executable`. Use private, nonoverlapping session/events/pages directories
per account and retain the existing per-account permit. Configurations without
`provider` retain the legacy `webjs` implementation. `whatsapp.webjs.example.json`
is the explicit legacy example. Neither implementation reads or converts the
other implementation's authentication database.

Compose >=2.24.4: combine `compose.yaml`, `compose.whatsapp.yaml`, then
`compose.whatsapp-native.yaml`. Pair using the existing owner-TTY pair overlay;
logging is disabled for pairing. The code and auth belong on the server. No auth
copy to a desktop is needed. The pair process checks the expected account before
success and exits; starting the collector is a separate operator step.

### Serve mode (session holder, ANYM-937)

`anymsg-whatsmeow --serve <socket-dir> --config <path>` runs the adapter as the account's session holder
(COLLECTORS.md, Session holders): it outlives collector restarts, so recreating the collector no longer reconnects
WhatsApp. Its config adds `holder_dsn_file`: an absolute path to a 0600 file with the holder login's DSN
(production.md, Session holder role, step 4), as `host=... port=... dbname=... user=... password=...` or a URL;
other libpq keys are sent to the server as runtime parameters and refused. `--version` prints the build's
`holder_source_sha` and the holder protocol range. Child mode (no `--serve`) is unchanged: it still requires the
collector's inherited claim and exits when its stdin closes.

### Collector attach mode, image and Compose (ANYM-937)

A collector config with `holder_socket_directory` (absolute, `provider: "whatsmeow"`, apart from the session, events
and pages directories) attaches to the holder instead of starting a child: it opens no session directory, takes no
host or session lock, makes no claim and never spawns the adapter (`messaging_service/whatsapp_holder.py`). Both
processes read the same `whatsapp<n>.json`, which then also carries `holder_dsn_file` (`/run/secrets/holder_dsn`) for
the holder. Without the key the collector runs child mode unchanged; that is the rollback.

- Image `whatsapp-holder` (production.py BUILDS; `whatsapp/whatsmeow/Dockerfile`, built from that directory alone, a
  `scratch` image with the static binary, CA certificates and licenses, UID 10001). Its smoke check is
  `anymsg-whatsmeow --version`. A release that changes only `messaging_service` keeps the holders (ANYM-895). It is
  built on the host, not published by CI. Labels: `io.deedkit.holder.protocol.{min,max}` on the holder image and
  `io.deedkit.collector.protocol.{min,max}` on `whatsapp-native`.
- Overlays `deploy/compose.whatsapp1-holder.yaml` and `compose.whatsapp2-holder.yaml`, one per account, after
  `compose.whatsapp-native.yaml`: `whatsapp<n>-holder` owns the session directory, the `session-locks` bind, the holder
  DSN file, the socket directory `./private/whatsapp<n>-holder` and the account's spool volume (the events directory
  both use), reads `files` and `file_uploads` read-only at the collector's paths (send_file), and has
  `stop_grace_period: 25s`. Its collector keeps its DSN, config, permit, spool and files mounts and adds the socket
  directory; it loses the session and `session-locks` binds. The other account is untouched.
- Where the holder writes (its root is read-only): `/sessions` (`auth.sqlite`, `capture.sqlite` and their rollback
  journals, `.holder-generation`, `.holder-terminal`, history downloads in `.native-tmp`); `/spool/events` (captured
  events, `send-receipts/`, `operation-outcomes/`, `.history-status`, `.repair-status`, `.transport-failure`);
  `/spool/pages` (media downloads in `.native-tmp`); `/run/deedkit/holder` (`token`, `control.sock`,
  `send.sock`). Avatars and thumbnails are capture rows, not files. SQLite's own temporaries
  (sorts, temporary tables) use the overlay's tmpfs `/tmp`, as the collector's child mode did. `/data` is mounted
  read-only (registered files and uploads for send_file). Guards: `TestNoAdapterWriteUsesTheProcessTempDirectory`,
  `TestNativeMediaTemporaryStaysOutsideSpoolQuota`, `TestStartupSweepsOnlyStaleNativeTemporaries` and the overlay test
  in tests/test_whatsapp_holder_image.py.
- Source status: `collector_health.operations.holder` holds `{state: attached|refused|disconnected, generation,
  protocol, holder_source_sha, code, reason, planned_end, observed_at}`. A refusal (`holder_unavailable`,
  `holder_busy`, `collector_lease_unverified`, a protocol mismatch) is written once and retried in-process: every 60 s
  for a protocol mismatch, else 5 s doubling to 60 s, inside the connect timeout; then the ordinary connection
  backoff applies. A `collector_lease_invalid` answer at attach stops the collector (`collector_lease_lost`).

### Session holder cutover (live action)

The cutover stops and starts provider sessions: it runs only on the owner's exact confirmation through the
orchestrator (AGENTS.md section 1), asked once for both accounts, never during a call, and after ANYM-896 (session
marks, pair rule, night window) and ANYM-936's hand release (the holder logins) are live. The plan gets its own plan
review before it runs. Each step is read back and has its undo; `S` is the serving release's SHA (`production.py
status`), so no other change rides along.

0. Preconditions, read-only: the installed host tool comes from a release that contains ANYM-937 part 2 (its
   `production.py` BUILDS names `whatsapp-holder`; an older tool refuses a `services` entry it does not know). Pause
   autodeploy with `/etc/deedkit/autodeploy.pause` (reason: holder cutover) until the last step or the undo.
1. Holder logins: the DSN files `deploy/private/whatsapp1-holder.dsn` and `whatsapp2-holder.dsn` (0600, UID 10001)
   from ANYM-936's release. Read back: `stat -c '%a %u' <file>` prints `600 10001`. Undo: remove the files.
2. Socket directories: `install -d -m 0700 -o 10001 -g 10001 deploy/private/whatsapp1-holder` (and `whatsapp2-holder`).
   Read back with `stat`. Undo: `rmdir` them.

Then per account, `whatsapp1` first and `whatsapp2` only after `whatsapp1` verified:

3. Back up `whatsapp<n>.json` and `/etc/deedkit/production.json` (a per-account copy, `.before-holder<n>`). Add
   `"holder_socket_directory": "/run/deedkit/holder"` and `"holder_dsn_file": "/run/secrets/holder_dsn"` to
   `whatsapp<n>.json`; append `/srv/deedkit/deploy/compose.whatsapp<n>-holder.yaml` after the native overlay in the
   project's `files` and add `"whatsapp<n>-holder": {"target": "whatsapp-holder", "session": true, "collector":
   "whatsapp<n>"}` (ANYM-896's marks, production.md "Session holders") to `services`. Read back with `jq`.
4. Read-only: no active call (production.md, the active-call check).
5. `production.py prepare --sha S`: it builds and smoke-checks `deedkit-production-whatsapp-holder:S` from the
   release checkout and resolves the candidate. Review it: only `whatsapp<n>` (new mounts) and `whatsapp<n>-holder`
   (new) change. A refusal stops here: undo step 3.
6. `production.py apply --sha S`. Order does not matter for safety: a holder started before the child-mode collector
   stopped waits in-process for the session locks, and an attach-mode collector retries until the holder serves.
7. Verify: source status shows `operations.provider.name = whatsmeow` and `operations.holder` `state=attached`,
   `generation=1`; the holder logs `holder.claimed` and one `holder.provider_connected` with generation 1; one live
   event is captured (`last_ingest_at` moves). Receipt: times, those log lines and the generation (criterion 10).

Last: remove the autodeploy pause.

Undo of one account (also after the cutover; the other account's holder and configuration stay): `docker compose $F
stop whatsapp<n> whatsapp<n>-holder` ($F: the current file list); restore that account's two `.before-holder<n>`
copies (for `whatsapp2` this keeps `whatsapp1`'s holder entries, taken after its cutover); `production.py prepare
--sha S` and `apply --sha S` recreate `whatsapp<n>` in child mode (one WhatsApp reconnect); then `docker compose
$F rm --stop whatsapp<n>-holder` ($F as before the restore, so it still names the overlay). The session store is never copied or converted; it stays in
`whatsapp<n>-session`.

Measurement (criterion 11, 7 days after the cutover, read-only): every `whatsapp1`/`whatsapp2` recreate in the
release receipts leaves the holder's generation unchanged (`.holder-generation` and `operations.holder.generation`);
a host reboot (another boot ID) is excluded.

## Contract and limits

Both providers use the same fenced session owner, private policy with expiry,
independent action and background IPC lanes, durable archive spool and outbox.
No send is retried after an uncertain result. Passive reads do not mark read.
Native captures are journaled before ACK and replayed into the ordinary spool.
Both providers capture a round video (PTV, native type `ptv`) as a video
attachment with its duration and a media job; its original downloads with the
WhatsApp Video keys. On the native adapter a view-once round video, like other
view-once media, is not retained, also after a reload from its capture cache.
The native adapter does not capture the sender-key part that whatsmeow emits
as a separate event before a group message, so the message that follows is
archived with its content. An album photo or video (`associatedChildMessage`)
is captured as its own photo or video with a media job; the album parent and a
message history notice are service events. Template, interactive, buttons,
list, product and group invite messages keep their visible text (header media
is not fetched). A business placeholder masked from linked devices stays an
empty `unknown` message of native type `placeholder` (ANYM-471).
A message stored before that as `unknown` with no text and no file gets its
photo, video or document (one file, with or without a caption) when the same
message is read again at the same provider time: a new revision with a media
job, the empty revision stays readable, and no version conflict is raised. It
is no later evidence, so it resolves no open conflict; any other shape keeps
today's outcome (ANYM-808).
Rows captured before ANYM-471 stay `unknown` in the archive. While the
provider is connected, the adapter counts them once per `REPAIR_VERSION`
(`repair.go`, ANYM-809): it runs the current classifier on each retained raw
(at most 32 journal rows a tick, each read in a statement of its own; it
yields before each statement when capture has queued work, and writes nothing unless
8,192 free pages stay under the file's page ceiling for capture) and tallies
`would_add_content`, `label_only`, `same`, `no_raw`, `placeholder`,
`sender_key_only` or `parse_error`. It rewrites no journal row and publishes
nothing; its only write is one `repair_progress` row in `capture.sqlite`, and
`.repair-status` carries counts and a fixed error code (`repair_read`,
`repair_storage`) to `get_sync_status` (detail=full, `native_repair`). A
rollback to the previous adapter ignores the table. The repair itself is a
follow-up.
An exhausted local cache is not a complete WhatsApp history.
History is partial by contract: the phone's batch after pairing (device props:
7 recent days, 50 messages per chat), live capture since, and explicit
`request_chat_history` pages. The collector's catch-up reads only the local cache.
When that read returns nothing older than the catch-up pointer on two consecutive
scans, the chat gets an unverified span (COLLECTOR_STATES.md, `history`): the
interval between the previous head and the pointer, which the archive can neither
prove missing nor prove covered. No provider request is made for it unless the
phone repair below is switched on; closing a span needs the phone's correlated answer.

Phone repair of unverified spans (ANYM-814), collector config
`whatsapp_gap_repair: true` (default `false`; the adapter reads the same key and
refuses `gap_repair` with `gap_repair_disabled` when it is off, before any
reservation). After each scan the collector reads `gap_repair_status` and asks
for at most one span through `gap_repair` on the background lane. Contract:
- What the phone sees: a history-sync request (`BuildHistorySyncRequest`, 50
  messages before an anchor) from its own linked device, sent as a peer message to
  the owner's own primary device. The phone uploads up to 50 messages of that chat
  for this device. No chat message, no contact notification, no read receipt.
- Durability: `repair_requests` in `capture.sqlite` keeps account, chat, span id,
  anchor, the request id and `reserved` → `sending` → `sent` → `answered` |
  `no_answer` | `uncertain`. The request id is generated before the send and
  `sending` is committed before any network I/O. On adapter start a `reserved` row
  is released without a send and a `sending` row becomes `uncertain`; a failed or
  unknown send is `uncertain`. An uncertain anchor is never asked again and its
  span ends `request_uncertain` (ANYM-441).
- Bounds: one outstanding request per account; the account's history request row
  is shared with `request_chat_history`, at least 5 minutes after either; a 10-minute
  answer deadline, then `no_answer`; per anchor at most 2 `no_answer`, 24 hours
  apart; per span at most 10 answered requests; two consecutive `no_answer` pause
  the repair for 6 hours (`phone_unreachable`), spending no answer budget.
- Correlation: only an ON_DEMAND history sync whose `PeerDataRequestSessionID`
  equals the reserved request id is recorded as its answer: the count and oldest
  of the messages capture kept, and how many it did not keep (skipped, excluded);
  one not kept ends the span `incomplete_answer`, never verified. An answer that
  arrives before the send returns completes the request. A cache page, a catch-up reaching the head, a pairing batch or an
  agent's own request closes nothing. If the phone does not echo the id, nothing
  closes and requests end `no_answer` (fail closed). The answer's messages are
  captured by the existing history worker like any history sync; the next request
  anchors on the answer's oldest message. The first anchor is the oldest cached
  message at or after the span's `to`.
- A refusal (`history_rate_limited`, an unavailable anchor, a cap) ends only that
  repair step; `provider_not_ready` keeps the reconnect grace of a page read. With
  the switch off the collector records `enabled: false` without an adapter call.
- Waiting holds no lock: the request returns after the send; live capture, sends,
  other history and media continue.

A native download maps the media server's answer to a fixed code (ANYM-815): HTTP 403, 404
and 410 are `media_unavailable`, a key that does not decrypt matching bytes is
`media_decrypt_failed`, HTTP 400, 401, 408, 429, 500, 502, 503 and 504 are `media_http_<status>`,
a failure of the local temporary file is `media_storage_unavailable`, and anything else stays
`media_network_error` (MEDIA_STORAGE.md, failed WhatsApp files). WhatsApp keeps media about 30
days (an integrator estimate; WhatsApp publishes none), so a file that was already older when it
was queued usually cannot be fetched from its servers; the phone may still have it.
`anymsg-whatsmeow --config <file> --media-keys` answers the operator media inventory: it reads
the config without creating or changing any directory, opens `capture.sqlite` read-only and
query-only, and never the provider client, the device store or the session owner lock, so it
runs beside the collector's adapter. It reads JSON lines
`{"id":1,"method":"media_keys","params":{"messages":[{"chat":..,"message":..}]}}` (at most 200
messages each, one journal row per statement) and answers
`{"id":1,"result":{"keys":["held"|"none"|"unknown",..]}}`: `held` where the newest journal row
holds the message's media key, `none` where the journal has no row or the row no key, `unknown`
where the row could not be read or decoded. Each lookup is bounded to 200 ms, so its shared
SQLite lock never outlasts the capture writer's 5 s busy timeout; a slower one answers
`unknown`. An invalid line answers `invalid_request`. Without a journal it exits with
`media_keys_unavailable` and creates nothing.
`anymsg-whatsmeow --config <file> --menu-reply-shapes` (ANYM-837) is the reference check for menu
replies: with the same read-only, query-only journal access and no provider client, device store or
session owner lock, it pairs every menu reply the owner sent (list, buttons, template or interactive
response) with the menu it quotes, when the journal still holds it, and prints one JSON report of
relations per unit (menu kind / reply kind): the names of the fields that are set, the response name
and version, which field holds the chosen option's ID, how a template's selected index relates to the
menu (its explicit `Index` or a position), whether the reply text is the option's title, and whether
the menu is quoted. It prints no ID, label, text, chat or message identifier, and no word or number a
message chose: response names, parameter keys, versions and enum values are printed only from a fixed
list of protocol words, anything else as `other`. It reads one journal row per statement, each bounded
to 200 ms (the driver's interrupt covers a statement only until its first row), up to the journal's
newest row when it started, at most 500,000 rows and 2 minutes; a slower row or a spent budget ends the
scan with `incomplete: true` and its cause. Without a journal it exits with `menu_shapes_unavailable`
and creates nothing.

The account-specific operation manifest reflects the selected provider after
an identity-verified connection; platform counts are legacy defaults only.
Native global revoke uses the explicit revoke operation. It never degrades to
local deletion. Native PN/LID mappings require observed message/store evidence.
Read-only reactions, pins and receipts are bounded local observations with
explicit coverage; absence is not proof of no reaction/read/pin.

Changing providers must not erase previously observed state. The central
`message_receipts` table is independent of browser/native sessions and remains
authoritative evidence for archived delivery/read state. Current native
`get_message_info` still reads its private cache; merging central evidence into
that response is a follow-up. Reaction and pin observations do not yet have a
normalized central registry: retained provider caches and saved command results
must be preserved before removing the old provider state. Do not promise that
uncollected historical metadata can be recovered. An empty or partial new cache
must not overwrite a known snapshot or imply that a reaction/pin was removed.
Historical observations need their original timestamp, source and completeness;
they do not prove the current live state. Unknown native unread counts likewise
must preserve a previous known count without refreshing its observation time.

Remote address-book add/delete and business label replacement remain unavailable
until their server-side semantics are verified. A local contact name update is
not an address-book mutation. Rich URL previews are not generated by the native
adapter; ordinary URL text remains supported. Do not enable preview metadata
fetching by adding unrestricted URL requests.

`request_chat_history` is an explicit native operation under the existing
`read-live` grant and account operation opt-in. It asks the phone for at most 50
older messages, at most once per account per minute. It returns pending/partial;
WhatsApp may not supply the requested history. It does not run an unlimited
backfill or claim that a local cache is the complete chat.

Sticker upload accepts an existing WebP rather than silently converting an image.
Audio, voice and sticker uploads reject a nonempty caption before transfer.
Forwarding accepts retained ordinary content; control, view-once and poll replay
are explicitly rejected.

The account sync status includes `operations.provider_history` for the latest
native history batch: state, bounded retry count, next attempt and safe reason.
It is independent of chat coverage and never proves complete history. Its
`observed_at` and `checked_at` distinguish provider activity from status polling.
Group operations (`list_participants`, group `chat_info`, `create_group`,
renames, descriptions, participant changes, `leave_group`, invite links) send
each group query once (`NoRetry`, `group_iq.go`): whatsmeow's own group helpers
resend a frame after a reconnect, so a dropped change ends `unknown`, never a
second write. They answer within 20 s even when the library waits on a lock that
ignores contexts; after that answer no change starts. A description takes the
previous topic ID from its target read. A failed send keeps its reply waiter
until the reply or the next disconnect (closing it races the receive loop). Group
cache refreshes apply in reply order off the reply path, and a slow LID or
contact store write never delays a later cache write; a change waits for its
target read's refresh. Every other native operation, `send_text` and the history
request run the same way (ANYM-441): their context carries the fork's
`WithoutFrameRetry`, so the library never writes a frame again after a reconnect
(a drop after the frame may have left ends `unknown`), and they answer within
20 s (`send_file`: 75 s for upload plus send) by phase: before the change
`operation_target_unavailable` or `operation_query_failed`, after it `unknown`;
`send_text` answers `provider_operation_uncertain` at its deadline. A guard test
keeps each deadline at least 10 s under the IPC wait and the worker timeout.
Reply/edit/revoke/forward/media targets require the original in the native cache;
an older PostgreSQL-only record can still be read but may return
`operation_target_unavailable` until native history supplies it.

Menu choices (ANYM-837, SENDING.md "Menu choices"): `send_text` with `menu_choice {option_id, label,
menu_type}` and `reply_provider_id` = the menu answers that menu with its structured reply; `text` must equal
`label`. Inside `sendText`, before the second policy check, the adapter loads the menu's newest raw capture
(`menu_unavailable` when missing, over-size or dropped), refuses an owner-sent menu, and enumerates its options
with the same code that renders them for the archive (`menuOptions`, menu.go). The option binds by its RAW ID,
which must equal `option_id` exactly (already in the archive's cleaned form; else
`menu_option_id_unrepresentable`), and its cleaned `title - description` must equal `label` (else
`menu_changed`); several matches are `menu_option_ambiguous`, also a match past the 100 kept options or in a
menu the scan budget cut (uniqueness unproven); a link, call, copy, Flow or payment button
`menu_option_unsupported`. A buttons-menu button is classified by its native-flow fields and type before its
display text: a native-flow one is never a reply button, and its units are not answerable there. The archive
render (`optionLine`) shows the plain `[id: X]` only for an ID kept losslessly and answerable; others carry
`[id (inexact): X]` or `[id (native flow): X]`, which Python's parser never offers as a choice. Sender text in a
menu block (`menuShow`) has its square brackets shown as parentheses, so no title, ID, section, link or call can
forge a marker, and an ID with a bracket is inexact. The block's header `Menu options` (or `Menu options
(<label>):`) marks this renderer; Python answers only such a block, and a block of the older `Menu` header (#739)
is `menu_options_not_captured`. `--menu-answer-fixture <file>` runs one synthetic `send_text` with `menu_choice`
through `sendText` with fake dependencies (no client, store, session or network) for the Python contract test. Shipped units (`shippedMenuReplies`, mirrored by Python's
`menu_choice.SHIPPED_MENU_TYPES`): list row (`ListResponseMessage{Title, Description, ListType
SINGLE_SELECT, SingleSelectReply{SelectedRowID}}`) and button (`ButtonsResponseMessage{SelectedButtonID,
SelectedDisplayText, Type DISPLAY_TEXT}`), both with `ContextInfo{StanzaID, Participant}` of the menu and no
quoted copy. Every other unit (native-flow quick reply or single select, template quick reply) is
`menu_reply_shape_unverified` until a reply of the owner's own phone, held in the journal, gives its wire
shape (`--menu-reply-shapes` prints its relations, never values). All refusals come before the change starts;
then exactly one send on the existing receipt, journal and capture path, never retried.

## Safe account handover / rollback

1. Preserve existing auth and archive backups on the server. Finish or cancel
   queued commands before switching provider; never retry a `dispatching` or
   `unknown` mutation with a fresh request ID.
2. Stop only the selected old collector and confirm its lease/owner lock is
   released. Keep the old session untouched. Do not stop another account.
3. Configure new private native directories and pair the intended account under
   the ordinary permit, owner terminal and database lease. A pilot device may be
   used for read-only comparison, not concurrent production writers.
4. Start the native collector. Check exact identity, live incoming/outgoing,
   attachments and honest history coverage. Re-create action previews after
   provider changes. Key grants and account opt-ins do not expand automatically.
5. Roll back by stopping only the native collector, retaining its auth for later,
   restoring the old config/image/session, and waiting for old ownership release.
   The browser collector overwrites provider metadata after successful connect.
   Existing archive IDs and SQL migration checksums remain unchanged.

Mutating live acceptance needs exact owner-approved recipients/content/actions;
synthetic tests do not prove real delivery, group permissions or a long-running
soak. See `quality/whatsmeow-parity.md` for the current acceptance matrix.

## License

Our adapter code is MIT. The whatsmeow library is MPL-2.0. The build uses the
fork https://github.com/GoSync-Inc/whatsmeow (branch `deedkit/f16f9ca30d6a`,
revision fa2ca87d4bd8): upstream f16f9ca30d6a plus `whatsapp/whatsmeow/fork.patch`
(two hunks in `request.go`, one test file). `scripts/check_whatsmeow_fork.py`
proves in CI that the pinned fork equals upstream plus that patch before its code
is built. Upstream update: rebase the commit onto the new upstream commit, push
a new `deedkit/<commit>` branch, regenerate `fork.patch`, update `require`,
`replace` and the recorded hash in the script in one reviewed PR; drop the fork
once upstream has an equivalent option. Native images include a module/version
inventory and dependency license texts under `/app/whatsmeow-licenses`. The
modified files stay available under MPL-2.0 at the fork revision above and in
`fork.patch`. `group_iq.go` transcribes upstream group node shapes and is
itself MPL-2.0 (file header). Release source includes the Go module lock and
adapter sources.

### Wire receipts and shutdown

The provider can route a phone-number destination through its LID namespace.
A successful send checks both directions of that mapping in the local session
store and verifies the actual sender against the linked account's PN/LID.
Capture retains only the actual wire chat/sender, so an own-device echo refers
to the same archived message. The requested destination and public receipt
projection stay immutable for idempotency.

Schema 28 stores a separate `archive_reference` for the exact wire chat and
message. Send status links delivery/provenance to that reference and exposes it
only if the current key's chat scope permits it. Otherwise `message_id` and
`archive_reference` are null, `archive_reference_state` is `scope_restricted`,
and delivery evidence is unknown. The reference is provider acceptance evidence;
it does not prove that ingestion is complete or override collector exclusions.
Operation sends use the same contract inside their result `data`.

Any error after provider invocation remains uncertain and never causes an
application resend. A mismatching receipt must be checked against history before
another owner-approved send. Receipt validation is covered without live sends,
including PN→LID, group sender LID, conflicting maps and missing fields.

The native process registers inherited command pipes/sockets with the Go runtime
poller. SIGTERM, either command lane closing, or a full output pipe can therefore
stop it without waiting for another command. Subprocess tests cover these cases.

Upgrade messaging runtime and backup tooling together before applying schema 28.
The additive migration does not rewrite requested destinations or archive IDs;
older runtimes deliberately reject a newer database. A binary-only rollback of
the Go subprocess remains independent of the PostgreSQL schema.
