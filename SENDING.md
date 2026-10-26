# Confirmed text sending

AnyMsg dispatches text through the **existing** account collector. It does not
open another Telegram/WhatsApp session. Sending is opt-in twice: the operator
enables the account and grants an agent the separate `send` permission.
Existing read keys keep their permissions after upgrade.

There is **no server approval token or approval bot** in this release. The acting
agent must first show the owner the sending account, exact recipient, full text
and reply target, and obtain explicit confirmation in its conversation. Tool
availability, retrieved messages and a boolean supplied by an agent are not owner
approval. The API intentionally does not accept `approved: true` as proof.

## Agent flow

1. Resolve the account and archive chat ID. Display the account, recipient,
   exact text and optional reply to the owner; obtain confirmation.
2. Generate an action ID (8–128 ASCII letters/digits or `_.:-`), then call
   `send_message(account, chat_id, text, request_id, reply_to="", wait_s=0)`.
   `reply_to` is the archive message ID in the **same chat and account**.
3. Poll `get_send_status(send_id)`. An HTTP/MCP response or `queued` status
   alone does not prove that the provider accepted the message.
4. After a lost response, reuse the **same request ID and exact payload**.
   Changing the payload with that ID fails with `idempotency_conflict`.
   Never create another action ID automatically to bypass an uncertain result.

### Optional bounded status wait

`wait_s` accepts a number from 0 to 25 seconds. Its default, `0`, returns the
enqueue receipt immediately. A positive value observes the same send until it
leaves `queued`/`dispatching`, or the wait expires. Each short poll authorizes
the same key again; no database connection is held between polls. Waiting never
dispatches another attempt or changes the request ID.

Wait responses preserve the normal status and receipt fields and add
`wait_elapsed_s` and `wait_timed_out`. A timed-out wait can still return `queued`
or `dispatching`; continue with `get_send_status(send_id)`. `unknown` ends the
wait immediately and remains uncertain. A matching body or timestamp in history
does not become a provider receipt. `sent` continues to mean provider acceptance,
with recipient delivery described separately by `delivery_receipt`.

The service admits at most 32 concurrent waits per process. `send_wait_busy`
or `send_status_timeout` describes the observation request, not a failed send.
Inspect the same action; after a lost enqueue response, reuse its original
request ID and exact payload. Cancelling or timing out a wait never cancels or
retries the already queued action.

`preview_message` requires only `read` and does not require sending to be enabled
or a collector to be connected. For rich text, first preview the exact account,
chat, source, format and reply. Show the owner its visible text, warnings and link
destinations, then pass the same arguments plus `preview_digest` to `send_message`.
The digest detects payload changes; it does not prove human approval.

Sending tools are also available as `POST /v1/send_message` and
`POST /v1/get_send_status`, using the same scoped bearer key. Responses contain
receipt metadata, not the outgoing text or provider session credentials.
Refresh MCP discovery after upgrading; the messaging manifest contains 41 tools.

| State | Meaning | Next action |
| --- | --- | --- |
| `queued` | Durable request, no provider send has begun | Poll status |
| `dispatching` | The collector started one provider attempt | Poll status |
| `sent` | Provider returned a matching outgoing message ID | Inspect the separate `delivery_receipt` evidence |
| `cancelled` | Access or account policy changed before dispatch | Review permissions and obtain approval for a new action |
| `expired` | The ten-minute pending window elapsed | Review and obtain approval for a new action |
| `failed` | Locally rejected before invoking send | Inspect safe error code |
| `unknown` | Timeout, interrupted dispatch or unverified receipt | Check history; never automatically send again |

The 30-second provider timeout starts when the request leaves the collector.
Waiting for the shared Telegram rate gate (history or media hit a flood wait)
and the last-moment access check are bounded by the ten-minute expiry instead.
A send that fails before reaching the provider, or that Telegram refuses with a
flood wait, ends as `failed` with `send_not_started` (or `expired` with
`send_expired`): nothing was sent, so a new action with the owner's approval is
safe. `unknown` remains only for attempts the provider may have received.

The provider payload is limited to 4000 UTF-16 units, with no automatic splitting.
Plain is the default; Markdown and restricted HTML use the passive
`preview_message` tool and require its exact `preview_digest` when queued.
The preview distinguishes visible text from provider wire text and exposes
formatting warnings and link destinations. Link previews are off by default;
WhatsApp multi-device behavior is provider-controlled. See [FORMATTING.md](FORMATTING.md).
`send_message` remains the confirmed text/reply path. The separate allowlisted
[messenger-operation interface](MESSENGER_OPERATIONS.md) adds registered original
uploads, forwarding, edits, reactions and provider-supported administrative actions;
it has independent per-key and per-account opt-in. WhatsApp global deletion is
unsupported, with no local-delete fallback. A server human-approval workflow is
not implemented.

