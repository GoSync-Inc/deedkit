# Passive delivery and read observations

Version 0.9.13 adds provider receipt evidence without sending a message, opening
another session, marking chats read or acknowledging an agent's archive cursor.
Outbox `status: sent` continues to mean provider acceptance. The independent
`delivery_receipt` is available in send status, outgoing history, search, context
and updates, including compact responses. Missing evidence is `state: unknown`.

## Evidence contract

| Field | Meaning |
| --- | --- |
| `state` | Strongest observation: unknown, pending, error, server, delivered, read or played; not_applicable for a self chat |
| `basis` | Telegram outgoing-read boundary or WhatsApp message ACK |
| `audience` | Private peer or provider aggregate for a group |
| `observed_at` | When the selected proof of this state was observed by the collector |
| `provider_at` | Exact provider event time; null for the current adapters |
| `delivered_at`, `read_at`, `played_at` | Exact provider times; currently null |
| `delivered_observed_at`, `read_observed_at`, `played_observed_at` | Separately observed stages; missing stages stay null |
| `recipient_delivery_confirmed` | True only for delivery/read/played proof for a private peer |

A message in the owner's own chat (WhatsApp note to self, Telegram Saved
Messages) has no recipient. Its evidence is `state: not_applicable`,
`basis: self_chat`, `audience: self`, with any raw device ACK kept as
`provider_state`, and it publishes no `message.status` event. The self chat is
known from the captured message or the dialog observation, including a WhatsApp
chat addressed by the owner's LID.

A read observation implies arrival without inventing a delivery timestamp.
Pending/error/server alone do not confirm recipient delivery. Weaker or duplicate
ACKs cannot downgrade stronger evidence. Group aggregates never assert that a
named participant or every group member read a message. LID and phone identities
remain distinct unless the existing identity subsystem has verified their link.

Telegram captures only verified private `UpdateReadHistoryOutbox` boundaries.
The [provider update](https://core.telegram.org/constructor/updateReadHistoryOutbox)
contains a maximum outgoing message ID, with no exact read or device-delivery
time. Groups, channels, inbox read updates and Saved Messages are excluded.
WhatsApp listens to the existing client's
[`message_ack`](https://docs.wwebjs.dev/Client.html#event:message_ack) events;
it does not poll message info or open a second browser.

## What a `sent` result tells the agent (ANYM-659)

A send in status `sent` carries two additive fields, computed from the row, the
platform, the chat type and the clock (no wait, no poll, no provider call).
`next_action` stays `none`; `recipient_delivery_confirmed` keeps its rule.

`messenger_reports` is a capability list, never a status: the receipt levels this
messenger can report for this chat.

| Messenger and chat | `messenger_reports` |
| --- | --- |
| WhatsApp (private chat or group) | `["delivered","read"]`; read and played are best-effort and depend on the recipient's settings |
| Telegram private chat with a human peer | `["read"]` (never delivered) |
| Telegram group, channel, bot peer; any self chat (Saved Messages, note to self) | `[]` |
| A platform or Telegram chat type not in this table | `null` (unknown) |

`receipt_state` is one word: `delivered`, `read` or `played` when that evidence has
arrived (the highest level, including a Telegram private `read`); `pending` when
`messenger_reports` includes `delivered`, none has arrived and the send is under 10
minutes old; `overdue` at 10 minutes or more; `not_reported` when the messenger can
never report delivery; `unknown` when `messenger_reports` is `null` or an input is
missing; `receipt_error` when the provider reported a delivery failure. `overdue`
means delayed, not lost: the recipient may be offline or the provider session may be
down (check `get_sync_status`; a provider shown offline there confirms it). A
WhatsApp group state comes from the receipts the provider reports for the message
from any participant (`audience: provider_aggregate`; the adapter does not record
which one), so `delivered` there never means every member got it. An agent that
needs a push subscribes to `message.status` events instead of polling.

## Durable updates and coverage

The account-scoped observation ledger accepts evidence before a message or chat
is archived. Reads join only authorized message IDs or an authorized outbox
reference. Receipt evidence does not change the message's content revision.
Durable publication jobs emit at most 100 message status events per collector
drain pass. Restart resumes their committed cursor; duplicate events are
suppressed. Each `message.status` snapshot preserves the proof known when it
was published. Other message responses may include newer receipt observations,
whose observation time is explicit.

Use `get_updates(kinds=["message.status"], consumer="receipt-view")` for status
updates. Existing consumers keep their exact filter binding. Change filters only
through the existing version-checked reset, or create another consumer. Archive
ACK state stays independent for every principal/consumer.

This is forward observation capture, not a complete historical receipt archive.
Events missed while offline, disabled read receipts and provider limitations may
leave evidence unknown indefinitely. Receipt-before-message evidence is exposed
on subsequent message reads; it does not require a synthetic status event for a
message which was not archived at publication time. No exact provider latency or
all-history coverage is promised. Live sending/read acceptance needs a separately
approved message and an actual recipient read.

## Upgrade and recovery

Migration23 adds three tables and preserves earlier migration checksums. Upgrade
the cold backup runtime before applying it, then apply the gateway/collector role
templates as owner. The gateway only reads receipt tables; the collector records
observations and advances publication jobs. Sending permissions remain unchanged.
Backups include ledger, pending jobs and publication frontiers. Quarantine restore
disables collectors, revokes credentials and prevents outbox replay; receipt jobs
are not activated until the operator explicitly resumes collection.
