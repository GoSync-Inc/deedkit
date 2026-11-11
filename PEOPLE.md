# People search

`search_people(query, accounts, account, chats, after, limit)` searches by observed name,
username (with or without `@`) and provider sender ID. The search is case-insensitive
and matches a literal fragment; `%` and `_` are not wildcards. For fragments
of three or more characters a PostgreSQL trigram GIN is used. The query is normalized
(Unicode NFC, trimmed, inner spaces collapsed, lowercase, leading `@` removed) and "literal" means
literal after lowercase and the ё/е fold: a query of three or more characters reads ё and е as the same
letter in both directions ("Федор" finds "Фёдор" and the reverse). A row found only through the fold
reports `match_type: literal_folded` and sorts after the literal rows. The fold does not touch the
Latin transliteration path (`fedor` does not find `fyodor`). The page limit is 100.

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

### The Telegram contact book (ANYM-673)

An empty `search_people` result carries `see_also: {tool: "list_contacts", argument: "query", accounts: [...]}`
when no row came from a Telegram account the key may read contacts for. The accounts are the Telegram accounts
where the key holds `contacts-read` (and is a full-account key) and the owner enabled `list_contacts`; the
field is absent when there are none. Follow it with the messenger operation `list_contacts` and `parameters.query`
(1-64 characters): it filters the account's contacts live by display name and username only, no phone number;
ё and е are one letter and a Latin spelling finds the Cyrillic name (`fedor` finds Федор; Фёдор is `fyodor`).
`limit`/`offset` page the filtered list. Each call reads the whole book once per account, so for many names page
the full list once without `query`. Nothing is stored; the contact book is not merged into the archive results.

### Birthdays of active contacts (ANYM-919, schema 86)

DeedKit keeps the Telegram birthday of the people the owner actually talks to, never of the whole contact book, and
writes no calendar (owner 2026-11-09). One deterministic archive rule decides who is **active** for an account
(`messaging_service/birthdays.py`): a Telegram user is active when, in the private chat with that user
(`chats.chat_type='private'`, the chat's provider ID is the user ID), the archive holds at least 3 outgoing and at
least 1 incoming message sent in the last 90 days, not deleted. The self chat, Telegram's service chat (777000) and a
chat whose incoming message in that window Telegram marked as sent by a bot are never active, nor is a chat the
collection policy excludes.

The Telegram collector keeps `contact_birthdays` to exactly the active users. About every 10 minutes it deletes the
row of anyone no longer active, reads `contacts.getBirthdays` (Telegram answers for birthdays within a day of today)
at most every 6 hours, and reads `users.getFullUser` for each active user whose birthday is unread or older than 7
days, at most 30 users per hour per account, through the account's request governor (background lane; a FloodWait
stops the pass until the wait ends). A user who does not share a birthday, or whom Telegram refuses, is stored
`hidden`. `ANYMSG_TELEGRAM_BIRTHDAYS=0` stops the reads. WhatsApp has no birthday field and is not covered.

Read it with the messenger operation `list_birthdays` (`contacts-read`, Telegram, tool `messages_read_contacts`;
`days_ahead` 0-31, default 14). It answers from the archive without a provider request: for each active contact
with a known birthday within `days_ahead` days of today in the owner's zone (`ANYMSG_OWNER_TIMEZONE`, named in
`time_zone`), `name`, `chat_id`, `day`, `month`, `year` (null when not shared), `date`, `days_until` and
`age_turning` when the year is known, sorted by date, at most 100 (`truncated`). 29 February falls on 28 February
in a common year. Hidden and not yet read birthdays are only counted in `not_shared`, without names. A chat-scoped
key sees only the contacts of its own chats: another contact is neither listed nor counted. To be woken the day
before, subscribe to `contact.birthday` (SUBSCRIPTIONS.md). An agent may propose a calendar entry only when the
owner asks, under the calendar's own confirmation; a greeting is a send with its own confirmation.

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
python -m messaging_service set-person-channel --person <id> --purpose message|call (--account <account> | --clear)
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

