# Archive search and reading

AnyMsg stores the messages and files of registered accounts and answers agents.
Telegram and WhatsApp are supported; the existence of an adapter does not mean an account
is connected or its history is complete. Coverage states are returned in the responses.

`search` accepts `accounts`, `chats`, `senders` — lists of up to 100 exact IDs.
Inside a list "or" applies; between lists and the earlier single filters —
"and". All conditions are intersected with the key's permissions. `null`/absence does not limit
the dimension, `[]` returns an empty result. A foreign ID in a list does not widen permissions; `search` answers `not_found` for it (below).
A sender ID belongs to the source/account; a match of strings does not merge people.

A message that replies to an archived message of the same chat carries
`reply_to: {message_id, snippet, sender_name}` in `search`, `get_messages` and
`get_context` (compact and full): `snippet` is at most 80 characters of the target's text
and is untrusted content. `reply_to_provider_id` stays. A missing, deleted or unreadable
target adds no `reply_to`, so nothing says which of those it was.

`search` with a `chat`, or a `chats` list with any member, that is unknown or outside the key's
scope returns the same `not_found` (404, `fields: ["chat"]` or `["chats"]`) for both, never an empty
page that looks like "no matches". Malformed values stay `invalid_filter`. `chats: []` matches
nothing. `get_messages` keeps its empty page.

Channels are excluded from `search` by default, so news posts do not crowd out
conversations. Pass `include_channels: true`, a `chat_types` list containing
`channel`, or name the chat with `chat`/`chats` to search channels; `get_messages`
and `get_context` read a channel as before. `chat_types` takes `private`, `group`,
`channel` and `unknown` (a chat without a stored type is `unknown`), the same values
as `list_chats`: "or" inside the list, "and" with the other filters and with
`exclude_channels`; `[]` returns an empty result and another value returns
`invalid_filter`. `include_channels: true` together with `exclude_channels: true` or
a `chat_types` list without `channel` is also `invalid_filter`. A `chat_types` list,
`include_channels: true` or `exclude_channels: true` turns a chronological archive
`next_cursor` from a bare message ID into an opaque cursor bound to the effective set of
types and to `include_channels: true`; continuing with another set, or adding or dropping
`include_channels: true` even with the same list, returns `invalid_cursor`. Only the same
normalized filter continues, for example dropping `exclude_channels: true` under the default. Relevance, fuzzy and
conversation cursors are always bound, and those issued before this filter existed
return `invalid_cursor` once: restart the search. Without named chats,
`result_evidence` counts only chats of the searched types; an empty effective set is
`empty_filter`.
`capabilities.search_options.search_channels_default` states the default.

`query_mode`:

- `all` by default: all words in any order;
- `phrase`: words in the given order as a phrase;
- `web`: quotes, `OR`, exclusion `-word`, for example `"срок оплаты" OR invoice`.

Search uses the built-in PostgreSQL dictionaries for word forms; languages, exact
search and the explicit approximate fallback are described below. Synonyms are not added automatically. `search` indexes message text;
for names there is [search_people](PEOPLE.md), for the content and names of files —
[search_files and get_file_text](FILE_INDEX.md). An empty
result does not prove the provider has no such message. See coverage.

`from_time` is inclusive, `to_time` is exclusive; dates with a timezone are converted to UTC.
`direction` limits to incoming/outgoing. The default order is `asc`,
`desc` starts with the newest. `next_cursor` is passed as `after` with unchanged
filters and order; it is a position in the live archive, not a fixed snapshot.
Changes of the source between pages are visible separately through `get_updates`.

Expanding a result:

1. `get_context(message_id)` returns the source message, neighboring records,
   attachments and `context_window`. Two bounded index passes replace
   sorting the whole conversation; the anchor message is included unless deleted.
2. Older than the window: `get_messages(chat_id, before=older_cursor, order="desc")`.
3. Newer than the window: `get_messages(chat_id, after=newer_cursor, order="asc")`.
4. Next pages — `after=next_cursor` with the same order. `after` and `before`
   together are forbidden. Identical timestamps are separated by a stable ID.
5. `get_attachment(attachment_id)` returns details and a temporary link to the
   original; pending/unavailable are not replaced with a preview. A file is not embedded in the JSON.

