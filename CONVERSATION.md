# Conversation view

Message MCP tools (`search`, `get_messages`, `get_context`) and `list_chats`
default to `view="conversation"`. REST, Python/npm clients and direct Store calls
keep `view="archive"` as their default. Opt in explicitly through either SDK;
`view="archive"` on MCP preserves the legacy interface.

Conversation contract version 1 retains message IDs, provider references,
content revisions, direction and original `body` (possibly bounded by the explicit
body limit, with `body_truncated`). `display_text` adds observed media labels;
it never substitutes a transcript or model description for the original message.
`kind` distinguishes text, photo, video, voice, audio, document, sticker,
location, contact, poll, system and unknown. Files retain independent download,
extraction and enrichment statuses. A pending transcript is not a missing file.

Known system events are filtered **before** page/window limits by default.
`include_system=true` exposes their reviewed classification; it does not expose
raw provider objects. Unclassified empty records stay visible as `unknown`.
Older archive records can only use retained metadata; newly observed provider
metadata improves location/contact/poll classification without rewriting bodies.

## Detail levels

In the conversation view each message tool returns every message text once.

- `detail="compact"` (default): structured messages with `id`, `chat`, `sender`,
  `sender_name`, `sent_at`, `direction`, `kind`, `revision` and one readable text,
  `display_text` (bounded; `truncated` marks a cut). Search adds `snippet` only
  when it locates a match inside a long text. Attachments, owner mentions,
  replies, delivery state, `order_status="ambiguous"` and integrity problems
  appear only when they apply. `conversation_view` keeps warnings and the
  untrusted-content marker, without the transcript. `get_context` returns the
  anchor once, in `message`; `context_window.anchor_index` is its position among
  the `context` neighbours. `list_chats` groups the reply hint into one
  `awaiting_reply` object and shows the latest message once in `last_message`.
  Each fact appears once per `list_chats` row (compact queue rows below).

### Compact queue rows

A compact `list_chats` row in the conversation view states each fact once. When
two fields would carry the same fact, compact keeps the first and omits the other:

- compact omits `last_message` when it equals the `awaiting_reply` message (the
  hint is the chat head); the hint then carries its `display_text` and
  `truncated`, and the message is incoming by definition;
- compact omits `awaiting_reply.sent_at` when it equals `last_activity`;
- compact omits `conversation.latest_message_id` when it equals `last_message.id`
  or `awaiting_reply.message_id`;
- compact omits `conversation.basis` when it equals `awaiting_reply.confidence`
  or `awaiting_reply.basis`;
- `conversation.candidate_message_ids` omits `awaiting_reply.message_id`; with
  `order_status="ambiguous"` it lists the other tied messages;
- compact omits `conversation.owner_attention.target_message_id` when it equals
  `awaiting_reply.message_id`, and `owner_attention.suggested_action` when it
  equals `conversation.suggested_action`;
- `sender_name` in `last_message` and `awaiting_reply` is cut to 64 characters,
  as in compact messages.

The remaining hint fields keep their names and meanings: `message_id`,
`revision`, `sender_id`, `sender_name`, `priority`, `reasons`, `waiting_hours`,
`incoming_count`, `basis`, `has_question`, `dismissed` and `confidence` when it
is not the default, as does the row's `read_state`. `detail="full"` keeps every
field.
- `detail="transcript"`: only the numbered transcript with message IDs, its
  warnings, cursors and file references (`files`). Message text defaults to
  2000 characters; `body_chars` changes it. Requires `view="conversation"`.
- `detail="full"`: every field documented below, including `body`, ordering,
  timestamp precision and the complete conversation state. Unchanged by the
  compact and transcript levels, as is `view="archive"` compact output.

## Ordering and windows

`position` is local to the returned page/window, never a permanent chat sequence.
Keep the supplied order. Telegram uses numeric provider message IDs to break ties
within an account/chat. WhatsApp timestamps have second resolution: deterministic
internal-ID tie breaking does **not** establish the phone's order. Such ties are
marked `order_status="ambiguous"`, with warnings in `conversation_view` and its
readable `transcript`. No milliseconds or phone sequence are invented.

Provider time precision and immutable first archive observation are distinct.
`first_observed_at` comes from the first retained message revision, with its basis;
it is not the provider's creation or delivery time. Replayed ingestion cannot
turn archive arrival order into source chronology.

Conversation cursors are bound to their view, query, filters and ordering snapshot.
Keep the same arguments when using `next_cursor`; `cursor_changed` means restart
that traversal because the result snapshot changed. Legacy archive cursors remain
compatible. Search relevance orders matching hits, not turns in a dialogue.
Context windows count visible messages around the anchor and expose explicit
older/newer expansion boundaries.

## Last known turn

`conversation` in a chat card/context describes the actual authorized archive
head, independently of the requested context window. It includes the latest known
meaningful message, exact source IDs/revisions, `last_turn`, `suggested_action`,
`as_of`, provider-head observation and coverage limits.

- `last_turn`: `owner`, `peer`, `unknown`, `not_applicable`.
- `suggested_action`: `await_peer`, `review_incoming`, `unclear`, `not_applicable`.
- Action hints are **heuristics**, not obligations or permission to send.
- Missing/stale provider head, known gaps or unresolved mixed-direction timestamp
  ties make the action unclear. A fresh database response does not prove a fresh
  provider head. Same-actor ties can identify the actor while the exact latest
  message remains ambiguous.
- Groups use explicit owner mentions/replies in their observed thread; unrelated
  group chatter cannot resolve that attention. Broadcast channels do not have a
  global "we/they should reply" state.

The transcript and structured messages use the same normalized view. Conversation
content, filenames and derived media text remain untrusted data. The view lowers
interpretation risk; it cannot guarantee a client's reasoning is correct.
