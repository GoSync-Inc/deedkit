# Server setup

The Python package includes server code, SQL migrations and Node adapter sources.
The npm package is a client for an existing server. Neither package silently
installs a database, signs in to a messenger or starts background services.

## A first empty installation with Docker

These steps target a fresh Linux amd64 VPS with Docker Compose and Python 3.
Run the administrative commands as root in a private checkout, not on a machine
that already holds an archive. Clone the tagged release, then enter `deploy/`.
PostgreSQL has no public port; the API binds to loopback on port 18940.

```sh
git clone --branch v0.9.9 https://github.com/GoSync-Inc/deedkit.git
cd anymsg/deploy
cp .env.example .env
```

Keep the local loopback settings for initial testing. For remote agents, choose
an HTTPS domain you own and set its exact values in `.env` before enabling a
reverse proxy. `PROBE_*` names are retained compatibility settings.

Create fresh random database passwords. This writes private files, never prints
passwords, and refuses an existing `private` directory. Keep the owner DSN out
of the API container; it is only for local migration/administration.

```sh
python3 - <<'PY'
from pathlib import Path
import os, secrets
p = Path('private')
p.mkdir(mode=0o700)
owner, api = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
files = {
 'postgres.env': f'POSTGRES_USER=probe_owner\nPOSTGRES_DB=messaging_probe\nPOSTGRES_PASSWORD={owner}\n',
 'owner.dsn': f'postgresql://probe_owner:{owner}@db:5432/messaging_probe\n',
 'app.dsn': f'postgresql://anymsg_api:{api}@db:5432/messaging_probe\n',
 'bootstrap.sql': f"CREATE ROLE anymsg_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{api}';\nGRANT messaging_gateway TO anymsg_api;\n",
}
for name, value in files.items():
 fd = os.open(p/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
 with os.fdopen(fd, 'w') as f: f.write(value)
for name in ('owner.dsn', 'app.dsn'): os.chown(p/name, 10001, 10001)
(p/'issued-keys').mkdir(mode=0o700)
os.chown(p/'issued-keys', 10001, 10001)
PY

docker compose build api
docker compose up -d db
docker compose -f compose.yaml -f compose.operator.yaml run --rm operator migrate
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U probe_owner -d messaging_probe < ../messaging_service/gateway_role.sql
docker compose exec -T db psql -v ON_ERROR_STOP=1 -U probe_owner -d messaging_probe < private/bootstrap.sql
rm private/bootstrap.sql
```

`migrate` creates an **empty** schema. Optional `init` seeds clearly synthetic
Telegram/WhatsApp examples for a throwaway evaluation. Do not run `init` in an
installation with real accounts. Use the owner-only `operator` overlay for both.
The API uses the separate gateway login and cannot migrate or administer roles.

For a synthetic evaluation only:

```sh
docker compose -f compose.yaml -f compose.operator.yaml run --rm operator init
docker compose -f compose.yaml -f compose.operator.yaml run --rm operator issue-key --principal evaluation-agent --accounts telegram-demo whatsapp-demo-1 --permissions read download cursor-write --days 7 --key-file /operator-private/issued-keys/evaluation.token
docker compose up -d api
```

The new `private/issued-keys/evaluation.token` is mode 0600. The issuer does not overwrite
an existing key file. Verify from the server, keeping the token out of argv:

```sh
python3 - <<'PY'
from pathlib import Path
import json, urllib.request
key = Path('private/issued-keys/evaluation.token').read_text().strip()
req = urllib.request.Request('http://127.0.0.1:18940/v1/capabilities', data=b'{"detail":"full"}',
 headers={'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=20) as r:
 data = json.load(r)
 print({'service': data.get('service'), 'synthetic': data.get('synthetic')})
PY
```

For real accounts, register each account with the operator CLI and issue separate
scoped keys to each agent. Read [ACCESS.md](ACCESS.md) and [COLLECTORS.md](COLLECTORS.md).
Provider pairing happens interactively in the owner's terminal. Stop any previous
client first; two processes must never own the same provider session. The example
collector/enrichment profiles start disabled and need private config files and
separate DB logins/grants. Additional accounts use additional independent services.

Optional images are built locally from this checkout; there are no prepublished
AnyMsg container images in this release:

```sh
docker build --target indexer -t anymsg-indexer:0.9.9 ..
docker build --target whatsapp -t anymsg-whatsapp:0.9.9 ..
docker build --target backup -t anymsg-backup:0.9.9 ..
```

Native OCR, transcription, Chromium sandboxing, quotas and encrypted backups need
their own host acceptance checks. A health response is not proof of complete source
history or a restorable backup. The adapter docs describe the remaining checks.
The API, gateway and calendar API Compose healthchecks only show that the HTTP
process answers its health route (`docker ps` shows `unhealthy` when it hangs);
Docker does not restart an unhealthy container, so alert on that state.

## Optional durable subscription worker

Version 0.9.16 adds an opt-in matcher with a separate DB role and five scoped
API/MCP tools. It does not start on installation or inherit permissions from
existing keys. See [SUBSCRIPTIONS.md](SUBSCRIPTIONS.md) for schema/grants, the
Compose overlay, bounds and the distinction between passive event pages and
future push/ACK delivery.

## TLS and remote MCP

Use `deploy/Caddyfile.example` as a template with your domain and an API upstream
reachable from your proxy. Connect the proxy to the `frontend` Compose network,
or use a host proxy at `127.0.0.1:18940`. Do not expose PostgreSQL. Keep temporary
file grants, tokens, QR codes, sessions and message bodies out of proxy logs.
Set `PROBE_PUBLIC_URL` to the HTTPS root and `PROBE_ALLOWED_HOSTS` to the exact
public Host value before starting the API. Attachments derive links from that URL.