Search, context and history do not call Telegram/WhatsApp and do not move the ACK.
MCP and REST use one Store/policy layer. For a name, first call
`search_people`, then search messages by the returned account/sender_id.
For files, `search_files` gives attachment_id, the source message and the indexing status;
`get_file_text` returns the text in parts with the original's SHA256 and the source revision.

## 0.9 query options

`search_language` accepts `auto`, `simple`, `russian`, `english`, `indonesian`.
`auto` tries complete queries under matching PostgreSQL dictionaries; it does
not detect a message's language. `query_mode: exact` means case-insensitive
literal substring, including punctuation and identifiers. Existing `all`,
`phrase`, `web` modes and chronological `asc`/`desc` remain available.

`order: relevance` returns an opaque query/scope-bound `next_cursor`; pass it
unchanged as `after`. Editing the anchor can invalidate it. `fuzzy: true` is
an explicit fallback for a single word of 3–64 characters, used only when the
scoped primary query has no matches, with at most 200 ranked candidates.
`match_type` distinguishes morphology, literal and approximate matches.
For `all` and `phrase` message queries, relevance ranks three tiers: the typed
query as a case-insensitive substring, then its words adjacent and in order in
any word form, then other matches; cover density orders each tier. Stemming can
merge different words (договор/договориться), so an exact phrase stays above
scattered morphology-only matches. `web` and file search ranking are unchanged.

`compact: true` omits full message bodies while retaining snippets and identity.
`body_chars` bounds returned bodies; `include_coverage: false` suppresses the
verbose source coverage block without changing permissions. These options are
also available for history/context. Context accepts separate `before_count`
and `after_count`, always preserving the authorized anchor.