Reply to an already archived message is supported. Provider chat
permissions still apply. Reads and WhatsApp sends do not explicitly mark chats
read; WhatsApp uses `sendSeen: false`.

### New WhatsApp conversations by phone number

On native WhatsApp accounts an agent can write to a number that has no archived
chat: check it with the `open_chat_by_phone` messenger operation (`create=false`),
show the owner the account, number, verified name and exact text, obtain
confirmation, call it again with `create=true` for the `chat_id`, then send with
`send_message`. A phone-shaped `chat_id` that is not found returns the exact `next_call` for each step, or
the reason there is none. Each account allows at most 10 WhatsApp lookups per rolling hour (local answers are free);
see [MESSENGER_OPERATIONS.md](MESSENGER_OPERATIONS.md#opening-a-whatsapp-chat-by-phone-number).
Cold messages from a personal account to strangers can get it restricted or banned
by WhatsApp, so keep them few and expected. New Telegram conversations by phone
number or username are not implemented.

## Sender identity and provenance (0.9.12)

Telegram private short-send receipts may omit the sender. The collector uses its
cached, authenticated self entity after checking the configured owner ID; it
makes no extra entity lookup. Anonymous group/channel senders are never inferred.
Legacy receipts are repaired only with matching confirmed outbox evidence and
identical content/time, using a new revision rather than rewriting old snapshots.
The optional `sender_identity_basis` makes this completion explicit.

Messages expose `agent_send` with the sending principal and receipt ID, including
compact responses. It proves a server-dispatched action, not owner approval.
An edited body can differ from the original text; ambiguous duplicate provider
receipts do not guess an actor. The originating principal's `get_send_status`
also includes its credential ID, request ID and acting-agent approval mode.
Credential/request IDs are not exposed in message provenance to other readers.
Since 0.9.13, passive provider receipts are exposed separately as
`delivery_receipt`; `sent` still means provider acceptance. Unknown provider
times remain null. See [RECEIPTS.md](RECEIPTS.md) for coverage and group semantics.
See [MESSAGE_ACTIONS.md](MESSAGE_ACTIONS.md) for the remaining stages.

## Operator setup

Only the private owner CLI can change permissions. Use each agent's own account
and chat scope; do not grant `send` to every read key.

```sh
anymsg migrate
anymsg set-sending --account account-personal --enable
anymsg set-access --principal assistant-personal --permissions read download cursor-write send
```

Apply `gateway_role.sql` and `collector_role.sql` as owner after migration25.
Gateway can enqueue/read receipts but cannot set their state or invoke providers.
Collector can update delivery state and read public key/policy metadata needed
for a fresh authorization check; it cannot read token hashes or change ACL.
The account must have an active connected collector and its collection policy
must permit the target chat. Queued work rechecks credential expiration/revocation,
principal permissions/scope and account policy immediately before dispatch.
Policy changes invalidate old scope-version receipts as described in ACCESS.md.

WhatsApp has two independent private IPC lanes inside its existing authenticated
collector. Sending has its own socket, response reader and serialization gate;
history and media use the background pipe. A slow download, including pipe
backpressure, does not occupy the sending lane. Only one send and one background
operation can run at once per account; this does not increase bulk API concurrency.
The same browser and session serve both lanes. Browser/provider outages can still
affect both, and this is not a guarantee of provider delivery latency. Cancellation
or protocol corruption stops that owned client; uncertain sends remain `unknown`.
Account cooldowns and fresh authorization checks still apply to sending.
An account can accept at most 60 new actions per minute; idempotent receipt reads
do not count as new actions. Ambiguous attempts are **not retried** by the outbox.
Telegram's client may retry one RPC using its existing random ID; that is different
from submitting a new application action. WhatsApp does not get an application retry.

Migration22 is additive and leaves all sending disabled. Upgrade the cold backup
runtime before applying it. Encrypted backups include the outbox. Quarantine
restore disables send policies, cancels queued jobs, marks interrupted attempts
unknown and revokes credentials: restoring a backup never automatically replays
outgoing messages. The audit records identities and resource IDs, not text.

Provider references: [Telethon send_message](https://docs.telethon.dev/en/stable/modules/client.html),
[Telegram random_id](https://core.telegram.org/method/messages.sendMessage),
[WhatsApp send options](https://docs.wwebjs.dev/Client.js.html).
