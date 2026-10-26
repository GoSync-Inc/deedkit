# Messenger capability audit

Current extension status and priority: [BACKLOG.md](BACKLOG.md), DK-22–DK-28.
This dated audit preserves capability/design evidence, not a separate work queue;
the current account/provider manifest determines actual support.

Source audit: 2026-10-14. Dependencies: **Telethon 1.45.0** and
**whatsapp-web.js 1.34.7**, as pinned in the lock files. This describes the current
source candidate, not deployment or successful live use of every operation.
The running server's catalog and capabilities remain authoritative.

The candidate catalog has **48 named operations**, reached through four generic
tools; the expanded MCP manifest has **24 tools**. Platform support is conditional:
41 catalog rows have a Telegram adapter and 45 have a WhatsApp adapter. A row does
not guarantee native rights, availability for every chat/account type, or a
confirmed provider effect. This is not feature parity with either upstream library.

## Implemented scope

| Capability | Telegram candidate | WhatsApp candidate |
| --- | --- | --- |
| Archived messages and files | Existing PostgreSQL search, history/context, people, direct references, files/segments/occurrences, updates and revision evidence | Same archive interface; linked-device history can be incomplete |
| Plain/rich text and preview | Existing `preview_message` / `send_message`, native entities | Existing preview/send, supported WhatsApp wire formatting and conversion warnings |
| Live chat/message information | Chat information, reactions, message information including observed poll, album and topic references | Chat information, reactions, own-message delivery/read/played information, pinned-message listing |
| Media and other sends | Registered original up to 32 MiB; document/photo/video/audio/voice/video-note, forward, poll, contact card, location | Registered original up to 32 MiB; document/photo/video/audio/voice/sticker, forward, poll, contact card, location |
| Existing-message actions | Own plain edit/global revoke, emoji reaction/removal, poll voting, message pin/unpin | Own plain edit, reaction/removal, pin/unpin and star/unstar; delete is deliberately unsupported |
| Explicit read/chat state | Read up to selected message, mark unread, archive/unarchive, mute/unmute | Whole-chat read, mark unread, archive/unarchive, mute/unmute, chat pin/unpin and Business labels |
| Group administration | Participants, first topic page, basic-group creation, title/about, add, basic-group remove, conservative channel/supergroup promote/demote, leave, existing invite/revoke | Participants, create/title/about, add/remove/promote/demote, leave, invite/export/revoke |
| Contacts and own profile | Contacts/blocked lists and individual contact, import/delete/block/unblock, own name/bio read/update | Contacts/blocked lists and individual contact, address-book save/delete/block/unblock, cached own profile and display-name/status update |

See the exact parameters, permission families and receipt contract in
[MESSENGER_OPERATIONS.md](MESSENGER_OPERATIONS.md) and the executable catalog in
[messenger_operations.py](messaging_service/messenger_operations.py).
Uploads accept registered originals, not arbitrary paths, URLs or raw bytes.
Forwards stay within one account. Telegram removal from a supergroup/channel is
not implemented: a kick/unban sequence could partially apply or become a ban.
Telegram topic listing currently returns one provider page; it is not complete
topic enumeration. WhatsApp labels require the corresponding Business features.
Provider responses without effect evidence remain `submitted`/`unknown`.

## Archive observations are not live metadata

The existing passive archive captures text, available formatting evidence,
original-file descriptors, sender names/usernames and bounded reply/mention/
forward/media-kind observations. Ordinary archive reads do not query contacts,
refresh group membership or change messenger read state.

