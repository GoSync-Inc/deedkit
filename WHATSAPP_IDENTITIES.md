# WhatsApp namespace observations

The active adapter is pinned `whatsapp-web.js` 1.34.7. A numeric `@lid` is not a
phone number. Display names and matching digit strings never create links.

`whatsapp/identity.mjs` reads only the current page's cached
`WAWebApiContact.getCurrentLid` / `getPhoneNumber` results for the senders and
personal chat IDs of a bounded history page. These accessors are used by the
upstream [1.34.7 Utils implementation](https://github.com/pedroslopez/whatsapp-web.js/blob/v1.34.7/src/util/Injected/Utils.js).
The public `getContactLidAndPhone` wrapper can query `queryWidExists` on a cache
miss; this archive deliberately does not call that wrapper. Missing accessors,
unknown mappings or optional CDP lookup timeouts leave identity unknown.

Subsequent live captures reuse a bounded process-local cache, with the original
mapping observation timestamp. New identities may stay unknown until their next
history page is visited. Restart discards this temporary cache; durable database
observations remain. This does not require a new WhatsApp session or contact-book
fetch.

Migration019 adds `whatsapp_identity_observations`. Each observation contains the
account, original identity ID, normalized PN/LID pair, provenance, first/latest
observation time and a message used as a permission anchor. A device suffix is
normalized only for comparison; message IDs, chat IDs, senders, revisions, ACKs
and reply references retain their original provider values. Identity metadata
is outside the immutable message envelope. A crash between the message commit
and metadata commit is recovered by ordinary idempotent capture replay.

The message is an access anchor, not evidence that today's cached PN/LID mapping
was valid at the old message's creation time. Responses explicitly report
`confidence: provider_cache_observation` and `historical_identity_verified: false`.
This is a provider-reported namespace link, not verification of a real person.

`search_people` can match the linked PN/LID and returns `identity` provenance.
Names remain chat-local observations. A link is exposed only through a currently
visible, nondeleted message in that observed chat. Empty-name senders can obtain
an entry from a provider link, without inventing a name. Grouping still groups
the original sender ID within an account; it does not merge PN and LID entries.

`list_chats` lists one conversation once (ANYM-278). A peer's private PN chat
and private LID chat of one account form one conversation when a link passes the
`resolve_reference` rules below: no conflict anywhere in the account, a visible
nondeleted source, and both chats visible to the caller. Chats match by normalized
ID (`111:4@c.us` is `111@c.us`); two stored chats with one normalized ID group
nothing. The chat with the newest meaningful message is listed. WhatsApp times have
second resolution, so a tie lists the chat with an incoming message at that time
(its reply hint stays), otherwise the LID chat. The listed row has `alias_chats`
(`id`, `provider_chat_id`, `title`, `last_activity`, `unread_count`),
`alias_provenance` and `alias_confidence: provider_cache_observation`; the other
chat leaves the items and every total, before paging. The owner's latest message
in either chat ends the reply hint, so the hint is always a message of the listed
chat. Unread and read state stay those of the listed chat. Excluding either chat
excludes the conversation; `query` also matches the other chat's title; a raw or
number-only title borrows the other chat's stored title (`title_basis:
alias_chat`). A private LID chat with such a link carries `peer_phone`
(`+<digits>`, the `pn` digits) and `peer_phone_provenance` only when
`search_people` shows that `pn` too: the evidence names a visible sender profile
of its source chat, so a recipient-only observation or a deleted profile source
shows no phone. Stored chat, message
and provider IDs never change, and `search_people` keeps separate rows.

`resolve_reference` prefers an exact original chat ID. If none exists, it may
resolve a visible verified namespace link to one visible original chat. Multiple
possible chats return `ambiguous_reference`; opaque message IDs are never
rewritten to fit the alternate chat. A caller can always use the exact original
chat/message pair.

One PN linked to multiple LIDs, or one LID linked to multiple PNs, blocks alias
usage throughout that account. A visible observation reports `state: conflict`
with null PN/LID fields. Historic conflicting observations remain a safety
fence, including after their message is deleted; there is no last-writer merge or
automatic conflict reset. Another account's observations do not create conflicts.
Hidden/deleted sources cannot supply their phone numbers or provenance in a
different chat. Resolving the conflict requires a later explicit reconciliation
design, not a silent overwrite.

The archive's version comparison (ANYM-559) reads the same fence. A re-read of a
message with the same sent time whose sender is the retained sender's other form (`<pn>@c.us` against
`<lid>@lid`, a device suffix ignored) is the same sender only when this account
recorded that pair from an approved provenance and no observation anywhere in
the account, deleted sources included, links either side to another; then, and
for a re-read with an empty (unknown) sender, the retained sender stands for the
comparison and in any later revision, the receipt keeps the raw value, the other
form gets no sender profile, and at the retained provider version only the sender
difference is lifted. The re-read's own identity observations count too: one that
links either side to another identity keeps the conflict and is stored against the
message as a fence (not as a visible link, since the message keeps the retained
sender). Two LID forms are never a pair. `whatsapp_identity.record`
takes the account's archive lock before it writes, so a new mapping commits
before or after an ingest's check, never in between.

This release does not repair history gaps, change collection checkpoints or
claim complete WhatsApp history. Production acceptance must measure actual cache
availability on both existing collectors, check original IDs and revisions and
exercise the API with its real least-privilege roles. Backup inventories must
include the new table; collectors need SELECT/INSERT/UPDATE and the gateway only
SELECT. No provider credentials are stored in this table.