Message metadata includes the sender name with its `sender_name_basis` (fallback
order in [PEOPLE.md](PEOPLE.md#sender-names)), username, chat title (the order for a chat titled
only by its provider ID is in [PEOPLE.md](PEOPLE.md#chat-titles); `list_chats` adds `title_basis`) and type,
direction, and known reply/forward/media fields. Unknown metadata remains null.
Older messages acquire provider metadata as they are observed again; this does
not invent a new content revision.

### Forwarded messages

A forwarded message's `sender`, `sender_name` and `direction` describe the
forwarder, not the author. Search, history, context, the conversation's latest
message and the reply-queue hint (`awaiting_reply.forward_origin` in the compact
conversation view, `awaiting_reply_forward_origin` in archive and full rows) mark
every forwarded message with `authorship: forwarded_by_sender` and a
`forward_origin` object, in compact and full detail; the transcript adds
`переслано, автор: <name>` or `переслано, автор неизвестен` to the line. A message
that is not forwarded has neither. An agent must not quote
forwarded words as written by the sender; it names the origin, or says the
author is unknown. `capabilities.search_options.forwarded_messages` states this
rule.

`forward_origin` holds only what the provider stated in the captured message:

| Field | Meaning |
| --- | --- |
| `kind` | `user`, `chat`, `channel`, `hidden_user`, `newsletter`, `unknown`, or `not_captured`; `status` is reserved (a WhatsApp status forward is `unknown` with `provider_origin_type: STATUS`) |
| `sender_id`, `sender_name` | Original sender ID and name (a channel or newsletter title for those kinds), from the message and the provider's cached entities only |
| `chat_id`, `provider_message_id`, `post_author` | Origin chat or channel, the original post ID and the signed post author |
| `sent_at` | Original send time, when the provider states it |
| `forwarding_score`, `provider_origin_type` | WhatsApp forward count and the raw `ForwardOrigin` enum name (`UNKNOWN`, `CHAT`, `STATUS`, `CHANNELS`, `META_AI`, `UGC`) |
| `basis` | `telegram_fwd_from`, `whatsmeow_context_info` or `whatsapp_web_js_message` |

Telegram fills it from the update's `fwd_from` without a provider request:
`user` with the original sender ID (and name when Telethon has the entity
cached), `hidden_user` when the original sender hid the link to their account
(only the name is stated), `channel` or `chat` with the origin chat ID, and
`unknown` when `fwd_from` names no sender. WhatsApp does not
carry the author of an ordinary chat forward: the native adapter states a
`newsletter` origin with the newsletter JID, name and server message ID when
present, otherwise `unknown` with the forwarding score; whatsapp-web.js states
`unknown` with its cached score. Compact message rows cut `sender_name` and
`post_author` to 64 characters. Queue rows and chat heads (the hint,
`last_message`, the conversation's latest message) keep only `kind` and
`sender_name` in compact detail; `detail: full` and the message tools carry every
field. A re-read of the same origin (same `kind`, and no conflicting `sender_id`,
`chat_id`, `provider_message_id` or `sent_at`) keeps fields an earlier capture
stated, for example a name Telethon had cached then; any other origin replaces
it. Compact names show control characters as U+FFFD. `not_captured` marks a message archived before
origin capture: it is not a provider-hidden origin. It gains its origin only when
ordinary capture or backfill reads it again, with the same revision and no
message conflict; there is no bulk rewrite.

`forward_origin.chat_id` and `sender_name` are provider data carried by the
visible forwarded message. They are returned with it even when they name a chat
the key cannot read, and never let the caller resolve, list or open that chat:
`resolve_reference` and `get_context` keep their access checks and answer such a
chat like one that does not exist.

Collectors capture the origin only with `ANYMSG_CAPTURE_FORWARD_ORIGIN=1` in
their environment ([SETUP.md](SETUP.md#forwarded-message-origin)); it is off by
default. The server always accepts and returns a
captured origin. Enable the flag only when the release a rollback would return to
already contains this change: an older server rejects a capture that carries the
origin, so the message would be skipped.

### Person filter (ANYM-283)

`person` (a person id from a `search_people` row) matches messages sent by any identity the owner
linked to that person and the key can read, as exact `(account, sender)` pairs, ANDed with the other
filters; no text query is needed. Cursors of a `person` request are bound to it. Linking, visibility
and errors: [PEOPLE.md](PEOPLE.md#linked-people-anym-283-schema-43).

### Forwarded copies in search (ANYM-273)

A `search` with a non-empty `query` returns forwarded copies of one message as
one row. Copies group only by the stated origin, never by text: the origin chat
and message ID, or else, for a `user` origin, the origin sender, time to the
second and exact text (one person can send two messages in one second). Origins without such a key (`hidden_user`, `unknown`, `not_captured`,
WhatsApp forwards without IDs) never group. The key holds the platform, not the
account, so one forward received by two owner accounts is one row. An archived,
not forwarded original joins its group when it also matches: by its chat and
message ID, or by its sender, second and text; only the earliest such original
joins.

The row shows the original, or else the earliest copy. It adds `copies_total`
(every readable member, this row included), `latest_copy_at` and `copies`: up to
20 other members, earliest first, each `{chat, message_id, sender, sent_at}`.
Members are only messages that the key can read and that pass the same filters,
so a chat outside the key's scope never appears. The group sits, and
`next_cursor` anchors, where its first member in the requested order sits: in
`desc` order a fresh forward of an old original keeps the original near the top.
Groups are formed over all matches before the page cut, so pages never repeat or
skip one. `search.collapse_forwards` reports whether grouping applied;
`collapse_forwards: false` returns every copy, as before, and binds its cursor.
`get_messages`, `get_context` and empty-query listings never group. Provider hits
in `result_evidence.live` are not grouped. `detail=transcript` shows the shown row
only; its copies are not listed there. Grouping applies to forwards archived
with a stated origin; forwards stored before origin capture keep `not_captured`
and stay separate rows.

MCP/REST `list_chats` defaults to recent activity order. `order: activity` and
`order: last_activity_desc` are aliases; `order: id` retains stable catalogue
traversal. Direct Store calls retain the legacy ID default. Restart pagination
when changing orders; activity pages use an opaque cursor, not a bare chat ID.
`limit` defaults to 25 rows in MCP, REST, the Store and the gateway
`messages_list_chats` tool, and accepts 1–100; larger values return
`invalid_limit`. Continue with `next_cursor`, including for a full catalogue
traversal.
Account/type/date filters and
`exclude_channels` are supported. `awaiting_reply` is only a heuristic from the
last known incoming message in a private chat, plus explicit attention in groups:
the provider marks an owner mention, the cached quoted participant is the owner,
or the quoted message is an archived outgoing message in the same account/chat.
Group size is irrelevant to an explicit mention/reply. Ordinary group chatter and
channels are excluded. Later messages by others do not clear the hint; a later
owner message does, heuristically. The default `awaiting_reply_days: 14` excludes
old attention based on the incoming/mention/reply message's own time; unrelated
group chatter cannot revive it. `0` removes the age limit; values through 365 are
supported. Known provider bot senders are excluded from private reply hints;
names and username suffixes are not used to guess bot identity. Missing bot
metadata is explicitly outside the filter's coverage. This does not prove a request was fulfilled.
The result supplies `awaiting_reply_message_id`, `awaiting_reply_basis` and a
snippet of the specific group message; last chat activity remains separate.
Old messages without captured mention metadata are not assumed to have none.
`group_attention_coverage=observed_mentions_and_replies_only` states this limit;
later reconciliation can acquire missing provider metadata. Reading the queue
does not alter provider unread state or mark a request completed.
Unread/muted/archived values remain unknown when the provider has not supplied
reliable evidence.

`get_sync_status` defaults to `detail: compact` for an account overview: coverage,
last message/ingest/scan timestamps, alerts, separate operation states and file
queue/error counts. `detail: full` retains complete account diagnostics. A
specific `chat_id` always retains its detailed history/coverage evidence. Cached
summaries include observation time and cache age; compact rendering never mutates
the full cache or implies complete history.

`get_updates` supports filters by accounts, chats, senders, origins, kinds,
direction and dates. A consumer name binds its initial filter; a call with a
different filter fails with `consumer_filter_mismatch`. Choose a new consumer
name for a different view; this prevents a filtered ACK from skipping events
intended for another view. Rebinding an existing consumer requires
`reset_filters: true`, its current `expected_ack_version` and `cursor-write`
permission: the new filter replaces the old one, the ACK version increases,
outstanding deliveries are dropped and the position is kept. ACK order remains commit order,
not the original timestamp of a backfilled message.

### Incoming overview: `get_updates` with `view: by_chat` (ANYM-458)

`view: by_chat` answers "what is new across my accounts" in one read-only call. It counts created
incoming message rows (`message.created` events; edits, deletions, receipts and outgoing messages
are not counted) after the consumer's acknowledged position and, when given, from `from_time`:
`window` is `acknowledged_position`, `from_time` or `acknowledged_position_and_from_time`; both
bounds always apply. Without a consumer `from_time` is required (else `invalid_filter` with
`fields: ["from_time"]`); an existing consumer at position 0 counts from the journal start. The call writes nothing: no consumer row, delivery
or receipt, so no cursor moves and ACK stays ingestion-ordered through `get_updates`/`ack_updates`.

One row per archive chat: `live` and `backfill` counts (backfill and reconcile origins, as
`is_backfill`; a history-only chat has `live: 0` and `newest: null`), `newest` (message, `sent_at`,
sender, sender name and text cut to `body_chars`, default 80; a media-only message shows its file
label such as `[photo.jpg]`) of the newest live message, `owner_replied_after_newest: true` when
an outgoing message in the chat is newer than it, and the `list_chats` read state (`read_state`
observed, stale or unknown; `provider_unread`, `provider_unread_count`, `read_state_observed_at`).
An absent read field is unknown (`absent_read_fields: unknown`); provider mute and archive are not
collected (`provider_mute_archive: unknown`). `accounts` lists each scoped account's source
`status` and `last_ingest_at`, so an empty account can be told apart from a stopped collector.
Rows order newest live first, history-only chats last; `limit` counts chats (default 20, up to
100). A page of up to 25 rows stays within 10,000 characters: when needed, texts shorten to 40
characters, names to 32, texts drop, sender IDs beside a name drop, observation times drop and
names shorten to 16 and drop, then the oldest rows move to the next call (`has_more`), then
`accounts` drops; `shortened_for_size` names the last step. `filters` lists only the names of the
filters that applied. `has_more`, `chats_total`, `live_total` and `backfill_total` cover the whole window, not the
page. Filters: accounts, chats, senders, dates, `exclude_channels`; chats outside the key's scope
are neither counted nor named. A WhatsApp conversation stored under a phone and a LID chat is two
archive chats here (`list_chats` shows it once, ANYM-278). `origins`, `kinds`, `order`,
`start_from`, `reset_filters` and `direction: outgoing` are `invalid_filter`.
Example: `{"consumer": "overview", "view": "by_chat", "from_time": "2026-10-23T06:00:00Z"}`.

For the messages themselves in time order use the default view with `direction: incoming`,
`origins: ["live"]` and `from_time`. `order: sent_at` sorts one delivered page only: read every
page (ACK each after processing), then sort the union by `sent_at`.

## Live Telegram search inside `search` (ANYM-478)

`search` takes `live`: `off` (default), `auto` or `on`. With `off` nothing changes and no provider is
asked; a thin answer (`empty_reason` `history_loading`, `history_not_scanned`, `history_has_gaps` or
`no_matches_in_partial_archive`) only adds `result_evidence.suggested_call`:
`{tool: "messages_search", args: {live: "on"}}` when a Telegram account is in scope, otherwise, for
WhatsApp chats that still load or have gaps, `messages_execute_messenger_operation` with
`request_chat_history` and up to 5 `chat_ids`. Existing `next_action` values are unchanged.

`on` runs a live search for the first page; `auto` only when the archive answer is thin. Per permitted
Telegram account in scope the call queues one `search_messages_live` operation, waits once up to
`wait_s` (0-20 s, default 15) for all of them, then reads the archive again with the key's current
authorization, so hits the collector archived appear as normal rows. `result_evidence.live` lists per
account `state`, `stored_new` and the provider `hits`:

| state | meaning |
|---|---|
| `done` | the live search finished; `hits`, `stored_new`, `next_cursor`, `operation_id` |
| `pending` | not finished within `wait_s`; read it with `get_messenger_operation(operation_id)` |
| `rate_limited` | the account's live search budget; `retry_after` seconds |
| `unavailable` | `reason`: `session_unavailable` (no connected collector), a provider error, or `operation_queue_failed` |
| `not_permitted` | the key lacks `read-live`, the chat or account is outside its scope, or (gateway) the `search_messages_live` action is not granted |
| `not_enabled` | the account has not opted in to `search_messages_live` |
| `unsupported_filter` | `fields` names what live search cannot apply |
| `first_page_only` | `after`/`before` continue the archive only |
| `not_supported_by_provider` | a WhatsApp account whose chats in scope still load; see its `suggested_call` |

Live search applies `query` (`query_mode` `all` or `phrase`), at most one chat (`chat` or `chats` of
length 1), `chat_types` / `include_channels` / `exclude_channels` (channels only when asked for, as in
the archive) and `from_time` / `to_time`. `web` and `exact` modes, `fuzzy`, `sender`/`senders`, `person`,
`direction`, several chats and `chats: []` give `unsupported_filter`. Keep one `request_id` per intent:
the same id maps to the same operation per account, so a repeat after a lost response asks the provider
nothing new. A failure to queue or a provider error never fails or empties the archive answer. When the
archive reread after the wait times out, the call returns the first archive answer with the live entries
and `result_evidence.archive_reread: "failed"` (live hits stored meanwhile appear in the next search). At most 8
calls wait at once (separate from send waits); a busy server returns the operations as `pending` with
`result_evidence.live_wait: "busy"`. `live` other than `off` needs `read-live` and, through the
gateway, the `search_messages_live` action; MCP clients reconnect after the release to see the fields.

## Coverage and diagnostics

Version0.9.2 adds result_evidence, per-chat sync evidence and exact provider ID resolution. Inspect `result_evidence` and call `get_sync_status(chat_id=...)` before interpreting empty or partial results. The archive never substitutes a search error with no matches.

## Query execution and timing

Version0.9.3 separates indexed text matching from chronological page selection.
Rare and absent queries must not walk the entire archive through a date index.
File search first selects candidate IDs from document/passage indexes, then
applies the existing ACL, current-source and best-passage checks. History with
an empty query retains its bounded chronological index path.

Measure database execution, direct REST/MCP HTTP and the full client connector
separately. A fast REST call does not measure connector initialization, retries
or client processing. Global `get_sync_status` and file coverage counts examine
the permitted scope and can take longer than a specific chat read. Prefer
`get_sync_status(chat_id=...)` when investigating one chat; for message/chat
lists, compact output and optional coverage reduce the returned payload.

## Reply queue (0.9.17, schema 27)

`list_chats(awaiting_reply=true)` returns the attention message's time, bounded
snippet, exact archive/provider IDs, current revision, sender ID, name (with
`awaiting_reply_sender_name_basis`; the compact hint carries `sender_id`,
`sender_name` and `sender_name_basis`, never a raw ID as the name), username and
`awaiting_reply_incoming_count` (meaningful incoming messages since the last
meaningful owner message). Empty/service messages and observed self chats are
excluded. Media without a caption remains meaningful. Telegram service account
777000 is excluded. Already archived messages can acquire service/self metadata
through ordinary re-observation; no bulk provider fetch is triggered by a read.
Telegram numeric message IDs resolve same-second ordering; WhatsApp ties have
no reliable comparable counter and are conservatively excluded after an owner
message at the same timestamp. This is still a heuristic, not proof that a
request was answered.

`order='priority'` requires `awaiting_reply=true`. The score is 100 for private
chats or 25 for explicit group attention, +25 for question punctuation, +10 for
an observed provider contact, +40 when a group message that mentions the owner
asks a question and its sender is no observed bot, business/verified sender or
other account of the key, plus waiting points: the square root of waiting hours
(1 hour = 1, 1 day = 4, 4 days = 9), capped at 20 after about 17 days, minus
demotions for chats the owner never wrote in, provider business/verified
senders, the owner's other accounts, closing courtesies and group bots
(weights and rules in [CONVERSATION.md](CONVERSATION.md#queue-priority-components)).
Every private question without a demotion outranks a group hint; a demoted
private hint, or a private statement, may rank below a group question that
mentions the owner. Each row
includes the score, `awaiting_reply_waiting_hours` and reasons; `waiting_age` or
`waiting_age_capped` appear only when waiting points were added, and
`detail=full` adds `awaiting_reply_priority_components` with the points per reason.
This does not infer identity from names or decide whether
a statement semantically requires an answer. Contact flags can be unknown.
Priority cursors retain a scoring timestamp and bind all filters.

Additional query filters: `exclude_chats` (archive chat IDs), `exclude_senders`
(provider sender IDs within selected accounts), `only_questions`, `only_unread`,
`exclude_likely_broadcasts` (opt-in: one-way chats and provider business/verified
senders; `excluded_likely_broadcasts_total`), `promotional` (`exclude` by default: likely promotional
one-way non-contact mailings leave the queue, `excluded_promotional_total`; `include`, `only`; CONVERSATION.md)
and `chat_types=['private']`. Exclusions are per request; a stored mute is
`mute_reply_source` (below). Select accounts when the same provider sender ID is ambiguous.

`unread_count`/`provider_unread_count` is a timestamped chat observation, separate
from recipient delivery/read receipts. `read_state` is observed, stale or unknown;
`read_state_observed_at` and `read_state_basis` explain it. Counts remain visible
when stale; the current boolean and `read_by_owner` then stay unknown. A snapshot
cannot predate the latest incoming message. It is fresh for five minutes, or, since
schema 42 (ANYM-275), for up to 24 hours while continuity is proven: the collector's
receiving period began before the snapshot under the same lease epoch, that lease is
unexpired, the capture heartbeat is under a minute old, the live capture spool is
drained and no update gap was recorded since: a whatsmeow app-state sync error,
disconnect or reconnect, a Telethon auto-reconnect or any (channel) difference fetch,
a failed read-state write, or a
newly quarantined or invalid capture. A restart, disconnect, takeover or expired
lease starts over, and the five-minute rule applies. A later owner read arrives as a passive event and
replaces the snapshot, so a continuously received snapshot stays true.
Telegram's inbox boundary can establish `read_by_owner` for the hinted message;
inbox read updates flow through the existing durable collector. WhatsApp keeps each
passive source apart (`read_state_basis`): the owner's app-state mark-read
(`whatsapp_app_state_read`) is an exact 0 at its action time, a mark-unread only
sets `read_state_marked_unread`; a history-sync `unreadCount`
(`whatsapp_history_sync`) is stamped no later than the conversation's newest
message; an own-device read receipt (`whatsapp_owner_receipt`) is evidence for
exactly the listed messages (resolved through the session's local LID/phone
mapping), so `read_by_owner` is true for the hinted message only when the receipt
names it. A receipt never sets or adjusts a count: one newer than
the count's baseline turns it into null with `read_state_count_basis:
stale_since_baseline`, and receipts alone give `partial`. whatsapp-web.js uses
cached chat counts from catalog scans. There is no extra account session,
provider polling or implicit mark-read action. `only_unread` includes chats with
a fresh positive count or a fresh marked-unread flag; `read_state_unknown_total`
reports excluded unknown/stale candidates. It is not an exhaustive unread inbox.

`dismiss_reply(chat_id, message_id, expected_revision)` requires `read` and
`cursor-write`. It hides the exact current hint for this credential until the
next meaningful incoming head or a changed attention revision. Other keys keep
their own queue. Historical backfill older than the incoming head does not reopen
it. Repeating the same exact dismissal is safe; a changed target returns
`reply_target_changed`, requiring a fresh queue read. Dismissal does not send,
mark source messages read, change archive ACKs or assert the request was fulfilled.

Since schema 41 the queue has stored controls (ANYM-281). None of them touches a
provider, sends or marks read; the write tools need `read` and `cursor-write`,
`list_reply_dismissals` needs `read`, and each call is audited under its own name
without content.

- `dismiss_reply` takes an optional `reason` (`no_reply_needed`, `spam_or_mailing`,
  `handled_elsewhere`, `other`) and `note` (1-200 characters without control
  characters, stored and returned as untrusted text). A bad value returns
  `invalid_filter` and changes nothing. `spam_or_mailing` adds
  `suggest: "mute_reply_source"` to the response.
- `snooze_reply(chat_id, message_id, expected_revision, until, note?)` hides the
  exact hint for this credential until `until` (RFC 3339 with an offset, in the
  future, at most 90 days ahead, else `invalid_date_range`) or the next meaningful
  incoming, whichever comes first. A due snooze returns the row even when the hint
  is older than `awaiting_reply_days`, with `snoozed_until` on the hint and
  `snooze_due` in its reasons. A dismissal or snooze replaces this credential's
  previous one for the chat.
- `restore_reply(chat_id, message_id, expected_revision)` removes this credential's
  dismissal or snooze of the chat when it names that message and revision and
  returns `restored: true` with the removed entry (`kind`, `reason`, `note`,
  `snoozed_until`, `active`); otherwise `restored: false`. The row is then whatever
  the queue computes. Mutes are untouched.
- `mute_reply_source` takes `chat_id`, or `account` plus the hint's exact
  `sender_id`, with optional `reason` and `note`. A chat mute keeps the chat out
  of the queue, including rows created by later messages. A sender mute skips that
  sender's messages within the account when the queue chooses a hint: a private
  chat with only that peer leaves the queue, and a group stays only when another
  participant's owner attention remains, which becomes the hint. Repeating a mute
  updates its reason and note. An agent (principal) holds at most 1,000 mutes
  (`mute_limit_reached`). One mute matches one ID: two accounts, or a WhatsApp
  sender seen as both a phone and a LID identity, need one mute each.
  `unmute_reply_source` with the same target removes it (`unmuted: true|false`).
- `list_reply_dismissals(kind, after, limit)` lists `dismissed` or `snoozed`
  entries of this credential, or `muted` entries of its principal (limit 1-100,
  default 20). Items carry `target`, chat ID and title, sender ID and name,
  `reason`, `note`, `created_at` and `snoozed_until`; dismissals and snoozes add
  `message_id` and `revision` (the arguments of `restore_reply`), the hint's
  snippet and `active` (whether the entry still hides the chat's current hint).
  Only chats the scope can read now are listed; a narrowed scope hides entries
  without deleting them.

While the agent has a mute, `list_chats(awaiting_reply=true)` reports `muted_total`, the
rows mutes removed within the scope and the request's other filters (absent means none). `include_muted=true` returns
them with `awaiting_reply.muted` set to `chat` or `sender`; a sender-muted row
shows the muted sender's hint, which cannot be dismissed or snoozed until it is
unmuted. Dismiss and snooze targets are chosen with the principal's sender mutes
applied, so a hint from a muted sender returns `reply_target_changed`.

Restore and unmute keep no request results. A retry after an uncertain response
restates the same state and also removes an entry for the same hint or target
that another request made in between. After an uncertain result, read
`list_reply_dismissals` instead of retrying.
