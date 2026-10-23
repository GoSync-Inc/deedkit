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

## Linked people (ANYM-283, schema 43)

The owner may state that identities in different messengers are one person. Links are
made by hand only: DeedKit never merges identities by name, spelling or phone number.
Each link is an exact `(account, sender)` pair; source messages, names and profiles stay
as they are.

Owner commands (operator CLI, owner database connection; no MCP tool, REST route,
operation permission or gateway action can change links):

```sh
python -m messaging_service link-person --account <account> --sender <sender_id> [--person <id>]
python -m messaging_service unlink-person --account <account> --sender <sender_id>
python -m messaging_service delete-person --person <id>
python -m messaging_service list-people [--person <id>]
```

Take `--account` and `--sender` from a `search_people` row (`account`, `sender_id`) read
with the owner's key. Without `--person` a new person is created and its id printed;
with `--person` the identity joins that person. Repeating the same link is a no-op; an
identity already linked to another person is refused (`identity_already_linked`), so
take it off that person first. A WhatsApp contact may appear under a phone (PN) and a
LID sender: these are two identities. `link-person` and `list-people` print
`whatsapp_siblings`, the other half of a provider-observed PN/LID pair; link it too, or
`search(person)` returns half the WhatsApp side. `delete-person` removes only the links.
Link and removal commands of one person are serialized, so a delete never leaves a
member that was linked at the same moment.

Reading: a `search_people` row of a linked identity carries `person`, the person's id.
`search_people(person=<id>)` lists the name observations of that person's identities;
an identity with no observed name is not listed there but `search(person=<id>)` still
finds its messages. `search(person=<id>)` needs no text query and matches messages sent
*by* the person's identities; the owner's own messages in a chat with them are found
through that chat. Both tools AND `person` with their other filters.

Visibility: a key sees a person only through members it can read now (account, chat
and a non-deleted message). Hidden members, their number and the fact that they exist
are never shown. A person with no visible member, or an unknown id, is `not_found`; this
holds mid-pagination too, when the last visible member is removed or hidden. A change
of the visible members during pagination returns `cursor_changed`. A key that sees one
member still sees that member's `person` id; this reveals only that a link exists, not
the other members. `person` is archive only (see `live` in SEARCH.md).

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

## Chat titles

A private chat whose stored title is empty or equals its provider chat ID gets a readable
title on `list_chats` (`title`, with `title_basis`), on the `resolve_reference` chat and as
`chat_title` of every message, people and file row. The peer is the chat's provider ID
(device suffix ignored) and, as a second key, its latest incoming sender. The first match wins:

1. `latest_incoming_sender_name`: the newest incoming name stored for this chat, unless it
   is raw by the rule above;
2. `seen_elsewhere`, 3. `linked_identity`: as under Sender names, from another chat of the
   same account the caller can read; only names seen on incoming messages count;
4. `phone_number`: `+<digits>` for a `@c.us` chat;
5. otherwise the stored title stays; a `@lid` chat is never shown as a phone number.

A self chat is `Saved messages` (`self_chat`). A chat with a provider title keeps it and has
no `title_basis`. Message, people and file rows carry the same `chat_title` and no basis.
Title `query` matching still uses the stored title (and, for a raw-titled private chat, the
incoming name stored for that chat); fallback names are not search aliases. Titles are
reading aids, not identity proof.

A name update does not create a new revision of the message text. An old backfill,
a conflicting event or a deletion does not replace the latest name observation.
The operation reads the archive without provider read receipts and without advancing the agent ACK.
