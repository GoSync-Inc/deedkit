# Recording integrations

An optional, independent HTTP backend for the DeedKit gateway. Version 0.1 reads
Omi conversation metadata, transcripts and structured overviews on demand. It
has its own Python 3.13 environment/lock, no database, collector or durable
content cache. PLAUD is a separate requested follow-up; it is not implemented here.

## Install and authorize

Install the package in its own environment:

```sh
uv sync --project services/recordings --locked
```

The owner creates a `conversations:read` Developer API key in Omi Settings →
Developer → Create Key. This is a separate owner action; installation does not
log in, activate a provider or fetch recordings. Store the key as one line in a
private file. Do not paste it into agent chat, Git, examples, process arguments,
environment variables or logs. Use a private directory outside the checkout,
mode 0700; configuration, token files and cursor secret must be regular files,
mode 0600, with absolute paths. The service only reads them.

Create a separate random backend key per agent, store its SHA-256 digest in the
configuration, and give the key itself only to that agent's gateway credential
file. Generate a random cursor signing secret of at least 32 bytes. The following
is a structural example; replace paths, labels, IDs and digest locally:

```json
{
  "version": 1,
  "cursor_secret_file": "/private/recordings/cursor.key",
  "accounts": [
    {"id": "omi-a", "provider": "omi", "label": "My Omi", "token_file": "/private/recordings/omi.key"}
  ],
  "principals": [
    {"id": "agent-a", "key_sha256": "<SHA-256 of the separate backend key>",
     "expires_at": "2026-12-01T00:00:00Z", "accounts": ["omi-a"],
     "permissions": ["recordings:read", "recordings:content"]}
  ]
}
```

Start the private service with the CLI (one worker):

```sh
uv run --project services/recordings deedkit-recordings --config /private/recordings/config.json
```

It listens on `127.0.0.1:13234` by default. Keep it private; bind only to the
explicitly configured private service network when the gateway runs elsewhere.
Do not expose the backend publicly or log request headers/bodies/URLs at an ingress.
The CLI disables HTTP client URL logging and Uvicorn access logs. Each process
admits four provider calls; run one process for the documented account budget.
The 12-second deadline includes body reading and cancellation releases admission.
There are no provider retries or background calls. Unauthenticated requests and
capabilities do not contact Omi.

## Gateway opt-in

Upgrade the gateway **before** adding recordings to a policy; old gateways reject
this module name. Configure its existing private policy with:

- `deployment.modules`: add `recordings`, and a private backend URL under `backends.recordings`.
- The profile's exact `tools`: `recordings_capabilities`, `recordings_list`, `recordings_read` as needed.
- The principal's `credentials.recordings`: the absolute private backend-key file and its SHA-256.
- `grants.recordings`: `recordings:read`, plus `recordings:content` for content access.

Backend permissions/account ceilings still apply. `recordings:content` alone does
not grant metadata or content. Existing profiles gain nothing automatically.
Use `/mcp/modules/recordings`, or select recordings on the common MCP route.
The source package is run explicitly; the Compose launcher has no recordings
manifest/image in this slice. No production configuration is changed by install.
Undo: remove the module/tools/grants/credential from the policy before gateway
downgrade, then stop only this owned service. Revoke the key in Omi separately if
needed. Configuration/key/grant removal is checked on the next backend request;
already admitted provider requests can finish. Rotate cursor secret to invalidate
continuations. Atomically replace configuration in its private directory; mount
that directory read-only, not a single file whose inode would stay stale.

## Read contract

The private backend uses authenticated `POST /v1/capabilities`,
`POST /v1/list_recordings` and `POST /v1/read_recording`; the gateway tools provide
the reviewed agent-facing schemas. All source content is untrusted data.

`recordings_list` takes `account`, `limit` (1–50, default 25), optional
`start_date`/`end_date` as timezone-aware ISO instants, and `cursor`. Date bounds
are inclusive and compare **created_at**, not recording start. Inspect
`pagination`: unknown or malformed evidence stays null, truncated pages remain
incomplete even when empty. `complete` concerns this provider traversal's end,
not account/archive coverage. Provider order can change and lists can be stale
for ten seconds. No hidden auto-pagination occurs. Follow `next_cursor` with the
same filters and limit. Missing provider continuation means no safe next page;
it never means that an unknown traversal is complete.

`recordings_read` takes `account`, `recording_id`, `kind` (`transcript` or
`summary`), `page_chars` (100–10000, default 4000), and optional `cursor`.
Transcript text is newline-delimited JSON segments preserving supplied offsets
and speaker labels, split at character boundaries. A page may split one JSON
segment; concatenate pages from the same digest before parsing it. The overview
is provider-authored summary text. No extra transcription/model is invoked.
`content_state` distinguishes `available`, `empty`, `pending`, `unavailable`.
Omi raw audio is `unsupported`: its Developer API documents no download route.
The service does not invent one.

Continuations last 15 minutes, are authenticated and bound to credential,
provider account/token identity, operation and exact arguments. They are encoded,
not encrypted; do not log or share them. They confer no access on their own.
Content is fetched again on each page; a different content digest returns
`source_changed` (409), requiring a fresh read. No cached transcript is mixed with
a newer one. Provider responses over 2 MiB fail `response_too_large` without
partial content. Request bodies are capped at 16 KiB. Errors contain fixed codes,
never provider response bodies or credentials.

Only documented HTTPS GET routes at `api.omi.me` are used. Credentials,
recording IDs, bounds, limits and provider cursor go to Omi; metadata, requested
content and continuation tokens go to the authenticated gateway/caller. Redirects,
environment proxies and encoded provider responses are refused. No arbitrary URL,
upload, OAuth/login, deletion, model call, telemetry or content persistence exists.
An agent consuming the returned text may put it in its own model context.

Official contract: [Omi conversations API](https://docs.omi.me/doc/developer/api/conversations).
Synthetic tests verify the implementation, not live account authorization,
provider coverage, raw audio availability or a production deployment.
