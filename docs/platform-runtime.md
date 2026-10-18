# DeedKit module runtime

The platform lives in the existing repository. Gateway, calendar and calls have
separate Python environments and locks. Messaging retains its package/import/CLI
names, schema and original test identities. `config/stack.toml` checks the shared
resolved versions without installing a union of provider dependencies.

## Selecting modules

Run `python scripts/deedkit.py resolve --profile config/profiles/calendar-reader.toml --json`
to inspect the deployment. `up`, `status` and `stop` use explicit module Compose
projects and services. Stop never removes volumes. Mark an existing backend as
`external` to use its configured endpoint without starting another database or
provider session. The phone-only profile does not require messaging. Telegram calls
use the existing session owner's broker through the optional calls-telegram overlay.

For an existing reverse proxy, select the `existing-ingress` connector and set
`DEEDKIT_INGRESS_NETWORK` in the private gateway environment. The configured public
URL may include a fixed path prefix such as `/deedkit`; the proxy strips that
prefix, preserves Host and disables access logging of temporary file URLs.
The private messaging `download_origin` can likewise include its existing prefix.
Only that exact prefix is removed from verified download grants; arbitrary paths
or redirects are never forwarded.

The launcher plan is deployment selection. The private gateway policy is the
authority for exposure: its exact profile tool names, principal grants and backend
permissions must all allow an operation. An empty profile stays empty. A disabled
or unavailable backend never silently falls back to another account or provider.

For generic messenger operations, both the profile and principal must explicitly
list actions under `messenger_operations`. Their intersection is further restricted
by current backend permissions. `rename_group` does not allow `leave_group` even
though both use the backend `groups-manage` permission. MCP schemas and operation
discovery show the same ceiling; REST applies it too. Backend account opt-in and
payload-bound preview digests remain authoritative at execution.

## Persistent state

Calendar requires an explicit existing data directory containing role-specific
configuration. Only its worker mounts the credential vault. The API and gateway
do not receive provider credentials. A PostgreSQL session advisory lock rejects a
second worker before job recovery or provider access. Calendar DB volumes are
explicit external volumes, so an accidental project rename cannot silently create
an empty replacement database.

Calls uses `VOICE_DATA_DIR` (or the stable user-state path under
`~/.local/share/deedkit/calls/data`), never a path computed from source checkout
parents. Initialize a new private directory explicitly before starting; migrations
must point to the existing state. The Compose bind refuses to create a missing
directory. Old browser/owner tasks retain their legacy boundary.

Calls agent API credentials are separate from the owner control token. An atomic
private `VOICE_AGENT_REGISTRY` JSON file contains version 1, a monotonically
increasing revision and principals with ID, SHA-256 key digest, enabled flag,
timezone-aware expiry, scopes, channels and, for SIP only, exact destination
allowlists. Phone, WhatsApp and Telegram have no recipient list (ANYM-288): an agent
may call a destination the owner named or one it found for the owner's task.
`dial_call` carries the owner's own words (`owner_instruction`) and the destination's
origin (`owner_named`, which the words must contain, or `agent_found` with evidence);
messages, files, web pages and model output never authorize a call. Emergency numbers
and short codes fail fast with a call-it-yourself message, invalid numbers are refused,
and every dial spends units of a durable cap of 10 per rolling 24 hours (premium-rate,
shared-cost and UAN numbers cost 3). Country limits stay in Twilio Geo Permissions. Brief
ownership is assigned from the authenticated principal before snapshot hashing.
Calls, results, decisions and followups inherit it. No agent tool can submit an
owner decision or read/activate global learned prompts. Optional archive context
uses that principal's separate private archive configuration; it never falls back
to the owner archive credential. Phone preparation works without an archive.
With `calls:read`, `list_briefs` and `list_calls` page only the principal's own items
newest first as short summaries without notes, facts, prompts or destinations.
Capabilities report each granted channel's configured state (from settings only, no
provider contact), the recipient policy (SIP: allowlisted recipient counts),
the daily call cap with the owner's active raises (`raise_call_cap`, ANYM-353),
`daily_calls_remaining` and the enforced request limits.

## Outcomes and verification

Preview, plan and brief preparation do not authorize real external actions. The
acting agent still needs the owner's concrete instruction. Domain request IDs,
digests, receipts, expected revisions and unknown outcomes pass through unchanged.
A transport failure after a mutation dispatch is explicitly reported as unknown;
the gateway does not resend it or invent a new request ID.

Original files use bounded, short-lived gateway bearer links. Their expiry bounds
when a download may start, not how long a started transfer may run. SHA/length are
checked while streaming, and the final chunk is withheld until both match, so a
mismatch truncates the response rather than completing a 200. Gateway revocation is rechecked every chunk and by a 250 ms
watcher; backend access is revalidated with the authenticated backend grant's HEAD
route at one-second intervals with a two-second timeout. A 401/403/404, redirect or
other unexpected status aborts at once. Rate limiting (429), 5xx, timeouts and
connection errors are tolerated until ten seconds pass without a confirmation, so a
busy backend does not cut a transfer but cannot hide a revocation for longer than
that. Messaging records the
binding of each grant whose GET it admitted. While that GET is sending, and for ten
minutes afterwards (relay buffers may still be draining to a slow client), HEAD
checks the current key, download permission, scope version and file instead of the
grant's 120-second TTL. A new GET always needs an unexpired grant. This record is
process-local; a messaging restart ends those transfers anyway. Failure aborts the
stream, including a stalled client.

Every agent behind one gateway reaches messaging from the same address, so
messaging charges authenticated requests to their principal, per minute: 180
general calls, 600 `capabilities` reads (the gateway's pre-check before each call)
and 256 upload chunks (two 32 MiB uploads, across REST and MCP). Only
unauthenticated work counts per peer address: download grant requests and missing
or rejected credentials, 600 per minute, checked before any database access. A peer
that keeps presenting rejected credentials is blocked for the rest of that minute,
including its valid agents. Restart invalidates temporary links. Never log download paths,
credentials, source content or private filenames at the reverse proxy.

CI retains the existing messaging checks and adds module JUnit reports with qualified
source identities. Cross-module tests use real HTTP and isolated synthetic databases.
The load runner saves raw timings for direct REST, gateway REST and MCP, at least
1,000 requests per scenario and three repeats, both isolated and contended. These
measurements concern the gateway and synthetic backends, not provider latency or
successful live calls. Runtime cutover and provider readback require their separate
migration receipts.
