# Archive search and reading

AnyMsg stores the messages and files of registered accounts and answers agents.
Telegram and WhatsApp are supported; the existence of an adapter does not mean an account
is connected or its history is complete. Coverage states are returned in the responses.

`search` accepts `accounts`, `chats`, `senders` — lists of up to 100 exact IDs.
Inside a list "or" applies; between lists and the earlier single filters —
"and". All conditions are intersected with the key's permissions. `null`/absence does not limit
the dimension, `[]` returns an empty result. A foreign ID in a list does not widen permissions.
A sender ID belongs to the source/account; a match of strings does not merge people.

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
order in [PEOPLE.md](PEOPLE.md#sender-names)), username, chat title/type,
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
an observed provider contact, plus waiting points: the square root of waiting hours
(1 hour = 1, 1 day = 4, 4 days = 9), capped at 20 after about 17 days. Each row
includes the score, `awaiting_reply_waiting_hours` and reasons; `waiting_age` or
`waiting_age_capped` appear only when waiting points were added. This does not infer identity from names or decide whether
a statement semantically requires an answer. Contact flags can be unknown.
Priority cursors retain a scoring timestamp and bind all filters.

Additional query filters: `exclude_chats` (archive chat IDs), `exclude_senders`
(provider sender IDs within selected accounts), `only_questions`, `only_unread`,
and `chat_types=['private']`. Exclusions are per request, not a stored global
mute policy. Select accounts when the same provider sender ID is ambiguous.

`unread_count`/`provider_unread_count` is a timestamped chat observation, separate
from recipient delivery/read receipts. `read_state` is observed, stale or unknown;
`read_state_observed_at` and `read_state_basis` explain it. Counts remain visible
when stale; the current boolean and `read_by_owner` then stay unknown. A snapshot
is fresh for at most five minutes and cannot predate the latest incoming message.
Telegram's inbox boundary can establish `read_by_owner` for the hinted message;
inbox read updates flow through the existing durable collector. WhatsApp uses
cached chat counts from catalog scans; it cannot establish exact per-message
owner reads, so `read_by_owner` stays unknown. There is no extra account session,
provider polling or implicit mark-read action. `only_unread` includes chats with
a fresh positive count; `read_state_unknown_total` reports excluded unknown/stale
candidates. It is not an exhaustive unread inbox.

`dismiss_reply(chat_id, message_id, expected_revision)` requires `read` and
`cursor-write`. It hides the exact current hint for this credential until the
next meaningful incoming head or a changed attention revision. Other keys keep
their own queue. Historical backfill older than the incoming head does not reopen
it. Repeating the same exact dismissal is safe; a changed target returns
`reply_target_changed`, requiring a fresh queue read. Dismissal does not send,
mark source messages read, change archive ACKs or assert the request was fulfilled.
