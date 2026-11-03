# Personal install: DeedKit on one rented server

> **For agents:** `AGENTS.md`, `CLAUDE.md`, `docs/operations/` and `scripts/production.py` are the
> maintainers' rules for the GoSync production server. Do not apply them to a personal install
> (no `production.py`, no tailnet backup server, no mandatory backups, no merge queue).

Goal: Telegram + WhatsApp archive, family calendar and one MCP endpoint for your own agent, on
one x86 VPS you rent. Everything below runs as root in `/opt/deedkit`. Steps marked **(you)** are
interactive and stay in your own terminal: pairing, OAuth, passwords.

Checked: the base sequence, the gateway with the messaging backend, and the calendar service
start, on synthetic data. **Not run end to end:** Telegram/WhatsApp pairing, connecting a
calendar account, public TLS. Follow the linked docs for those and report defects.

## 1. Server

x86_64 only (**not ARM**: no arm64 images are published or tested), Ubuntu 24.04 LTS, public IPv4.

| | vCPU | RAM | Disk |
|---|---|---|---|
| Minimum | 2 | 4 GB (+2 GB swap) | 40 GB |
| Recommended | 4 | 8 GB | 80 GB |

Measured idle (database, API, gateway, calendar API and database): about 0.3 GiB. Each collector
is capped at 768 MiB; image builds, the WhatsApp Go build especially, need the headroom. Disk
grows with your media. Add OCR/audio later and size up.

Examples (prices checked 2026-11-03, verify when ordering): OVHcloud VPS-2 (4 vCore / 8 GB / 75 GB,
from $8.50/month), netcup VPS 1000 G12.5 (4 / 8 GB / 128 GB, EUR 14.50/month, 12-month term),
Hetzner Cloud CX33 (4 / 8 GB / 80 GB) if orders are open (Hetzner pauses new orders at times).
Use a CX/x86 plan there, never CAX. Any VPS with these specs works.

