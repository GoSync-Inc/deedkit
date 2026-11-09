# Message actions and agent feedback

This document preserves feedback history and action/acceptance boundaries.
Current work and status are maintained only in [BACKLOG.md](BACKLOG.md),
including optional server approvals (DK-27) and discovery notifications (DK-30).
Collection/retrieval remains separate from external actions.
An agent report is evidence about a client/version, not permission to send or
change approval policy. Reproduce findings against the current server first.

## Implemented sender/provenance follow-up (0.9.12)

- Complete missing Telegram private short-send senders from the cached self
  entity after checking the configured account owner ID. Never infer anonymous
  group/channel senders or replace a known sender.
- Repair at most 100 legacy private outgoing records on collector startup,
  requiring matching confirmed outbox account, chat, provider message ID, text
  and reply. Append an archive revision, preserving original snapshots.
  Content/timestamp changes remain conflicts. Resolve only equivalent competing
  copies; unrelated contradictions remain visible.
- Expose `agent_send` in history/search/context/updates, including compact MCP
  results. Attribute the principal through a confirmed outbox receipt. Multiple
  receipts for one provider reference are explicitly ambiguous. Distinguish
  edited message text from the original sent text.
- The caller's send receipt includes its principal, credential ID and request ID.
  Other readers do not receive credential/request IDs. Provenance adds no token,
  request hash or original outgoing body.
- `owner_approval_verified_by_server` remains false: the outbox proves an agent
  action, but does not prove owner approval.

Acceptance: real Telethon message shapes, synthetic PostgreSQL fixtures,
immutable-revision and denied-scope checks, no provider sends. Production checks
must separately report which legacy records were repaired.

## Delivery and read observations (implemented in 0.9.13)

The implemented contract and coverage limits are in [RECEIPTS.md](RECEIPTS.md).

Keep provider acceptance (`sent`) separate from delivery/read/played observations.
Receipt capture must not mark provider chats read or acknowledge archive updates.

1. Add a durable, idempotent observation ledger keyed by account + provider chat
   + message reference, accepting receipts before message ingestion. Persist
   provider time when supplied and observation time separately.
2. Capture Telegram outgoing read boundaries through the existing client. The
   [outbox-read update](https://core.telegram.org/constructor/updateReadHistoryOutbox)
   supplies a peer and maximum message ID, not an exact recipient read timestamp
   or separate device-delivery timestamp. Unknown times stay null. A private-chat
   boundary must not be described as every participant reading a group message.
3. The historical browser adapter captures WhatsApp observations through
   [`message_ack`](https://docs.wwebjs.dev/Client.html#event:message_ack) and
   [ACK states](https://docs.wwebjs.dev/global.html#MessageAck). The native
   [whatsmeow provider](docs/whatsapp-native.md) is now also implemented, with
   bounded observed receipt evidence. Missing read receipts remain unknown;
   observation does not start extra sessions or imply continuous provider polling.
4. Expose receipt state, basis, `observed_at` and optional provider time in send
   status and message evidence. Emit deduplicated receipt updates; extend kind
   filters without silently changing existing consumer bindings.
5. Test out-of-order/duplicate events, restart/spool recovery, fencing, account
   isolation, groups, ACL changes, migrations and quarantine backup restore.
   Live acceptance needs a separately approved send and a recipient read.

## Implemented formatting and supported expanded actions

Formatting is a new requirement from the subsequent feedback. Its implemented 0.9.14
contract is in [FORMATTING.md](FORMATTING.md): bounded maintained parser,
passive preview, native rendering, payload-bound outbox persistence, then incoming
format evidence. It is implemented after receipt capture
and before expanded sends; it uses the existing provider sessions. Long messages stay
single actions until an explicit multipart protocol exists.

Reaction reads, file/voice sends, supported own-message edits/deletes,
reactions and explicit read marks are implemented through the allowlisted
[operation catalog](MESSENGER_OPERATIONS.md). Support differs by provider/account;
check the current manifest instead of treating an upstream capability as a
completed adapter. [Scoped file uploads](https://github.com/GoSync-Inc/deedkit/pull/22)
also exist; staging bytes does not approve or send them.

The acceptance boundaries remain: observed/unknown actors, account isolation,
exact attachment/target approval, bounded resources, stale-target rejection,
retained evidence and no blind retries. Ordinary reads stay passive. An unknown
mutation outcome must be reconciled, not resent with a new request ID.

Optional typing extensions and server approvals are DK-26/DK-27 in the central
backlog. If enabled, server approval must be payload-bound, single-use and
expiring; agent keys cannot approve drafts. The report does not change the
owner's acting-agent approval mode.

## Polls

Reading a poll is passive: the archive keeps a Telegram (ANYM-920) or WhatsApp (ANYM-921) poll's question,
options, counts and, where the provider attributes votes, who chose what ([CONVERSATION.md](CONVERSATION.md)).
None of it votes. Voting stays the existing `vote_poll` operation (Telegram, owner-confirmed;
[MESSENGER_OPERATIONS.md](MESSENGER_OPERATIONS.md)), and creating one stays `send_poll`. The background read of
a public Telegram poll's voter list is a read (`messages.getPollVotes`), never a vote.

## Client discovery

Tool counts depend on the current version, provider and grants. A different
client count alone does not prove caching: compare actual names, `tools/list`,
`capabilities.tool_manifest` and any host filtering/configuration first. Check
the caller's effective permissions separately; discovery never grants sending
access. The existing REST fallback uses the same ACL and is useful for diagnosis
but does not refresh a client's tool cache.

The current stateless JSON POST transport explicitly rejects a notification
GET stream with 405. That is permitted by the
[MCP transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
Do not advertise `tools.listChanged: true` without implementing notification
delivery. The [tools specification](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
ties that capability to list-change notifications; it does not guarantee that
every client discards its persistent cache.

The optional DK-30 discovery design binds streams to authenticated principals,
rechecks revoked/expired keys, isolates clients, bounds queues/reconnect resources
and announces only manifest changes. Acceptance includes server restart/deployment
recovery and fresh `tools/list` in representative clients.
Avoid adding a GET stream that never emits data merely to hide the old timeout.
Until then, refresh/recreate the connector when stale discovery is established,
and keep unresolved host filtering/cache behavior explicit.

## Older report sections

Strict arguments, compact responses, filtered subscriptions, source diagnostics
and Ogg/Opus input already exist. They do not prove live extraction, full history
or zero client cache problems. History waves, exhausted media recovery, storage
capacity, file analysis and MCP notification transport remain outstanding.

`connected` is not proof of complete history: distinguish live capture from
catch-up gaps, archive conflicts and media download failures. Storage-paused
originals need capacity/lifecycle work, not a lower safety floor or a blind
collector restart. Classify conflicting observations with evidence; never clear
them in bulk to make an account summary look healthy. Remaining retrieval and
action work is recorded in the single central backlog;
neither a source increment nor this historical report proves live completeness.
