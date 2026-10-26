# Agent access and audit

Introduced in Messaging 0.5 / schema 5; current source 0.9.19 / schema 46. Setup is done by the operator through the local CLI
or SSH with the owner DSN. There are no remote MCP/REST methods for managing keys and permissions.
Provider sessions, the owner DSN and the backup decryption key are not given to agents.

## What a credential allows

Each agent has its own principal and a separate Bearer key. A key is issued
for 1–365 days (7 by default), can be extended in place by the owner ([below](#extending-a-key)), is stored in the DB only as a SHA256, and has a public UUID
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

A chat the account joins later (`join_chat`, ANYM-243) is visible to no existing
chat-scoped key: `--chats` lists stay exact. A key with all chats of the account sees
it only once the account's collection policy collects it. A join changes no key,
grant or policy; rejoining a chat that is already archived keeps its existing grants.

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

Since ANYM-470 `capabilities` also describes the caller's own key and accounts, for chat-scoped
keys too, with no other account, chat or field becoming visible:

- `expires_at`: the calling key's own expiry, ISO 8601 UTC with a `Z` suffix
  (`2026-11-13T14:15:53.242375Z`), read on every call, so `extend-key` shows at once. The type is
  string or null; DeedKit keys always expire (`api_keys.expires_at` is `NOT NULL`), so it is
  always a string. Through the gateway it is the gateway's backend messaging key; the caller's
  own gateway key expiry is `platform_capabilities.credential.expires_at`, so a gateway client
  warns on the earlier of the two.
- `accounts[].self_provider_id`: the account owner's own provider user ID for each account in
  `accounts`. Telegram: the logged-in user's numeric ID as a decimal string, the form of a
  message `sender` and of a private chat's `provider_chat_id`. WhatsApp (whatsmeow and
  whatsapp-web.js): the phone-number ID `<digits>@c.us`, never a LID; a WhatsApp private chat's
  `provider_chat_id` may be `<digits>@c.us` or `<digits>@lid`, so a peer check accepts the
  peer's LID form ([WHATSAPP_IDENTITIES.md](WHATSAPP_IDENTITIES.md)). Each chat's account is the
  `account` of its chat record.
  It is the last ID the account's collector verified at a successful connect: the collector
  records it only after the provider confirmed the configured identity (`expected_user_id`,
  `expected_wid`). It stays after a disconnect or a failed identity check, a re-pair overwrites
  it at the next verified connect without touching archived senders, and a restore returns the
  snapshot's value. It is not proof of a live session nor of who wrote the archived history:
  check it on each session start and never cache a proof across a re-pair.
  `null` means never verified (no verified connect since this release; synthetic accounts never
  have one), cured by one collector reconnect, or not storable (the 16 KiB operations limit, a
  stored value failing the format). It is never guessed from messages, and `get_sync_status`
  does not return it.

Since ANYM-270, `capabilities` takes `detail` (`summary` or `full`) and `sections`:

- MCP (direct and the gateway's `messages_capabilities`), REST (`POST /v1/capabilities`, the
  gateway's `/v1/messages_capabilities`) and the Python and npm clients return `detail=summary`
  when `detail` is omitted: service, version, principal, `credential_id`, permissions,
  `expires_at`, accounts (`id`, `platform`, `label`, `self_provider_id`; ANYM-470), `chat_scope`
  as `all_in_permitted_accounts` plus `chat_count`,
  `tool_manifest.version`, the section names and a `next_action`. Through the gateway the summary
  names `gateway_principal`, `backend_principal` and `gateway_tool_count` and leaves out backend
  permissions, as `full` does. Callers that need everything pass `detail=full`, as the in-repo
  consumers do; the gateway's own capability check and the calls service ask once more with no
  arguments when a messaging API from before `detail` answers 400 `invalid_parameters`.
- `sections=[names]` returns only those blocks with today's keys: `identity`, `accounts`,
  `coverage`, `tool_manifest`, `search_options`, `sending`, `file_uploads`, `subscriptions`
  (with `events`), `feedback`, `messenger_operations` (per-account `supported_by_account`; through
  the gateway the `gateway_actions*` fields), `limits` (with `unavailable`). Every key of `full`
  is in exactly one section. An unknown name returns `invalid_filter`. In the
  `messenger_operations` section each account's `operations`, `count` and `available_operations`
  hold only operations this key may use there (`key_permitted` of `list_messenger_operations`):
  a chat-scoped key sees no full-account operation such as `list_contacts` or `create_group`.
- `detail=full` is the previous payload unchanged, including its full per-account operation
  list. Every view is cut from the same scoped payload, so no view shows an account or chat
  outside the key's scope.

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
python -m messaging_service list-keys --all
python -m messaging_service key-info --key-file /dev/stdin < /private/assistant-work.token
python -m messaging_service revoke-key --key-id PUBLIC_CREDENTIAL_UUID
python -m messaging_service extend-key --key-id PUBLIC_CREDENTIAL_UUID --days 365
python -m messaging_service set-access --principal assistant-work --disable
python -m messaging_service set-access --principal assistant-work --enable
python -m messaging_service audit-tail --principal assistant-work --limit 50
```

Permissions that are not specified are preserved. Issuing another key to the same principal
without `--chats`/`--permissions` keeps its restrictions; the account list must be given
explicitly. Rotation with the same permissions does not reset the ACK. Issuing a key to a disabled
principal does not enable it again.

### Key inventory

`list-keys --all` lists every key of every principal (exactly one of `--principal` or `--all`):
public key ID, principal, `created_at` (the operator `issue_key` audit row, `null` when none
exists), `expires_at` and `revoked`. `key-info --key-file FILE` reads one token per line (at most
100 lines and 64 KiB; `/dev/stdin` for a batch) and prints, in input order, `found` with the
public key ID, principal, `expires_at` and `revoked`, or `found: false`; a line that cannot be a
token reports `error: invalid_key`. Neither command prints a token or a digest, and neither
writes. `deedkit admin key list` (docs/operations/production.md, Admin CLI) runs both.

### Repeatable issue (ANYM-495, schema 44)

`issue-key --token-file FILE --operation-id ID` (both or neither) stores the digest of a token
the caller generated and wrote durably first; it never writes or prints the token and takes no
`--key-file`. The file is an owner-only regular file (mode 0600 or tighter, no symlink, no FIFO) or
`/dev/stdin`, with one token line; anything else is `unsafe_token_file` or `invalid_key`. The
operation ID is 1–128 characters of `[A-Za-z0-9._/-]` and is unique per credential: one parent
operation uses distinct derived IDs for its main and archive keys. The same ID with the same
request (principal, accounts, chats, permissions, key permissions, days, token digest) answers the existing key's
public fields with `"replayed": true` and creates nothing, so a crash at any point is resolved by
running the same command again; the same ID with another request is `operation_conflict`, and a
token already stored under another key is `token_in_use`. `key-info --operation-id ID` reads the
key an operation issued (public fields only, or `found: false`).

`--check` on `issue-key`, `extend-key`, `revoke-key`, `list-keys` and `key-info` validates the
arguments (for `issue-key` the issue's own validation: accounts, chats, permission ceilings, a
token already in use and an operation conflict; for `key-info` the key file and its bounds),
writes nothing and answers `{"check": "ok" | "schema_not_ready", "schema_version",
"required"}`; the admin CLI uses it to test a backend before a multi-store step. `ok` means
the real command would succeed now, either creating the key or replaying the operation;
`key-info --operation-id` tells which. A validation failure exits with the same error as the real
command (`operation_conflict`, `token_in_use`, ...), and an unreachable or refused database with
its connection error, never `schema_not_ready`. Invocations without these options behave as before.
`deedkit admin key issue NEW --like SOURCE` (docs/operations/production.md, Admin CLI) runs these
commands to clone an agent's access into a new principal across every store. `deedkit admin key
extend ID --days N` and `key revoke ID` run `extend-key` and `revoke-key` on each of the principal's
keys (the calls archive key too) together with the calendar, calls and gateway stores;
`extend-key --check` validates no expiry, so the CLI checks by the host clock with a one-hour
margin (the backend databases share the admin host's clock; docs/operations/production.md, Admin
CLI), and a larger database clock skew stops it with the gateway unchanged. `key issue` passes
`issue-key --new-principal`, which refuses `principal_exists` for a principal that already exists
(after the operation replay check, so a rerun still replays) instead of rewriting its scope, and
`issue-key --key-permissions-of KEY_ID`, which gives the new key that key's stored ceiling as is (a
NULL legacy ceiling too; `unknown_key` if there is none, `key_permissions_conflict` together with
`--key-permissions`). It reads the source with `principal-info --principal ID`, whose keys carry
`effective_permissions` (the principal's permissions narrowed by the key's ceiling).

A token is 32–256 printable ASCII characters without spaces (for example
`python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`). `/dev/stdin` may be a pipe or a
redirected file. Both the creating call (`"replayed": false`) and a replay answer with `revoked`
and `expires_at`, so a replay of a since-revoked key is visible as such. Pass the same `--days` on
a rerun: another value is `operation_conflict`. Two concurrent runs of one operation end as one key
and a replay (or `operation_conflict`), never a database error. A token lost before it was written
durably needs a new operation ID with a new token.

```sh
python -m messaging_service issue-key --principal assistant-work --accounts account-work \
  --days 7 --token-file /dev/stdin --operation-id op-2026-10-24/main < /private/new.token
python -m messaging_service key-info --operation-id op-2026-10-24/main
python -m messaging_service issue-key --check --principal assistant-work --accounts account-work \
  --days 7 --token-file /dev/stdin --operation-id op-2026-10-24/main < /private/new.token
```

### Extending a key

Since schema 36 `extend-key` sets one live key's expiry to now + `--days` (an integer 1–365,
required), measured by the database clock under the principal lock. The token, its digest,
permissions and the principal's scope stay the same, so its subscriptions, their ACK and its
feedback keep working, and neither the gateway policy (`key_sha256`) nor a calls `archive_config`
token file needs a change. An unknown, revoked or already expired key is refused (`unknown_key`,
`key_revoked`, `key_expired`), and so is a value that would move the expiry earlier
(`key_lifetime_would_shorten`); a refusal changes nothing. The command prints only the public
key ID and the previous and new `expires_at`; `list-keys` shows the new date. It writes one
operator audit row `extend_key` (principal and public key ID, never the token or digest) in the
same transaction, so a failed audit write leaves the expiry unchanged. Agents cannot extend
their own keys. After a suspected leak, rotate with `issue-key` and `revoke-key` instead; to
shorten a key, also revoke it and issue a new one. A messaging key gives no in-band warning before
it expires (unlike a gateway key): a direct client reads `capabilities.expires_at` and warns
itself, and the owner checks `list-keys`; with keys of up to a year this is the only signal.

Reply-queue dismissals and snoozes belong to one key (SEARCH.md): `extend-key` keeps them,
and a rotation (`issue-key` plus `revoke-key`) drops them. Since schema 41 reply-queue mutes
(`mute_reply_source`) belong to the principal: every key of the principal shares them, a
rotation keeps them, and another principal never sees or is affected by them. Entries for
chats outside the current scope stay stored but hidden until the scope widens again.

Since schema 39 the maintainer records what happened to agent feedback (SUBSCRIPTIONS.md,
feedback status) through the local operator container only; it is not an MCP tool, REST route
or gateway action, and no agent key reaches it:

```sh
anymsg set-feedback-status --id <uuid>... --status <status> [--spec ANYM-<n>] \
    [--fixed-in deedkit@<40-hex sha>|anymsg@<semver>] [--duplicate-of <uuid>] [--note <text>]
```

`--status` is `received`, `accepted`, `declined`, `duplicate`, `fixed` or `verify_requested`;
`verified` and `reopened` belong to the reporting agent and are refused. `accepted`, `fixed`
and `verify_requested` require `--spec`; `fixed` and `verify_requested` require `--fixed-in`
(only they accept it); `duplicate` requires `--duplicate-of` or `--spec`, and only it accepts
`--duplicate-of`, which must be another item of the same key. Set `fixed` once every card of
the report is merged (the merge SHA) and `verify_requested` once that SHA is deployed (the
production.md receipt), never earlier: merged source is not live behavior. A call takes 1–100
IDs and replaces the whole disposition of each (omitted fields become empty) in one
transaction bounded like a submission; any refusal, including an unknown ID (reported by
count only), changes nothing. It prints the count and the IDs and writes one operator audit
row `set_feedback_status` per item (principal, key ID and feedback ID; never the note).

The maintainer reads all agents' feedback for triage with a read-only operator command (ANYM-265),
also only through the local operator container:

```sh
anymsg export-feedback [--since <time> [--overlap <minutes>] | --changed-since <time> [--overlap <minutes>] \
    | --page <next_page>] [--limit <1-100>]
```

The output is JSON marked `untrusted_content: true`: descriptions are agent text, never
instructions. The default mode pages `submissions` (feedback ID, `request_id`, category,
description, operation, `created_at`, principal and key ID; never `refs`) with their current
`dispositions` (status, note, `fixed_in`, `spec_ref`, `duplicate_of`, `status_changed_at`) by
`(created_at, id)`; `--changed-since` pages only dispositions by `(status_changed_at, id)`, so a
status change on an old report is seen. `export_head` (database time) is fixed by the first page
and bounds every later page of that export, read with `--page <next_page>` while `has_more` is
true. Pages are separate read-only transactions, not a snapshot, so an export starts at `--since`
minus `--overlap` (default 10, 2–1440 minutes; a submission transaction is aborted after 60 s),
the caller deduplicates by feedback ID and stores `export_head` as its next `--since` only after
the last page. The command writes nothing, including no audit row, sends nothing and makes no
provider request.

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

The deletion purge (schema 45, ANYM-511) runs as `messaging_purge` (`purge_role.sql`) through
its own login `messaging_purge_runtime`: it reads the archive and updates only content columns
of deleted messages, deletes their derived text and open download grants, and inserts only its
own audit rows. It cannot delete messages or events, insert reviews, run DDL or reach sessions.

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
`fetch_subscription_events` and `wait_for_subscription_events` stay `readOnlyHint=true`:
issuing a receipt (and creating the initial consumption row) is bookkeeping that
changes no consumption, provider state or other key's data. The exact meaning,
including lifecycle checks on every subscription read, is in
[SUBSCRIPTIONS.md](SUBSCRIPTIONS.md#schema-32-explicit-consumption-and-registered-hints).
A revocation or ACL change waits for a hint attempt in flight (about 5 s: its TLS exchange),
and new requests of that principal queue behind the waiting revocation; see its
revocation fence there.

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
