# Ready-before-route HTTP releases

This opt-in protocol releases the **gateway and messaging HTTP API as one
generation**. A source merge, successful CI and production promotion are separate
events. Enrollment and live acceptance remain in [DK-41](../../BACKLOG.md).
The existing operator handles explicitly scheduled singleton updates.

## Release sequence

1. `prepare` requires the exact current `main` SHA and its latest successful push
   CI attempt. It fetches clean source, obtains immutable images through
   `production.py prepare` (the CI-published images are pulled by verified digest,
   so the host needs the installed attestation verifier and GHCR access;
   [production.md](production.md#attestation-verifier)) and saves private
   configuration snapshots without starting application containers.
2. `promote` rechecks image, configuration, container and router identities. Old
   and new messaging images must have identical SQL inventories. It starts a
   uniquely named gateway/API pair beside the serving generation, without
   inherited host ports or provider-owner dependencies.
3. Candidates remain `warming`: ordinary HTTP fails closed and upload maintenance
   stays paused. Private readiness requires successful ASGI startup. The API
   checks the database version and every migration checksum in a read-only
   transaction; the gateway checks policy and passive HTTP backend health.
4. After both candidates are ready, activation and a final exact-main/CI check,
   the operator changes the internal release router with an ETag condition.
   Failed startup or readiness leaves the serving generation selected.
5. The old gateway stays active until requests already selected by the router
   have completed. It then drains ordinary admissions while serving issued file
   tickets. After zero active requests and zero unexpired tickets, the operator
   fences further issuer admissions in the router, waits for all router leases
   to finish and stops the gateway. Its paired API drains and stops afterward.
   A timeout retains the processes and records pending retirement.

Request accounting includes uploads, response backpressure and entire streamed
responses. The router chooses a target and acquires its request lease under the
same mutex as configuration changes. A request with slow headers chooses the
current generation when its handler starts. Closing an issuer's admission fence
uses that mutex too; a zero counter cannot race a new old-generation admission.

A gateway stays paired with its API throughout draining because download
revocation checks use process-local API bindings. New gateway links contain
`/_releases/<generation>/v1/files/<token>` under the existing public prefix.
The router strips that generation prefix and sends the request to its issuer.
Token format, permissions, original bytes and revocation checks stay intact.
Ticket TTL limits admission; it never kills an admitted transfer.

## Scope and compatibility

Only the gateway and messaging `serve` processes overlap. Telegram, WhatsApp,
calendar API/worker, voice, indexer, subscriptions, enrichment and database
containers retain their current owners. The gateway keeps the original read-only
policy bind, so ACL and credential revocation reaches serving and draining
instances. A strictly generation-bound messaging dial override selects its paired
API while preserving the original Host, public download origin, consumer
identities and all other module targets.

Updating singleton code still needs its own deployment and applicable session or
call authority. Schema changes require a separate reviewed compatibility and
backup plan; this operator refuses differing SQL inventories before overlap.
Never use a migration or provider command as readiness.

## Internal router and first enrollment

The dedicated [release router](../../services/release-router) uses Go's standard
HTTP server and reverse proxy. Its stable listener and atomic admission counters
provide the retirement barrier. It has no public published ports, joins only the
existing ingress network and exposes administration through a private Unix
socket. Its private parent configuration directory is bind-mounted so persisted
JSON can be replaced atomically. It runs from an immutable image built from
reviewed, verified `main` source.

The shared public Caddy handles HTTPS and voice. A running container does not
establish a reproducible deployment or a working restart path. Routine promotions
never reload it. Reloading Caddy alone does not prove its previous HTTP server
has finished accepting work, so its upstream counters cannot safely authorize
backend retirement.

Before first enrollment, prepare and review a reproducible private ingress
manifest. It must pin the inspected immutable image and preserve the current
command, configuration mounts, published listeners, existing certificate/state
volumes and networks. Declare retained volumes and networks as external with
their exact existing names, so a different Compose project cannot silently
substitute empty certificate storage or a new network. Include a persistent
restart/startup policy; a container configured never to restart is an unresolved
enrollment prerequisite. A Compose label referring to a missing source file is
not a usable deployment manifest.

Resolve that manifest and compare it with inspected live identities, mounts,
networks, ports and restart settings. Validate the proxy configuration without
recreating or reloading the serving listener. Record the reviewed manifest and
validation evidence privately. Any required live restart or reboot acceptance
belongs to a separately scheduled operation with the applicable call authority;
document review alone does not prove restart recovery.

`bootstrap --sha <verified-main-sha>` checks main/CI, the private clean-build
receipt and the router image's source and revision labels, then generates initial
JSON and Compose artifacts. It starts nothing and changes no ingress. After the
enrollment prerequisites are satisfied, review and start only that internal
router, then verify its initial routes still reach the current healthy services.

The one-time ingress conversion must preserve Host and pass `/deedkit/*` and
`/pos-probe/*` to the router **without stripping their prefixes twice**. Preserve
voice and unrelated configuration. The legacy ingress has no trusted drain
protocol for active voice WebSockets: establish quiescence and prevent new call
admissions for this conversion window, or review a separate transport migration.
Synthetic tests do not establish that live quiescence.

If current and candidate SQL inventories differ, complete a separate reviewed
compatibility and migration plan before enrollment. Verify that the pinned
backup and restore runtimes support the current database schema and the planned
target schema. Resolve receiver capacity or delivery failures such as
`receiver_quota` through a reviewed storage/retention operation that preserves
the protected restore anchor. Then obtain a fresh compatible pre-migration
snapshot with confirmed durable delivery. Retain its ID, snapshot time, schema,
image revision, ciphertext size/hash and receiver receipt privately, and verify
restore integrity in an isolated quarantine as described in
[backup and recovery](../../BACKUP.md). Measure freshness from snapshot time,
not from a later retry or receipt timestamp. A failed delivery, a local-only
snapshot or an old successful receipt does not satisfy this migration gate.

These are live enrollment and migration prerequisites for the operator to
verify and record. The release scripts do not enforce or attest all of them;
successful source CI, router preparation and bootstrap artifacts do not prove
ingress reproducibility, restart persistence, backup freshness or durable
delivery.

Legacy gateway/API containers lack trusted lifecycle sockets. Missing evidence
never means zero requests. Their unprefixed `/deedkit/v1/files/*` route remains
pinned to the legacy gateway and their pair remains running until quiescence is
established separately. Later managed generations can drain automatically.

## Operator configuration and commands

Use a private operator-owned configuration based on
[the example](../../deploy/seamless-release.example.json). Inspect real Compose
paths, external networks, policy mounts and legacy aliases on the target host.
The example contains placeholders and authorizes no deployment.

On the production host, prepare the router with
`python3 scripts/production.py prepare-router --sha <40-character-main-sha> --config <private-config>`.
This command checks main/CI, fetches main into a new private source directory,
builds and smoke-checks the router, then checks main/CI again. It writes
`<state_directory>/router-<sha>/router-build.json` only after success. Record the
returned immutable image ID in the private configuration before `bootstrap`.
Bootstrap requires this private receipt to match its selected SHA and image;
revision labels alone cannot prove clean source. `check-image` is a local
diagnostic and cannot authorize enrollment. Preparation starts no serving units.

The [operator](../../scripts/seamless_release.py) exposes `bootstrap`, `prepare`,
`promote`, `retire` and `status`; use `--help` from the reviewed checkout. Commands
coordinate with both the canonical host deployment lock and scoped state lock.
Keep resolved Compose/policy files, socket directories and journals out of Git
and distribution artifacts.

Application control is newline JSON on each process's private UID10001 socket
at `/run/deedkit/release/control.sock`. Protocol 1 identifies exact SHA,
generation, phase, readiness, active requests and pending tickets. `drain` is
irreversible. Router control is HTTP on its private socket, with bounded JSON,
ETag compare-and-swap, request census and atomic issuer retirement fences.
Neither control surface is available through public HTTP or agent tools.

## Failure and recovery

- Before routing changes, failures leave the serving generation selected. Only
  the new, unrouted containers belong to candidate cleanup.
- Changed main/CI, images, source policy, container identities, router ETag or
  persisted configuration require inspection and fresh preparation.
- An unknown routing or fence mutation outcome requires comparing live state,
  persisted JSON and the attempt journal. Never blindly retry or restore an
  older configuration over a concurrent change.
- Long downloads retain their predecessors. Resume retirement after completion;
  never force-stop them to satisfy a timeout.
- Rollback is a new reviewed routing decision. Drained processes cannot simply
  reactivate; an image rollback implies no database or state rollback.

Synthetic CI verifies ordering, isolation, request leases, slow-header handoff,
issuer routing, streamed transfers, admission fences, drift and failure receipts.
It does not prove public ingress enrollment, live provider health or calls,
compatibility of a future schema migration, or a production promotion.

See also: [verified production operator](production.md),
[platform cutover](platform-cutover.md).
