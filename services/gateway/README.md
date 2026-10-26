# DeedKit gateway

A single MCP/REST boundary over independent domain services. This package contains
no collectors, databases, provider credentials, calendar or voice libraries.

The owner supplies a private JSON policy file (`0600`), explicit deployment URLs,
exact profile tool names, principal key hashes, scopes and separately stored backend
keys with pinned hashes. Modules and methods are never discovered by importing code
or automatically exposed when a backend adds a feature. Missing backend capability
contracts fail closed. The calendar backend must provide scoped `permissions`.

```sh
uv sync --locked
uv run deedkit-gateway --policy /private/policy.json --public-url https://agents.example --check
uv run deedkit-gateway --policy /private/policy.json --public-url https://agents.example
```

Default binding is loopback port 13240. Deploy behind an authenticated TLS network
boundary with the exact configured Host; forwarded identity headers are not trusted.
The policy is validated at startup and before every admission. Replace it atomically,
increase `revision`, and keep the same principal/backend key binding when rotating
only a gateway key. Backend URL changes require a restart. Existing subscription
owners are backend credential identities, not gateway session IDs.

Every principal's `expires_at` is enforced at admission: from that instant the key
gets the same `401 unauthorized` as an unknown key. `platform_capabilities.credential`
reports the calling principal's own expiry and never another principal's:
`expires_at` (UTC), whole `expires_in_days` left, `warning_window_days: 7`,
`expiry_warning` (true when fewer than 7 × 24 hours remain) and `next_action`
(`ask_owner_to_extend_gateway_key` in the window, otherwise `none`). In the window
every successful MCP `tools/call` result gets one extra text item, "DeedKit gateway
key expires at <ts> (in N days). Ask the owner to extend it.", and the same fields
under `_meta["deedkit.credential"]`; `structuredContent` and the first text item are
unchanged, so declared output schemas still hold. REST `/v1/{tool}` responses carry
`X-DeedKit-Key-Expires: <expires_at>` in the window, errors after authentication
included; a `401` never does. All of these follow the request's latest authentication,
so a policy replaced mid-request cannot contradict them. `--check` adds `expiring`: the
principals in the window or already expired, as `id`, `expires_at` and `expired`
only. In the window, a principal's first request each UTC day logs one warning per
gateway process; an idle principal logs nothing, so use `--check` for an overview.
These warnings cover only the gateway principal's key. Each backend enforces its own
key expiry and reports none of it to the gateway, so the owner checks the backend
credentials' expiries separately. A messaging backend key is extended in place with
`extend-key` ([ACCESS.md](../../ACCESS.md#extending-a-key)); its token stays the same, so
`key_sha256` and the credential binding in this policy do not change.

To extend a key, the owner writes a new policy file with a later `expires_at` for
that principal, the same `key_sha256` and credential binding, and a higher
`revision`; runs `--check` on it; and replaces the live file atomically (write a
`0600` file beside it, then rename it over the old one). The next request uses
the new date without a restart. Agents cannot extend their own keys.

Containers mount the policy through its directory, never as a single file
(`deploy/compose.gateway.yaml`: `./private/gateway-policy/` read-only at
`/run/deedkit/policy/`, `--policy /run/deedkit/policy/policy.json`). A
single-file bind keeps the inode the container started with, so a rename over
it would never reach the running gateway. The directory is private (no group
or other bits) and readable by the runtime UID 10001; it holds only
`policy.json`, never unrelated secrets or kept backups. A transient private
staging file written there and renamed over `policy.json` is the expected
procedure; the gateway reads only `policy.json`.

`POST /mcp` uses the official SDK transport and lifespan. `GET /mcp` returns 405;
notifications/SSE are disabled. `POST /v1/{selected_tool}` uses the same authorization
and argument schema. There is no generic arbitrary backend executor. Unknown writes
are not retried. Capability discovery intersects profile, local grants and current
backend permissions. An unavailable module leaves platform introspection available.

Arguments that fail a tool's input schema are rejected with 422 `invalid_arguments` on REST and
`isError` on MCP, with the same body: `error`, `next_action: "check_parameters"` and
`field_errors`, a list of at most 20 entries, one per field, in schema order:
`{field, reason, expected?, allowed?}`. `field` is a slash path from the reviewed schema
(`destination_source`, `rows/0/version`), or `$` for the arguments object itself. `reason` is one of
`required`, `unknown_field`, `type`, `enum`, `min_length`, `max_length`, `pattern`, `minimum`,
`maximum`, `min_items`, `max_items`, `unique`, `format`, `blank`, `size`. `expected` and `allowed` come
from the schema, never from the submitted value, and an unknown key is named only when it is a short
identifier. Other rejections (non-finite numbers, a non-object body) keep the bare
`{"error": "invalid_arguments"}`.

When a rejection has a top-level `required` or `unknown_field` entry, the body adds
`tool_fields: {required: [{name, type, values?}], optional: [name]}`: the tool's top-level properties from
the packaged catalog schema (at most 64 names; `values` only for enums of at most 8 values), so a caller with an
old tool list can retry at once. An `unknown_field` entry also adds `tool_list_stale: "likely"` and an
`owner_hint` line to relay once (reconnect the DeedKit MCP client); a missing required field alone does not.
Nested failures keep the shape above. Calling a tool this key cannot call (nonexistent, outside the profile or
of an undeployed module) returns one identical `not_found_or_forbidden` body with
`next_action: "refresh_tool_list_once"` and a `hint`: if the tool is still absent after a refresh, the key does
not have it.

A messaging consumption rejection (for example `invalid_feedback`) relays the backend's `field_errors` only when
the whole list passes `platform_contracts.field_errors.validated` and the tool's reviewed schema: at most 20
entries, one per field; each `field` a declared path of that tool's input schema (or `arguments`); a `reason`
from the vocabulary above; `expected` one of a closed set of hints (types, length and count limits, a few fixed
phrases) or `matching <pattern>` equal to the schema's own pattern; `allowed` at most 50 short values, each one of
the schema's own enum values or declared keys. Any other list, extra key, non-string reason, text that is not
valid UTF-8, or an unlisted error code drops `field_errors` altogether; the other backend detail stays dropped.

`platform_capabilities.tools` and a fresh MCP `tools/list` use the same effective
catalog. Both expose `discovery` metadata (under `_meta["deedkit.discovery"]` for
`tools/list`): selected tool count, a SHA-256 of the effective public schemas,
policy revision, and explicit `notifications_supported: false`. Action-enum changes
also change the hash. A host can cache its registered tools; after an authorized
profile expansion, compare a fresh list with the host registration and reconnect
when they differ. Cached tool visibility never bypasses current call authorization.
This diagnostic does not claim that a particular host has refreshed its registration.

Client discovery: `discovery.catalog` is the first 12 hex digits of `tools_sha256` for the key's own list. The
`platform_capabilities` description in `tools/list` ends with `Catalog: <c>` (the digest is computed without
that sentence). An agent compares the `catalog` in a `platform_capabilities` result with the one in its
cached description; if they differ, its tool list is stale and the owner should reconnect the DeedKit MCP
client. Hosts should also reconnect after a DeedKit release or a profile change. The server `instructions`
say the same.

### Choosing modules (ANYM-271)

An agent that needs only some modules can load fewer tools. Selection only narrows the key's list. It grants
no tool, scope or action.

- **Connection path.** `POST /mcp/modules/<m>[,<m>...]` with `m` from `messaging`, `calendar`, `calls` is the
  same MCP endpoint as `/mcp`. Its `tools/list` and `tools/call` cover `platform_capabilities`,
  `platform_select_modules` (when the profile lists it) and the named modules' tools, intersected with
  everything that limits the key on `/mcp`. Order does not matter (`calendar,messaging` and
  `messaging,calendar` are one path, reported as the sorted form). An unknown or repeated name, an empty list
  or extra segments return 404 `{"error": "unknown_module"}` before any backend is asked. A module the key
  cannot use yields no tools, not an error. Plain `/mcp` is unchanged. Host, Origin, bearer, the 64 KiB body
  limit, the upload-chunk envelope, `GET` 405 and revocation apply to every module path exactly as to `/mcp`.
- **In-session tool.** `platform_select_modules({"modules": [...]})` records a selection for this principal
  and connection path, replacing the previous one. The path's own modules still bound it: on
  `/mcp/modules/messaging`, selecting `["calendar", "messaging"]` keeps messaging only. To widen again, select
  all three modules. The result has `selection_path`, `effective_modules`, the new `tools_sha256`,
  `takes_effect: "next_tools_list"` and `next_action: "refresh_connection_if_tools_list_differs"`; the server
  does not push a list change (ANYM-248). The selection lives in gateway memory only, one entry per
  principal and path, and is cleared by a policy revision change, a gateway restart or a release handover.
  New tool, so existing profiles do not get it until the owner adds it to a profile.
- **Shared key.** Every connection of one key on the same path shares its in-session selection. Use one key
  per host. A module path is stateless and is the better choice when one key must serve several hosts.
- **Calls outside the selection.** An MCP `tools/call` for a tool whose module is not selected returns
  `isError: true` with `{"error": "module_not_selected", "module": "<m>",
  "next_action": "select_module_or_use_its_connection_path"}`, and no backend is contacted.
- **REST has no selection.** `POST /v1/<tool>` ignores module paths and selections and keeps the full profile
  set. `POST /v1/platform_select_modules` records the selection of plain `/mcp`; it affects MCP only.
- **Summary.** `platform_capabilities` adds `selection_path`, `selected_modules` and `modules_available`: for
  each module with at least one usable tool for the key, `{id, purpose, tool_count, selected, path}`.
  `tools` and `discovery` describe the current path's effective list on MCP and the full list on REST.

Connector setup with a module path: keep the host's existing DeedKit connector settings and authentication and
change only the URL path from `/mcp` to the module path (`<gateway>` is the public gateway URL).

- Claude Desktop / claude.ai: Settings → Connectors → add (or edit) the custom connector with URL
  `https://<gateway>/mcp/modules/messaging`. Reconnect it after a change.
- Claude Code: `claude mcp add --transport http deedkit-messaging https://<gateway>/mcp/modules/messaging
  --header "Authorization: Bearer <key>"`.
- Grok (remote MCP tool): `server_url` `https://<gateway>/mcp/modules/calendar,messaging` with the same
  `Authorization` header as for `/mcp`.

Hosts with deferred tool loading (Claude Code tool search) need this less; hosts that load every tool
(Grok, Cursor, some Desktop setups) gain the most.

Module capabilities preserve the backend `principal` and add `backend_principal`
and `gateway_principal`; platform `principal` remains the gateway identity.
Subscription ownership and receipts continue to use the existing backend credential.
A missing backend principal is reported as null rather than inferred from a gateway
label. Safe `backend_tool_manifest` summaries preserve the version, catalog count
and hash, and the reviewed discovery hint without advertising unselected backend
methods or a backend-specific REST URL. Platform introspection returns these as
`backend_tool_manifests` keyed by module.

`gateway_actions` always refers to the `messenger_operations` action group;
`gateway_actions_scope` makes this explicit. Inspect `gateway_tools` for calendar
and calls methods. An empty messenger action list and `owner_decision_write: false`
do not deny a selected calls preparation method. Calls channel, destination, scope
and owned-brief checks remain the backend's authority; these labels grant no access.
`calls_list_briefs` and `calls_list_calls` (`calls:read`) page the backend credential's
own briefs and calls newest first as short summaries; existing profiles do not gain them.

Backend HTTP failures retain only their numeric `backend_status`, a reviewed
`backend_error` classification (including `query_timeout`), and a UUID-shaped
`request_id` when supplied. Error bodies, exception text and transport details are
not forwarded as diagnostics. MCP and REST return the same safe error. A failure
after a mutation is dispatched still reports `mutation_outcome_unknown` and forbids
automatic retries, even when a backend error classification is available.
Calendar capabilities retain a reviewed `constraints` allowlist (write limitations,
including `occurrence_write`, `all_day_write` and `provider_write`; `recurrence_write`, `future_split` and
`invitations` are listed as false, so series-wide, this-and-following and guest writes are never advertised;
accepted time format, freshness, bounded refresh and request limits), while upstream
feature/tool catalogs remain hidden. `gateway_tools` is the selected connection's operation ceiling;
`gateway_actions` describes messenger actions only. Calendar `allowed_event_actions`
also intersects local/backend scopes and requires both selected plan/apply tools.
`write_enabled` alone is not a write grant. `calendar_plan` takes, per action, a timed event (`start`, `end`,
`timezone`), an all-day event (`all_day: true`, date `start`, exclusive date `end`, no `timezone`), or
an `update`/`delete` with the `event_ref` of one occurrence of a series; the backend refuses series-wide
targets and guests with `capability_unverified`, and names a missing timed field in `field_errors`. A `project`
action (`event_ref` of a lesson occurrence, `activity` label) plans linked drives from guardian rules; the gateway
refuses it unless `assignment:read` is in both the local grant and the backend key, the same ceiling as
`guardian_rules` in members, and passes `plan_step_limit` (16) in constraints. An `unproject` action (the lesson's
`calendar_ref` and `event_ref`, or an `operation_id`) plans deleting those drives and follows the same ceiling, as does a `create` with `activity`. Under that
ceiling, and only when the backend declares `features.carry_projections`, the gateway sends
`carry_projections: true` (an agent's own value is dropped), so the backend carries drives with a moved
or cancelled lesson; a plan returned with `link` steps or `projections_skipped` is refused unless the
ceiling holds. Likewise `calendar_agenda` gets `projection_status: true` (drift status of linked drives) only
under that ceiling and when the backend declares `features.projection_status`. Slot requests may add explicitly reviewed
`additional_calendar_refs` to member bindings and return their exact `source_selection`.
Pending refreshes are polled with `refresh=false`; missing or pending coverage is unknown.

`messages_request_file_download` explicitly schedules bounded retrieval of one
existing archive attachment. Select it in the profile and grant `download` locally
and upstream; the backend must advertise `request_file_download` in its manifest.
It does not send messages or mark them read, and returns without waiting for bytes.
Inspect its request/download status and poll `messages_get_attachment` passively.
Repeated requests coalesce; source availability, storage limits and provider backoff
remain in force. Existing profiles do not automatically gain this tool.

Messenger-operation enums also intersect the backend's explicit supported names
for each permitted account. A mixed Telegram/WhatsApp key sees their union; each
preview/execute rechecks the exact account before dispatch. Unsupported operations
are omitted from account catalogs. Missing support names hide live actions rather
than inferring them from a provider name or count.
`messages_capabilities` advertises in `gateway_actions_by_account` (and their union
`gateway_actions`) only actions that are granted, supported and reported in the
backend's per-account `available_operations` (key permitted and account opt-in).
`gateway_actions_excluded_by_account` counts the remaining ceiling actions per
reason (`not_available_for_account`, or `availability_not_reported` for an older
backend) without listing them; `list_messenger_operations` shows per-row reasons.
The operation enum and the per-call ceiling are unchanged by this narrower view;
the backend still enforces account opt-in on every preview and execute. Scoped receipt reads remain
available across provider changes under the existing action permission ceiling.
Upgrade the messaging backend before the gateway when adding this contract.

New file staging is separately selected with `messages_prepare_file_upload`,
`messages_upload_file_chunk` and `messages_get_file_upload`. It requires current
backend manifest support, local and backend `read`, `send`, `upload` grants and
an allowed `send_file` action. The current `send_file` ceiling also requires
`download`, including sends using newly uploaded files; this draft preserves that
existing authorization model. Existing profiles do not gain upload access.
Source bytes are staged for one credential/account/chat; URLs and server paths
are not accepted. Uploading never sends to the messenger or asserts owner approval.
Chunks contain at most 262144 decoded bytes, 349528 base64 characters, and an
exact 360448-byte HTTP/MCP envelope cap. Every other gateway request keeps the
65536-byte cap. A separate upload pool admits 2 active and 4 waiting requests,
with 8 chunks per second per backend credential. Body reads and upstream chunk
transfers each have a 10-second deadline.
Read back the upload's next offset after an uncertain response; writes are not retried.

Subscription consumption and private feedback are separately selected tools.
`messages_fetch_subscription_events` and `messages_wait_for_subscription_events`
return owned pages; only the explicit `messages_ack_subscription_events` with
`subscription-ack` can acknowledge an issued receipt. Delivery configuration and
synthetic receiver probes require `read` plus `subscription-write`; they accept an
opaque operator-registered receiver ID. A hint reaching a receiver does not prove
an agent ran a turn and does not acknowledge any event. Feedback submission requires
`read` plus `feedback-write`, stays private to the exact backend credential and
never publishes an external issue or message. Each new tool also requires an exact
backend manifest entry. Existing profiles gain none of these names automatically.
The `config/profiles/messaging-subscriber.toml` profile (ANYM-518) exposes the
messaging-reader tools plus `messages_create_subscription`,
`messages_update_subscription`, `messages_configure_subscription_delivery` and the
three consumption tools `messages_wait_for_subscription_events`,
`messages_fetch_subscription_events` and `messages_ack_subscription_events`;
messaging-reader is unchanged. The recommended loop is wait, fetch, handle, ACK:
wait up to 25 seconds, fetch a page with its receipt, handle every item, then ACK
that receipt. A crash before the ACK makes the next fetch return the same page.
`messages_capabilities` lists `subscriptions.transports` (`pull`, `long_poll`,
`webhook`) only when this profile exposes every tool of the transport.

Long subscription waits and sends with positive `wait_s` use a separate pool with
2 active and 4 waiting requests; ordinary messaging reads retain their own pool.
Both waits are bounded to 25 seconds. Cancellation releases waiting capacity.
When this pool is full, `messages_wait_for_subscription_events` returns
`wait_status: "busy"` with `retry_after_s` and the pool size instead of a bare 503;
the `long_poll` transport in capabilities shows the same `wait_pool`.
`messages_get_updates(start_from="now")` requires `cursor-write` both locally and
upstream, initializes only a new consumer, and cannot reset an existing consumer's
ACK state. The legacy default remains `start_from="beginning"`.

Original files are streamed through bounded, short-lived bearer tickets with separate
connection pools. URLs cannot select another origin or path. A ticket's expiry limits
when a download may start; a started transfer may outlive it. Throughout the transfer
the gateway rechecks current local policy and credential binding, and polls the
backend's existing HEAD download grant check. A denial or unexpected answer ends the
transfer at once; 429, 5xx, timeouts and connection errors end it only after 10
seconds without a confirmation. For a transfer it is serving, the messaging backend
answers that HEAD from the current key, scope and file instead of the grant's
120-second expiry; upgrade it first, or long transfers still end there.
Revocation terminates an active response; a partial file is never converted to a
successful error JSON. The final chunk is withheld until length and SHA-256 match, so
a corrupted original ends short of its Content-Length instead of completing. Clients
should still verify the advertised length and SHA-256. Do not share or log these
bearer URLs. Access logging is disabled.

Deployment profiles in `config/profiles/` select managed or external backends. Run
`python scripts/deedkit.py resolve --profile config/profiles/existing-readers.toml --json`
from the repository root to inspect a plan without starting a process. Resolution is
not a health check or proof that the private policy grants the listed profile tools.

Normal tests use synthetic HTTP doubles and real SDK loopback transport. They do not
read accounts or start provider sessions. Full acceptance also requires real isolated
backend tests, load budgets, server restore evidence and Tiden readback; see
`docs/quality/platform-acceptance.md` at the repository root.
