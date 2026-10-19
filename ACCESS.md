# Agent access and audit

Introduced in Messaging 0.5 / schema 5; current source 0.9.19 / schema 35. Setup is done by the operator through the local CLI
or SSH with the owner DSN. There are no remote MCP/REST methods for managing keys and permissions.
Provider sessions, the owner DSN and the backup decryption key are not given to agents.

## What a credential allows

Each agent has its own principal and a separate Bearer key. A key is valid
for 1–30 days (7 by default), is stored in the DB only as a SHA256, and has a public UUID
for revocation. Several keys of one principal share the principal scope and the
archive-consumer ACK; the subscription ACK and feedback belong to the exact key.
Different agents get different principals. Keys must not be passed in a query string,
pasted into conversations or saved in tracked Git files.

| Level | Rule |
|---|---|
| Accounts | An explicit non-empty list of existing account IDs |
| Chats | `NULL`/`--all-chats` — all chats of these accounts, including future ones; `--chats ID…` — only those listed; an empty `--chats` — none |
| `read` | Search, history, context with attachment metadata, chat list, reading events |
| `download` | Getting an attachment's original/status and a temporary link; the allowed attachment ID is known |
| `cursor-write` | ACK of a previously received page; on its own it does not allow reading the archive |
| `review-write` together with `read` | An immutable review mark with an author, revisions of all sources and a link to the result |
| `send` | Enqueue exact owner-confirmed text and inspect own send receipts; requires separate account opt-in (SENDING.md) |
| `subscription-write` together with `read` | Creating/changing own subscriptions, binding a pre-registered receiver and a synthetic test hint |
| `subscription-ack` together with `read` | ACK of an issued page of an own subscription; a separate position for the exact key |
| `feedback-write` together with `read` | A local private feedback record with verified source references; no external sending |
| `capabilities` | Available to any valid key, including one without operational permissions |

By default a new principal gets read/download/cursor-write in the explicitly specified accounts.
`review-write` is granted separately and lets a key create marks only together with `read`;
reading marks is part of `read`. Details: [REVIEWS.md](REVIEWS.md).
For a restricted assistant, specify chat IDs and permissions when issuing the key.
`read` includes file names/metadata in the message context, but without `download`
the original and the link are not issued. `download` is a separate right to the content of a
file; keep this in mind when granting it without `read`.

`capabilities` returns the current permissions, `scope_version`, the public `credential_id`
and the allowed accounts. When access is limited by chats, account-wide timestamps/status
and degradation reasons are hidden (`coverage=chat_scope_only`): they may depend
on other people's conversations. An empty chat list does not reveal even account labels.

## Operator commands

The commands require `DATABASE_URL_FILE` with the owner DSN. Below are placeholders, not real
account/chat IDs. The output key file is created with mode 0600 and is not overwritten.

```sh
python -m messaging_service issue-key --principal assistant-work \
  --accounts account-work --chats chat-work --permissions read download cursor-write \
  --days 7 --key-file /private/new-assistant-work.token

python -m messaging_service set-access --principal assistant-work \
  --chats chat-work --permissions read

python -m messaging_service list-keys --principal assistant-work
python -m messaging_service revoke-key --key-id PUBLIC_CREDENTIAL_UUID
python -m messaging_service set-access --principal assistant-work --disable
python -m messaging_service set-access --principal assistant-work --enable
python -m messaging_service audit-tail --principal assistant-work --limit 50
```

Permissions that are not specified are preserved. Issuing another key to the same principal
without `--chats`/`--permissions` keeps its restrictions; the account list must be given
explicitly. Rotation with the same permissions does not reset the ACK. Issuing a key to a disabled
principal does not enable it again.

Any change to accounts, chats, operations or enabled increases the scope version:
old receipts and download grants stop working. The same consumer starts
reading events again in the new scope. Re-enabling or restoring the earlier permissions
does not revive the earlier receipts. Text or a file already handed to a client
cannot be recalled. An in-flight operation finishes before the permissions change; the next
operation re-checks them in the DB. All operator mutations use the same
transactional principal lock; a direct UPDATE that bypasses the CLI breaks this rule.

## Audit log boundaries

`audit_log` contains the DB time, principal, public key UUID, db_role, actor,
fixed operation/outcome/error codes and at most 100 IDs of resources that were already allowed.
It has no message body, search query, credentials, download grant,
URL, unknown submitted ID or traceback. Rejected requests have an empty
resource list. Operator mutations are marked separately.

