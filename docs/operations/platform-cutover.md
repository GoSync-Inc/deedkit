# DeedKit operations and cutover

The repository contains one platform, while module processes, locks and state remain independent. See [runtime policy](../platform-runtime.md), [verification](../quality/platform-acceptance.md) and module READMEs. The existing `anymsg` package/import/CLI and npm name remain compatible.

Production upgrades use the [verified promotion procedure](production.md): only
a `main` commit with successful required CI may be prepared/applied.
The launcher below is for explicit module setup and local operation; it does not
provide the production revision/CI gate.

## Select and start

Inspect a profile before running it:

```sh
python scripts/deedkit.py resolve --profile config/profiles/calendar-reader.toml --json
python scripts/deedkit.py up --profile config/profiles/calendar-reader.toml
python scripts/deedkit.py status --profile config/profiles/calendar-reader.toml
python scripts/deedkit.py stop --profile config/profiles/calendar-reader.toml
```

The launcher uses Docker Compose and checks private files only for selected managed modules. Prepare role-specific configuration, an explicit restored/initialized calendar database volume, private state and the integration network before `up`; it never creates credentials or bootstraps a missing state directory. Module manifests name required files and environment files. Calendar-only needs no messaging/calls/Node runtime. Use `messaging-reader.toml` or `calls-phone.toml` for those selections. `calls-telegram` explicitly requires messaging's existing session broker; never start a second Telegram session. An `external` backend is routed but not launched. Existing reverse proxies can use the `existing-ingress` connector.

## Access and consumers

Deployment selection and agent exposure are separate: a profile's exact tools, principal grants and backend permissions must all agree. Copy the reviewed sample gateway policy, bind private backend key files with their SHA-256 digests, and use a separate incoming agent key. Protect private policy/key files and their directory; keep them outside Git and images. Generic messenger operations also require exact action allowlists in both profile and principal.

Connect an MCP client to the gateway HTTPS `/mcp` endpoint with its scoped bearer key. Validate initialize/list/call using that credential. Removing methods or disabling a principal takes effect without reconnect; reconnect clients that cache schemas after expanding a profile. Increment policy revision on each atomic replacement. Changing backend deployment addresses requires a gateway restart. Retain the existing backend credential when moving a consumer so subscription ownership and cursor identities survive. Do not substitute an owner/admin key.

Calls has a separate principal registry and per-principal archive credential. Keep legacy owner tasks at their original boundary; do not adopt them into an agent identity. Preparing a task does not authorize sending, inviting or dialing. Follow the domain's request IDs, expected versions and unknown-outcome receipts; do not create a replacement request ID to retry an ambiguous external action.

## Backup and migration

Record source revisions, image IDs, Compose/config paths and volume identities before deployment. Keep encrypted backups outside the repository with restricted permissions. For calendar, stop the old worker and API, disable their autostart, take a consistent PostgreSQL dump plus roles and the credential vault/key, and restore into a separately named explicit volume. Compare every table's ordered-row hash/count and verify all encrypted connections can be opened before starting one new worker. Its session advisory lock is mandatory. Provider readback verifies connectivity; it does not prove missing family accounts are covered. Never mark unknown family availability free.

For calls, preserve the existing state directory, provider settings and owner control credential. Stop only that owned service, take a SQLite online backup, then deploy the new image against the same directory. Compare prior call IDs and bodies. Do not enable calling or change destination allowlists as a health check. Messaging collectors, session owners and database stay on their existing volumes while the common gateway is introduced.

## Verification and rollback

Run the real module CI, installed distributions and load budgets. Tiden receives exact checked-out SHA/branch outcomes including failures and skips; source imports or equal totals are not preservation evidence. Compare full stable Tiden exports, preserving old object IDs, links and historical source anchors. Source path metadata may move; old commit anchors remain valid history. Keep the same GitHub repository ID and Tiden product during rename, then check App processing, main CI/gate, publisher trust and consumer links separately.

Verify the common HTTPS MCP with read-only capabilities and provider reads after deployment. Keep legacy endpoints during the compatibility period. Roll back a client to its prior scoped endpoint independently of module state. For a module rollback, stop its new worker before restarting the pinned previous image/config against the same state. If server calendar has processed writes or refreshed tokens, take a current backup and restore it to the former runtime first; do not restart a stale Mac copy. Keep the old source/backup until acceptance. Never remove volumes with `down -v` or prune unrelated images/processes.

For a file ticket, the proxy must strip only the configured fixed prefix, retain Host, disable URL-bearing request/response logs and pass original bytes. Expiry/revocation can abort future streaming; already-delivered bytes cannot be recalled. A gateway restart invalidates its temporary tickets without changing backend data.