The new live operations query the existing session owner under separately granted
permissions. They do **not** imply that reaction actors/counts, poll options/votes,
album/topic IDs, location contents, current group roles or profile
changes have been continuously captured and indexed in the archive. These need
their own bounded schemas, observation time, revision/coverage rules and refresh
jobs. In particular, a live Telegram poll/topic result is not yet a searchable,
complete passive poll/topic archive. Shared contact cards (names, phones,
WhatsApp/Telegram IDs) are captured as a bounded block when
`ANYMSG_CAPTURE_CONTACT_CARDS=1` ([SEARCH.md](SEARCH.md#shared-contact-cards)).

## Capability-extension design reference

Each increment must extend the allowlist and permission model explicitly. It must
not grant existing keys new rights. Mutations need a concrete owner-confirmed
preview; partial changes and uncertain results must not trigger automatic retries.

| Increment | Confirmed upstream capability; current gap | Intended boundary |
| --- | --- | --- |
| 1. Persist richer read evidence | Telegram exposes poll/album/topic/reaction metadata; WhatsApp exposes quoted messages, reactions and poll votes. Add passive observations plus bounded refresh, rather than assuming live queries populate the archive. [Telethon messages](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.messages.MessageMethods), [WhatsApp Message](https://docs.wwebjs.dev/Message.html), [WhatsApp poll votes](https://docs.wwebjs.dev/Client.html#getPollVotes) | Archive read remains passive; separately permitted live refresh; label unknown/stale/partial data |
| 2. Profile pictures and contact details | Telegram user/group photo methods and username updates; WhatsApp contact about/profile-picture/status-broadcast reads and own/group picture changes. The current WhatsApp profile `status` field is About text, not a Status story. [Telegram photos](https://core.telegram.org/method/photos.uploadProfilePhoto), [Telegram username](https://core.telegram.org/method/account.updateUsername), [WhatsApp Contact](https://docs.wwebjs.dev/Contact.html), [WhatsApp own picture](https://docs.wwebjs.dev/Client.html#setProfilePicture), [WhatsApp group picture](https://docs.wwebjs.dev/GroupChat.html#setPicture) | Full-account rights for own profile/contact book; scoped group rights; bounded registered image inputs; no raw URL fetching; separate story audience/read effects |
| 3. Group settings and admission | Telegram default/individual restrictions and slow mode; WhatsApp admins-only messaging/settings/additions, membership requests and approve/reject. Native WhatsApp creation options also include membership approval; this is not a claim of an existing-group toggle method. These settings and workflows are absent from the current catalog. [Telegram rights](https://core.telegram.org/api/rights), [Telegram slow mode](https://core.telegram.org/method/channels.toggleSlowMode), [WhatsApp GroupChat](https://docs.wwebjs.dev/GroupChat.html), [WhatsApp creation options](https://docs.wwebjs.dev/Client.html#createGroup) | Separate explicit settings/member effects; no owner transfer or unrestricted native administrator-rights payload; never send implicit private invitations |
| 4. Join, invitations, channels and topics | Telegram joining/importing invites, supergroup/broadcast creation, invitation limits/approval and topic create/edit/close/delete; WhatsApp invite joining, channel creation/subscription and related channel APIs. Current group creation and topic listing cover only a subset. [Telegram invites](https://core.telegram.org/api/invites), [Telegram channel creation](https://core.telegram.org/method/channels.createChannel), [Telegram forums](https://core.telegram.org/api/forum), [WhatsApp invite join](https://docs.wwebjs.dev/Client.html#acceptInvite), [WhatsApp channels](https://docs.wwebjs.dev/Client.html#createChannel) | New destinations require explicit operator scope decisions; previews show invitation/administrative targets; do not expose newly joined content through old chat-scoped keys |
| 5. Media/message completeness and privacy | Telegram supports albums, scheduling and richer edit/media parameters; WhatsApp exposes native poll-vote reads and other message variants. Add reviewed variants rather than raw option objects. Telegram privacy read/write methods also exist but are not exposed; the pinned WhatsApp public client does not offer equivalent general privacy management. [Telethon uploads](https://docs.telethon.dev/en/stable/modules/client.html#telethon.client.uploads.UploadMethods), [Telegram privacy](https://core.telegram.org/method/account.setPrivacy), [WhatsApp sends](https://docs.wwebjs.dev/Client.html#sendMessage) | Fresh per-key grants for account privacy; validated high-level choices; media resource budgets; no paid/custom-emoji transaction passthrough |

Upstream signatures were checked against the pinned installed Telethon package
and the pinned WhatsApp npm source, with primary documentation linked above.
Upstream support alone is not a completed AnyMsg adapter or a production test.
Business-only, channel, forum and administrator features require separate native
acceptance tests with explicitly authorized targets.

## Outside this agent interface

Authentication/pairing/2FA, credentials or session export, logout, account deletion,
payments/purchases/paid reactions, ownership transfer, voice/call control and raw
TL/browser/RPC execution are outside this catalog. They must not be added by
reflecting all native methods into MCP. Pairing remains an operator action; any
separate calling service has its own scope and does not acquire authority from
messenger-operation keys.