A successful ACK change/link issue and its audit are committed atomically. If writing the
audit fails, the operation returns an error and rolls back. A rejected operation
rolls back its writes but keeps the audit of the rejection. At the HTTP boundary, unauthorized
and malformed requests are counted anonymously with a limit of 5 records/min/peer, so that
foreign traffic does not fill the DB. SDK/protocol validation before the Store call
is not a full request log: it is not a forensic access log of every
network packet. SDK errors are returned through `ToolError` without a crash traceback.
Additionally, 0.5.1 redacts diagnostic records of the MCP/HTTP/DB-pool libraries
before formatting: severity, a fixed component category and the exception type
remain. The original message/args/traceback are removed. This is required even for
expected SDK warnings: an unknown tool name may be input text.
An error with a detailed traceback should be reproduced on fictional fixtures;
do not enable verbose logging of real conversations on a public gateway.

The gateway DB role `messaging_gateway` has SELECT on the archive, CRUD on its own
service tables consumers/deliveries/grants and only column INSERT on the audit.
It cannot read, edit or delete the audit, forge actor/db_role/time,
change ACLs/keys, run DDL or manage collectors. The owner applies
`messaging_service/gateway_role.sql`, then grants membership to a dedicated login role.
The account/chat ACL is checked by the shared Store layer for MCP and REST; this is not PostgreSQL RLS.
Compromise of the gateway process itself is a separate threat boundary, since its
DB role reads the archive. This DSN is not available to the agent.

Audit retention is not enabled yet: rows are kept and included in the encrypted
backup. Before significant load, growth measurement and an agreed retention
period are needed. Gateway/uvicorn access logs are off. Proxies/clients have separate
logs; their redaction is checked at deployment and before a live cutover.
In the [Caddy example](deploy/Caddyfile.example) the global log encoder
removes the whole block of request and response headers, including error logs on 502:
turning off the access log alone is not enough, since a URI may contain a download grant.
Logger/status/duration are kept. This is a logging setting; request routes
do not change. Verify support on the actual proxy binary with
`caddy validate` and an isolated failed-upstream test; reference:
[global log](https://caddyserver.com/docs/caddyfile/options#log),
[filter encoder](https://caddyserver.com/docs/caddyfile/directives/log#filter).

## Upgrade and restore

Migration 005 preserves existing accounts, keys, expiry dates, ACKs and scope versions;
they are assigned the earlier read/download/ACK capabilities in all allowed chats.
A backup image that supports schema 5 must be installed **before** the migration:
the previous image knows only schema ≤4. Schema 5 includes 23 tables, among them the audit.

**Do not roll back a single API image to 0.4 after the restrictions are applied.** It does not know
the chat/operation/enabled policy and may serve data under the earlier broad rules.
On a fault, stop the gateway and either fix 0.5 or restore a separately
verified snapshot into quarantine with revoked keys and disabled workers.
Do not open the restored archive to old keys until permissions are assigned again.

Migration 006 adds an optional review-write without widening existing permissions.
Backup image 0.6 must precede schema 6; the manifest includes 26 tables.
The gateway has SELECT and a limited column INSERT on reviews/review_sources,
SELECT/UPDATE on the publication counter, but not UPDATE/DELETE of marks or of the creation time.

## Opt-in sending (schema22)

See [SENDING.md](SENDING.md). Migration does not grant send to existing keys.
Gateway can only insert request columns/read outbox; collector alone updates
states and reads public credential expiration/revocation and ACL metadata,
without token hashes. There is no remote access administration or server approval
gate. Existing agent-side exact owner confirmation remains mandatory.

## Schema 32: subscription consumption and delivery

`subscription-ack` and `feedback-write` require explicit principal and credential
ceilings. Existing NULL legacy key ceilings inherit neither capability. Page
receipts bind principal, exact key, policy versions, subscription generation and
consumption version/positions; only receipt hashes are stored. A changed policy,
paused/closed subscription, expiry or stale concurrent page cannot advance it.

The gateway can bind only an operator-registered opaque receiver belonging to
that exact principal/key; it cannot read endpoint URLs or signing-secret paths.
The internal matcher can enqueue opaque hints but has no receiver/secret grants
or public egress. A separate `messaging_event_delivery` role can deliver hints;
it cannot read message bodies, API token hashes, subscription page receipts or
ACK state. Its deployment requires explicit HTTPS egress and private secret
mounts; installation alone activates nothing. [Delivery contract](SUBSCRIPTIONS.md).
The wake relay (`packages/wake-relay/`) runs on the owner's agent host, not on the
DeedKit host. It holds the receiver secret and the vendor credentials (Claude
routine token, Grok webhook secret) in owner-only files, and forwards only the
content-free hint fields. A woken run reads and ACKs events only through its own
DeedKit key, so key revocation still stops it ([Host receivers](SUBSCRIPTIONS.md#host-receivers-wake-relay)).
