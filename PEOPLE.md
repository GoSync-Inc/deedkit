# People search

`search_people(query, accounts, chats, after, limit)` searches by observed name,
username (with or without `@`) and provider sender ID. The search is case-insensitive
and matches a literal fragment; `%` and `_` are not wildcards. For fragments
of three or more characters a PostgreSQL trigram GIN is used. The page limit is 100.

The result contains `account`, `chat`, `sender_id`, `display_name`, `username`,
`sender_name`, `sender_name_basis`, `source_message`, the message date and the
observation time. `display_name` stays this chat's observation; `sender_name` is the
readable label described under Sender names. One sender may
appear in several chats as separate rows. These are provider hints,
not confirmation of identity; identical names/IDs of different accounts are not merged.

Each observation names its chat in `chat_title` (at most 64 characters); `source_sent_at` is the
time of the source message.
For the next step, pass `account` + `sender_id` to `search` (also `chat` if needed),
or `source_message` to `get_context`. `next_cursor` is used
as `after`. Account/chat/read permissions are checked on every call, including the cursor.
A name from a closed chat does not appear in the results of an open one. If the message that was the source
of a name is deleted, the corresponding observation is hidden until the next suitable one.

Telegram uses the cached sender entity from the received message, WhatsApp uses the
name from the message payload, if available. Capture makes no extra
network lookup and does not import the whole address book. Therefore old records without
this metadata, unknown usernames or unavailable contacts cannot be found by
name; use the exact sender ID in `search`. Checking the completeness of the catalog on
working accounts remains part of live acceptance.

## Sender names

Messages (`search`, `get_messages`, `get_context`, `get_updates`), the conversation
view, people rows and the reply-queue hint name a sender in `sender_name` and say
where the name came from in `sender_name_basis`. The raw provider ID is always in
`sender` (messages) or `sender_id` (people rows and the queue hint), never in
`sender_name`. A stored name that, without surrounding whitespace, equals the
sender ID or spells its digits as an ID (bare, `+digits`, with any device suffix or
`@` domain) counts as no name on every platform. For a WhatsApp account the first
match wins:

1. `message`: the name observed for this sender in the message's own chat;
2. `seen_elsewhere`: the newest name of the same sender (device suffix ignored) in
   another chat of the same account whose source message the caller can read;
3. `linked_identity`: the same for the linked phone-number/LID identity, only when
   the link's source message is visible and the link has no conflict anywhere in
   the account ([WHATSAPP_IDENTITIES.md](WHATSAPP_IDENTITIES.md));
4. `phone_number`: `+<digits>` for a phone-number sender (`@c.us`); a LID is never
   shown as a phone number;
5. otherwise `sender_name` is null.

Telegram keeps its cached entity names; only the raw-ID rule applies. A name from a
chat the key cannot read is never used. Compact message rows and the compact queue
hint show `sender_name_basis` only when it is not `message`; the queue hint keeps showing a
Telegram username in `sender_name` when there is no name. These labels are
reading aids, not identity proof.

A name update does not create a new revision of the message text. An old backfill,
a conflicting event or a deletion does not replace the latest name observation.
The operation reads the archive without provider read receipts and without advancing the agent ACK.