Preferred channel (ANYM-548, schema 53): `set-person-channel` stores one account per
person and purpose (`message`, `call`); the account must be one the person has a link on
(`account_not_linked` otherwise). `--clear` removes it. `list-people` prints
`preferences`. Unlinking the person's last identity on that account, or `delete-person`,
removes the preference with it. Like the links, no agent key can set it.

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

## One target for any identifier (ANYM-545)

`platform_resolve_target(to, purpose='message'|'call', account?, sender?)` (MCP and REST
`resolve_target`, `read` permission) turns an identifier into one exact target or a list
to choose from. It is read-only: no provider call, no write. The audit log records it as
`resolve_reference`.

- Exact forms: a `chat_` ID; a `msg_` ID (its chat; with `sender=true` the sender's
  private chat; a shared contact card resolves to the card's phone or Telegram user); a
  person UUID; a WhatsApp JID; a phone in any format (`+7 911 …`, `00…`); `@username`.
  The answer is `{state: succeeded, target: {account, chat_id, network, label,
  how_resolved, person?, phone?, telegram_user_id?}}`; `how_resolved` names the rule (`chat_id`,
  `message_chat`, `message_sender`, `person`, `person_preferred`, `jid`, `phone`, `username`,
  `contact_card_phone`, `contact_card_telegram_user`). With `purpose: call` a private
  Telegram chat also carries `telegram_user_id`, the user ID a call dials, and a person
  UUID returns only `{account, network, label, how_resolved, phone | telegram_user_id}`:
  no `chat_id` or `person` (ANYM-548; `calls_call` asks this with the caller's own key).
- A phone with no chat on exactly one native WhatsApp account that has the
  `open_chat_by_phone` opt-in and a key with `send` (SENDING.md) gives `chat_id: null,
  opens_chat: true`; send with `phone`. Two such accounts give `selection_required` with
  one candidate per account and no `next`: repeat the call with `account`. This answer reads
  the account's discovery setting, not exclusions of the number's other IDs the key may not
  see, so the send can still refuse it (`chat_not_collected`). An account's own
  phone-number chat wins over its LID alias chat.
- A name or fragment never resolves by itself, even with one match: `selection_required`
  with candidates (chats), each with its own `next` (this tool with the exact ID). The
  same holds for a person with several private chats and no usable preference, and a
  username shared by several identities. A person whose owner-set preferred account for
  the purpose has exactly one private chat the key can read resolves to it
  (`how_resolved: person_preferred`); a preference on an account the key cannot read
  changes nothing. At most 10 candidates, newest `last_activity` first; with more, `more: true`
  and `next` is `messages_search_people`. A candidate label with seven or more digits
  (any separators) becomes `<network> <kind>`. `account` narrows every form, a contact
  card's phone included.
- `purpose='call'` refuses group and channel chats (`unsupported_target`).
- Scope: every read is the key's own projection (accounts, chats, visible members), as in
  the section above. A hidden chat, identity or person is the same `not_found` as a
  missing one; `not_found` carries a read-only `next` (`messages_search_people`). A
  contact card with neither a usable phone nor a Telegram user ID is `not_found` with
  `hint: contact_card_fields_missing`.

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

Telegram keeps its cached entity names; only the raw-ID rule applies, except on the
`get_context` anchor (ANYM-831): an incoming Telegram anchor with no name and no username
in its own chat takes the newest usable name of the same sender ID in another chat of the
same account (else the newest username), counted only through an incoming, undeleted source
message the caller can read, with `sender_name_basis: seen_elsewhere` and
`sender_name_observed_on` (the source message's UTC date). Telegram names belong to the
user, not the chat, but this is still an observation, not verified identity; the owner's
other Telegram accounts are not searched (`search_people` may find more). Search rows,
`get_messages` and context rows keep the same-chat rule. An incoming anchor still without a
name or username carries `sender_name_unavailable: "not_observed"` (no readable incoming
message of this account named this sender; it never means "not a contact" or "deleted"),
except a channel post or an anonymous group admin, whose chat is the author. A name from a
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