```sh
curl -fsSL https://get.docker.com | sh          # Docker Engine + Compose v2
apt-get install -y git python3 ufw caddy
ufw allow OpenSSH && ufw allow 80 && ufw allow 443 && ufw --force enable
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

PostgreSQL and the API have no public port (loopback only); Docker does not bypass this because
the compose files bind to `127.0.0.1`.

## 2. Messaging core

There is no release tag newer than 0.9.9, and that tag predates the gateway, calendar and native
WhatsApp. Use `main`.

```sh
git clone https://github.com/GoSync-Inc/deedkit.git /opt/deedkit && cd /opt/deedkit/deploy
cp .env.example .env
python3 - <<'PY'
from pathlib import Path
import os, secrets
p = Path('private'); p.mkdir(mode=0o700)
owner, api, col = (secrets.token_urlsafe(32) for _ in range(3))
files = {
 'postgres.env': f'POSTGRES_USER=probe_owner\nPOSTGRES_DB=messaging_probe\nPOSTGRES_PASSWORD={owner}\n',
 'owner.dsn': f'postgresql://probe_owner:{owner}@db:5432/messaging_probe\n',
 'app.dsn': f'postgresql://anymsg_api:{api}@db:5432/messaging_probe\n',
 'collector.dsn': f'postgresql://messaging_collector:{col}@db:5432/messaging_probe\n',
 'bootstrap.sql': f"CREATE ROLE anymsg_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '{api}';\nGRANT messaging_gateway TO anymsg_api;\nALTER ROLE messaging_collector PASSWORD '{col}';\n",
}
for name, value in files.items():
 fd = os.open(p/name, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
 with os.fdopen(fd, 'w') as f: f.write(value)
for name in ('owner.dsn', 'app.dsn', 'collector.dsn'): os.chown(p/name, 10001, 10001)
(p/'issued-keys').mkdir(mode=0o700); os.chown(p/'issued-keys', 10001, 10001)
PY
R=../services/messaging/messaging_service
docker compose build api && docker compose up -d db
docker compose -f compose.yaml -f compose.operator.yaml run --rm operator migrate
for f in $R/gateway_role.sql $R/collector_role.sql private/bootstrap.sql; do
  docker compose exec -T db psql -v ON_ERROR_STOP=1 -U probe_owner -d messaging_probe < $f; done
rm private/bootstrap.sql
```

Put the messaging API on the gateway's network, then start it (`.env` makes this the default file set):

```sh
docker network create deedkit-integration
echo 'COMPOSE_FILE=compose.yaml:compose.personal.yaml' >> .env
docker compose up -d api
```

## 3. Telegram and WhatsApp (native)

Follow [COLLECTORS.md](../COLLECTORS.md) steps 2 to 6 and [whatsapp-native.md](whatsapp-native.md),
skipping step 1 (the backup gate is a production rule). Condensed:

1. Register each account: `docker compose -f compose.yaml -f compose.operator.yaml run --rm operator register-account --account telegram-primary --platform telegram --label Telegram --discover-chats` (WhatsApp: `--platform whatsapp`).
2. Copy `telegram*.example.json` / `whatsapp.example.json` / the permit examples to `private/` (names in `compose.collectors.yaml` and `compose.whatsapp.yaml`), fill in your IDs, create `private/telegram-session`, `private/whatsapp1-session` as `chown 10001:10001`, mode 0700, files 0600. Telegram needs `api_id`/`api_hash` from my.telegram.org.
3. Build the WhatsApp image once: `docker build --target whatsapp-native -t anymsg-whatsapp-native:0.9.19 ..`
4. **(you)** Pair in your own terminal: `docker compose -f compose.yaml -f compose.collectors.yaml run --rm --no-deps telegram python -m messaging_service.telegram_pair --config /run/secrets/telegram_config`; WhatsApp: `docker compose -f compose.yaml -f compose.whatsapp.yaml -f compose.whatsapp-native.yaml -f compose.whatsapp-pair.yaml run --rm --no-deps whatsapp1` (scan the QR). Never run a second client on the same session.
5. Set `"enabled": true` and `"prior_clients_stopped": true` in the configs, then switch collectors on:
   in `.env` replace the `COMPOSE_FILE` line with `COMPOSE_FILE=compose.yaml:compose.personal.yaml:compose.collectors.yaml:compose.whatsapp.yaml:compose.whatsapp-native.yaml`, add `COMPOSE_PROFILES=collectors,whatsapp` (only the ones you paired), then `docker compose up -d`.

## 4. Calendar

Needs a database volume, state files and the shared network. Details: [services/calendar/docs/SERVICE.md](../services/calendar/docs/SERVICE.md).

```sh
cd /opt/deedkit/deploy
python3 - <<'PY'
from pathlib import Path
import json, os, secrets
s = Path('private/calendar-state'); (s/'service'/'vault').mkdir(parents=True, mode=0o700)
pw = secrets.token_urlsafe(32)
(s/'service'/'postgres.env').write_text(f'POSTGRES_USER=fcs_owner\nPOSTGRES_DB=family_calendar\nPOSTGRES_PASSWORD={pw}\n')
(s/'service'/'runtime.json').write_text(json.dumps({'dsn': f'postgresql://fcs_owner:{pw}@calendar-db:5432/family_calendar', 'port': 13232}))
for root, dirs, files in os.walk(s):
 for n in dirs: os.chown(Path(root)/n, 10001, 10001); os.chmod(Path(root)/n, 0o700)
 for n in files: os.chown(Path(root)/n, 10001, 10001); os.chmod(Path(root)/n, 0o600)
os.chown(s, 10001, 10001); os.chmod(s, 0o700)
PY
cat >> .env <<EOF
CALENDAR_STATE_DIR=/opt/deedkit/deploy/private/calendar-state
CALENDAR_DATABASE_VOLUME=deedkit_calendar_db
EOF
docker volume create deedkit_calendar_db
C="docker compose -f compose.calendar.yaml"
$C up -d calendar-db && $C build calendar-api
cal() { docker run --rm -v /opt/deedkit/deploy/private/calendar-state/service:/data/service --network deedkit-calendar_calendar-backend deedkit-calendar:0.1.0 "$@"; }
cal migrate && cal db-roles && cal db-roles --check
cal init-family --owner owner --label Owner --group family
```

`db-roles` writes `gateway.json`/`worker.json`. Edit `gateway.json`: add `"bind": "0.0.0.0", "allowed_hosts": ["calendar-api:13232"]` (the gateway reaches the service by that name). Run `$C up -d calendar-api`.

**(you)** Connect an account (iCloud app password or Google OAuth) per SERVICE.md, then `$C up -d calendar-worker` (it refuses to start before a connection exists). Issue the gateway's calendar key with `cal agent-key --id gateway --member owner --calendar REF --scopes agenda:read,availability:read --output /data/service/keys/gateway.key` (`REF` from the discover output; run `grant` first; this key step was not run here). Google's OAuth callback listens on a random port inside the container, so iCloud is the simple path in Docker.

## 5. Gateway and your agent

One MCP endpoint, a read-only tool profile, one key for your agent. Issue the messaging backend key, then generate the policy (rerun after adding the calendar key: it bumps `revision` and picks up whichever backend keys exist):

```sh
docker compose -f compose.yaml -f compose.operator.yaml run --rm operator issue-key --principal gateway --accounts telegram-primary whatsapp-owner-1 --permissions read download --days 90 --key-file /operator-private/gateway-messaging.token
python3 - <<'PY'
import hashlib, json, os, secrets
from pathlib import Path
priv = Path('private'); h = lambda t: hashlib.sha256(t.strip().encode()).hexdigest()
catalog = json.load(open('../services/gateway/src/agent_gateway/catalog.json'))
sources = {'messaging': priv/'issued-keys'/'gateway-messaging.token', 'calendar': priv/'calendar-state'/'service'/'keys'/'gateway.key'}
backends = {'messaging': {'url': 'http://anymsg-api:8080', 'host_header': '127.0.0.1:18940'}, 'calendar': {'url': 'http://calendar-api:13232'}}
mods = [m for m, f in sources.items() if f.exists()]
tools = [t for t in catalog if t['module'] in mods and t['annotations'].get('readOnlyHint')]
grants = {m: sorted({s for t in tools if t['module'] == m for s in t['scopes']}) for m in mods}
keys, pdir = priv/'gateway-keys', priv/'gateway-policy'
for d in (keys, pdir): d.mkdir(mode=0o700, exist_ok=True)
creds = {}
for m in mods:
    text = sources[m].read_text(); (keys/f'{m}.key').write_text(text)
    creds[m] = {'key_file': f'/run/deedkit/keys/{m}.key', 'key_sha256': h(text)}
agent = priv/'agent-gateway.token'
if not agent.exists():
    with os.fdopen(os.open(agent, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600), 'w') as f: f.write(secrets.token_urlsafe(48) + '\n')
live = pdir/'policy.json'
rev = json.load(open(live))['revision'] + 1 if live.exists() else 1
policy = {'version': 1, 'revision': rev, 'deployment': {'modules': mods, 'backends': {m: backends[m] for m in mods}},
  'profiles': {'default': {'tools': ['platform_capabilities'] + [t['name'] for t in tools]}},
  'principals': [{'id': 'my-agent', 'key_sha256': h(agent.read_text()), 'profile': 'default', 'enabled': True,
    'expires_at': '2099-01-01T00:00:00Z', 'grants': grants, 'credentials': creds}]}
tmp = pdir/'policy.json.new'; tmp.write_text(json.dumps(policy, indent=1)); tmp.rename(live)
(priv/'gateway.env').touch(mode=0o600)
for path in [keys, pdir, *keys.iterdir(), live]: os.chown(path, 10001, 10001); os.chmod(path, 0o700 if path.is_dir() else 0o600)
print('policy revision', rev, 'modules', mods)
PY
echo 'DEEDKIT_PUBLIC_URL=https://agents.example.com' >> .env     # your domain
docker compose -f compose.gateway.yaml up -d --build gateway
```

The gateway starts only with a valid policy; `--check` shows why not. `private/agent-gateway.token` is your agent's Bearer key. The profile is read-only: sending, calls and writes stay off until you grant them ([ACCESS.md](../ACCESS.md), [gateway README](../services/gateway/README.md)).

## 6. HTTPS and MCP

Point a DNS A record at the server, then `/etc/caddy/Caddyfile`:

```
agents.example.com {
    reverse_proxy 127.0.0.1:13240
}
```

`systemctl reload caddy`. Your agent connects to `https://agents.example.com/mcp` (Streamable HTTP, Bearer = the contents of `private/agent-gateway.token`). Test: `curl -s -H "Authorization: Bearer $(cat private/agent-gateway.token)" -H 'Content-Type: application/json' -d '{}' https://agents.example.com/v1/messages_capabilities`. Do not log request bodies in Caddy.

## 7. Updates (automatic, daily)

`deploy/personal-update.sh` follows `origin/main` (there is no newer release tag), but only to a commit whose GitHub `CI gate` is green. Each run: refuses if tracked files in the checkout were edited (keep settings in `.env` and `private/`); takes a local `pg_dump` of each database into `/var/backups/deedkit-local` (newest 3 kept, plus one pinned dump per database from before the last successful update that never rotates; this is the only backup in this lite path, a rollback for a bad migration, media files are not included); builds; runs migrations and re-applies the role SQL; `docker compose up -d` for the services that already exist; waits for health; writes one line to `/var/log/deedkit-update.log`. A failure before the containers are replaced leaves the old ones running. A commit whose update failed is not retried (no new dump) until a newer one appears; the log says so. Collectors restart on an update, so their sessions reconnect.

```sh
cp /opt/deedkit/deploy/deedkit-personal-update.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now deedkit-personal-update.timer
/opt/deedkit/deploy/personal-update.sh      # run once by hand to see the result line
```

The timer runs at about 03:30 server time. Settings (`DEEDKIT_TRACK=tag` to follow release tags later, dump directory, keep count) are at the top of the script. Rollback instructions are in the same header. Read the release notes in [SETUP.md](../SETUP.md) when an update fails: some releases need extra steps.

## 8. Optional later

- **Backups are off by default.** When you hold data you cannot lose, read [BACKUP.md](../BACKUP.md) (encrypted dumps to storage you choose). Your VPS provider's snapshot or daily backup is a cheap extra layer.
- Not installed here, add when needed: calls (`services/calls`), recordings (`services/recordings`), OCR/audio enrichment ([ENRICHMENT.md](../ENRICHMENT.md)), sending messages ([SENDING.md](../SENDING.md): needs explicit opt-in per account and key).