MCP: `https://your-domain/mcp`, Streamable HTTP, Bearer auth. REST: the same root
with `/v1/<operation>`. An MCP GET notification channel is not supported; a client
must accept the explicit 405 response. Each agent should have a separate principal.

## Native Python installation

```sh
python3.13 -m venv .venv
.venv/bin/pip install anymsg==0.9.9
.venv/bin/anymsg --help
```

Prepare PostgreSQL 18, a private owner DSN file, a writable originals directory,
then set `DATABASE_URL_FILE` and `PROBE_FILES` before `anymsg migrate`. Each lock wait in its
`schema.sql` replay and in migrations up to 035 is bounded to 2 s (036 and later set their own
bound, and some reset it for their last statements): behind an open transaction on a table it
needs, it exits with `LockNotAvailable` and rolls back as a whole; run it again once that
transaction ends. `migrate` never queues for the replay's share locks on `messages`, `deliveries` and
`download_grants`: it takes them together or not at all. Once it holds them, writes to those
tables wait until it commits, pending migrations included. Apply the
role SQL as owner and switch the gateway to its dedicated DSN before `anymsg serve`.
Backups need PostgreSQL tools and age. OCR needs Poppler and Tesseract. Optional
audio profiles need ffmpeg and their configured runtime/model dependencies.

For new WhatsApp connections, use the native whatsmeow adapter (no Chromium).
Build the `whatsapp-native` target of `services/messaging/Dockerfile`, use
`deploy/whatsapp.example.json`, and add `deploy/compose.whatsapp-native.yaml`
after the ordinary WhatsApp compose file. The default memory budget is 768 MiB
per account, configurable independently from the browser. Existing configs with
no `provider` remain `webjs`; they are never silently converted. See
[the native adapter runbook](docs/whatsapp-native.md) before switching an account.

The Python wheel uses an external `native_executable` for native sessions. Build
it from the pinned source in `whatsapp/whatsmeow` with Go 1.26.9; binaries and
private sessions are never bundled into the cross-platform Python wheel.

For a legacy browser wheel installation, Node sources are bundled but Node modules are not:

```sh
anymsg whatsapp-assets --directory /opt/anymsg/whatsapp-runtime
cd /opt/anymsg/whatsapp-runtime
npm ci --omit=dev --ignore-scripts --no-audit --no-fund
export ANYMSG_WHATSAPP_ASSETS_DIR=/opt/anymsg/whatsapp-runtime
```

Use Node 24 and install Chromium separately. The export requires a new directory
under existing nonsymlink parents; it refuses overwrite and performs no downloads
or login. On macOS use `/private/tmp`, not the symlink `/tmp`, for temporary exports.
Use the same asset override for pairing and the collector. Native Windows servers
and Docker ARM support are not claimed. See the source Dockerfiles for pinned
system dependencies; the Python package alone is not a one-command VPS installer.

### Forwarded message origin

Collectors record where a forwarded message came from (`forward_origin`,
[SEARCH.md](SEARCH.md#forwarded-messages)) only when their environment sets
`ANYMSG_CAPTURE_FORWARD_ORIGIN=1`; it is off by default. The server always
accepts and returns a captured origin. Set it on the Telegram and WhatsApp
collector services only once the release a rollback would return to already
contains forward origin support: an older server rejects such a capture and the
collector skips that message.

### Shared contact cards

Collectors record a shared contact card's names, phone numbers and WhatsApp or
Telegram ID (`contact`, [SEARCH.md](SEARCH.md#shared-contact-cards)) by default
(native WhatsApp, the whatsapp-web.js fallback and Telegram read the same
variable). `ANYMSG_CAPTURE_CONTACT_CARDS=0` on the collector services is the kill
switch for new captures only: contact messages captured after it carry no block,
as before, while cards already in a capture spool or the native journal keep
theirs. The server always accepts, returns and searches a captured card. A server,
collector or drainer older than the contact card release (0f173c2) rejects a
card and quarantines it, and a full quarantine stops the drain, so rollback
targets stay at or after that release. Going older needs its own reviewed
procedure for the pending spools and native journal exports; the variable alone
is not one.

### Provider business and verified flags

Collectors record that a sender is a WhatsApp business (`VerifiedName`) or a
Telegram verified/support account (`sender_is_business_or_verified`, used by the
reply queue demotion in [CONVERSATION.md](CONVERSATION.md#queue-priority-components))
only when their environment sets `ANYMSG_CAPTURE_PROVIDER_FLAGS=1`; it is off by
default. Only a stated flag is captured. Set it on the Telegram and WhatsApp
collector services once the release a rollback would return to accepts the field:
an older server rejects the capture and the collector skips that message.

### Telegram link preview page ID

The Telegram collector records a link preview's Telegram page ID
(`metadata.web_preview_id`, the unsigned decimal form) only when its environment
sets `ANYMSG_CAPTURE_WEB_PREVIEW_ID=1`; it is off by default. Telegram re-crawls a
preview without an edit, so the same message can arrive again with another preview
photo or document at the same provider time. With the same page ID on both sides
and nothing else changed, the archive keeps the stored revision and records no
version conflict; without the ID on both sides it records a conflict, as before.
Set it on the Telegram collector service once the release a rollback would return
to accepts the field: an older server rejects the capture and the collector skips
that message.

### Reply queue upgrade (0.9.17)

Apply migration 027 as owner and reapply `gateway_role.sql` and
`collector_role.sql` before restarting the updated gateway and collectors.
The new owner-read observations and credential-local dismissals are included
in schema-27 backups. Existing migration checksums are unchanged. Preserve
provider session volumes and existing custom collector extensions. Reconnect
clients which cache the tools manifest to discover `dismiss_reply`.
