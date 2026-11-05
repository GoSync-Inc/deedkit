#!/usr/bin/env bash
# Restore the family calendar's protected backup into the scratch project of compose.calendar-restore.yaml, prove it,
# and remove everything again (ANYM-610; BACKUP.md, "Restore the family calendar"). Backup server, as root:
#   DIGEST=sha256:<attested calendar image digest> bash calendar-restore.sh
# It stops at the first failure; teardown runs on every exit once it holds the verifiers' host lock. It prints no
# password, token or row: stderr that can hold one (psql's role step, pg_restore) goes to a private file, and the
# containers log nothing. The worker never starts and no step calls a provider.
set -euo pipefail
umask 077

: "${DIGEST:?Set DIGEST to the calendar image digest verified with gh attestation verify}"
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${CALENDAR_RESTORE_ROOT:-/srv/backups}
CONFIG=${CALENDAR_RESTORE_CONFIG:-/etc/deedkit-calendar-backup-r2}
LOCK=${CALENDAR_RESTORE_LOCK:-/var/lib/deedkit-backup-verify/host.lock}
RECEIVER_PYTHON=${CALENDAR_RESTORE_PYTHON:-/opt/deedkit-backup-r2/venv/bin/python}
PROJECT=deedkit-calendar-restore
LABEL=com.docker.compose.project=$PROJECT
export CALENDAR_RESTORE_IMAGE="ghcr.io/gosync-inc/deedkit-calendar@$DIGEST"
export CALENDAR_RESTORE_STATE_DIR=""

# What the scratch project still holds; fails when Docker cannot say, which is never the same as "nothing".
scratch_left() {
  docker ps -aq --filter "label=$LABEL" || return 1
  docker volume ls -q --filter "label=$LABEL" || return 1
  docker network ls -q --filter "label=$LABEL" || return 1
  ls -d "${ROOT:?}"/calendar-restore.* 2>/dev/null || true
}

# By the project's label and the fixed directory prefix, so it needs no Compose config and no earlier step, and it also
# clears what an interrupted run left. It runs only under the host lock.
teardown() {
  local status=$?
  set +eu
  trap - EXIT HUP INT TERM
  echo "== teardown (exit $status) =="
  echo "containers:"; docker ps -aq --filter "label=$LABEL" | xargs -r docker inspect --format '{{.Name}}'
  docker ps -aq --filter "label=$LABEL" | xargs -r docker rm --force --volumes >/dev/null
  echo "volumes:"; docker volume ls -q --filter "label=$LABEL"
  docker volume ls -q --filter "label=$LABEL" | xargs -r docker volume rm >/dev/null
  echo "networks:"; docker network ls --format '{{.Name}}' --filter "label=$LABEL"
  docker network ls -q --filter "label=$LABEL" | xargs -r docker network rm >/dev/null
  echo "files:"; find "${ROOT:?}"/calendar-restore.* -mindepth 1 -maxdepth 2 -printf '%P\n' 2>/dev/null
  rm -rf -- "${ROOT:?}"/calendar-restore.*
  local left
  if ! left=$(scratch_left); then
    echo "COULD NOT VERIFY the teardown: Docker did not answer; restored data may remain in the scratch project's volume"
    [ "$status" -ne 0 ] || status=1
  elif [ -n "$left" ]; then
    echo "LEFT BEHIND, remove by hand:"; echo "$left"
    [ "$status" -ne 0 ] || status=1
  else
    echo "left: nothing"
  fi
  exit "$status"
}

# One heavy restore at a time on this server, as the verifiers: a run that cannot get the lock has created nothing.
exec 9>>"$LOCK"
flock --wait 3600 9
trap teardown EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

restore() { docker compose -p "$PROJECT" -f "$HERE/compose.calendar-restore.yaml" "$@"; }
owner() { restore run --rm --no-deps -T -v "$CALENDAR_RESTORE_STATE_DIR/service/runtime.json:/data/service/runtime.json:ro" calendar-api "$@"; }
sql() { restore exec -T calendar-db psql -X -q -v ON_ERROR_STOP=1 -U fcs_owner -d family_calendar "$@"; }
# The backup image the verifier pins, with the verifier's bounds. The label puts it in teardown's reach.
helper() {
  docker run --rm --label "$LABEL" --log-driver none --restart no --memory 512m --memory-swap 512m --cpus 1 --pids-limit 256 \
    --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp --entrypoint python "$@"
}

LEFT=$(scratch_left)
if [ -n "$LEFT" ]; then
  echo "an interrupted restore left resources; this run only clears them: run it again"
  exit 1
fi

echo "== 1. protected point and capacity =="
point() {
  python3 -I - "$CONFIG" "$ROOT" <<'PY'
import json, shutil, sys
config, root = sys.argv[1:3]
backup = json.load(open(config + "/control/protected.json"))["backups"][0]
verifier = json.load(open(config + "/verifier.json"))
# The verifier's planning bound, with the plaintext as well: Docker's root shares this filesystem on the backup server.
needed = 2 * backup["bytes"] + verifier["postgres_headroom_bytes"] + verifier["reserve_bytes"]
if shutil.disk_usage(root).free < needed:
    sys.exit("restore_disk_capacity")
print(backup["id"], backup["sha256"], verifier["helper_image"], sep="\t")
PY
}
IFS=$'\t' read -r ID SHA HELPER < <(point)
echo "point $ID"

echo "== 2. download and decrypt into a new 0700 directory =="
WORK=$(mktemp -d "$ROOT/calendar-restore.XXXXXX")
CALENDAR_RESTORE_STATE_DIR="$WORK/state"
docker pull -q "$CALENDAR_RESTORE_IMAGE"
"$RECEIVER_PYTHON" -m messaging_service.backup_r2_receiver --config "$CONFIG/operator.json" \
  download --id "$ID" --output "$WORK/bundle.tar.age" >/dev/null
echo "$SHA  $WORK/bundle.tar.age" | sha256sum --check --quiet
# The receiver's venv has no psycopg, which decrypt_snapshot needs to verify a calendar bundle.
helper --network none -v "$WORK:/work" -v "$CONFIG/restore/identity.agekey:/identity:ro" "$HELPER" -c '
from messaging_service.backup_crypto import decrypt_snapshot
decrypt_snapshot("/work/bundle.tar.age", "/identity", "/work/decrypted")'

echo "== 3. profiles in the scratch state directory, the vault stays out =="
install -d -m 0700 -o 10001 -g 10001 "$CALENDAR_RESTORE_STATE_DIR" "$CALENDAR_RESTORE_STATE_DIR/service"
for name in postgres.env gateway.json worker.json runtime.json; do
  install -m 0600 -o 10001 -g 10001 "$WORK/decrypted/state/profile/$name" "$CALENDAR_RESTORE_STATE_DIR/service/$name"
done
# Only this copy changes, and only when a profile names a host that does not resolve inside the scratch project.
python3 -I - "$CALENDAR_RESTORE_STATE_DIR/service" <<'PY'
import json, sys
from pathlib import Path
from urllib.parse import urlsplit
for name in ("gateway", "worker", "runtime"):
    path = Path(sys.argv[1]) / (name + ".json")
    config = json.loads(path.read_text())
    parts = urlsplit(config["dsn"])
    if parts.hostname == "calendar-db":
        print(name, "dsn host ok")
        continue
    config["dsn"] = parts._replace(netloc=parts.netloc.rsplit("@", 1)[0] + "@calendar-db:5432").geturl()
    path.write_text(json.dumps(config))
    print(name, "dsn host rewritten to calendar-db")
PY

echo "== 4. database, roles with the bundled passwords, restore, roles.sql of this release =="
restore up -d --wait calendar-db
python3 -I - "$CALENDAR_RESTORE_STATE_DIR/service" <<'PY' | sql 2>"$WORK/roles.err" || { echo "creating the roles failed"; exit 1; }
import json, sys
from pathlib import Path
from urllib.parse import unquote, urlsplit
for name, role in (("gateway", "fcs_gateway"), ("worker", "fcs_worker")):
    password = unquote(urlsplit(json.loads((Path(sys.argv[1]) / (name + ".json")).read_text())["dsn"]).password)
    print("CREATE ROLE %s LOGIN PASSWORD '%s' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;" % (role, password.replace("'", "''")))
PY
# pg_restore's stderr can quote rows: only the verifier's fixed phrase for it is printed.
if ! restore exec -T calendar-db pg_restore --exit-on-error --no-owner --no-acl -U fcs_owner -d family_calendar \
  < "$WORK/decrypted/database.dump" 2>"$WORK/pg_restore.err"; then
  echo "pg_restore failed:"
  helper -i --network none "$HELPER" -c '
import sys
from messaging_service.backup_restore_worker import restore_detail
print(restore_detail(sys.stdin.buffer.read(64 * 1024)))' < "$WORK/pg_restore.err" || true
  exit 1
fi
restore run --rm --no-deps -T --entrypoint python calendar-api -c '
from pathlib import Path
import family_calendar.service as service
print(Path(service.__file__).with_name("roles.sql").read_text())' </dev/null | sql

echo "== 5. API, isolation, schema, grants, manifest =="
restore up -d --wait --wait-timeout 180 calendar-api
restore exec -T calendar-api python -I -c '
import os, socket, sys
if os.path.exists("/data/service/vault"):
    sys.exit("vault mounted")
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5)
except OSError:
    print("isolated: no vault, no route out")
else:
    sys.exit("route out exists")' </dev/null
owner schema-status </dev/null
owner db-roles --check </dev/null
python3 -I - "$WORK/decrypted/manifest.json" "$CALENDAR_RESTORE_STATE_DIR/service/runtime.json" <<'PY' |
import json, sys
manifest, runtime = (json.load(open(path)) for path in sys.argv[1:3])
print(json.dumps({"dsn": runtime["dsn"], "tables": manifest["tables"], "schema_version": manifest["schema_version"]}))
PY
helper -i --network "${PROJECT}_calendar-backend" "$HELPER" -c '
import json, sys
import psycopg
from messaging_service.backup_calendar import calendar_manifest, calendar_schema_version
job = json.load(sys.stdin)
with psycopg.connect(job["dsn"], connect_timeout=10) as connection:
    tables = calendar_manifest(connection)[0]
    version = calendar_schema_version(connection)
same = {"manifest_equal": tables == job["tables"], "schema_equal": version == job["schema_version"]}
print(json.dumps({**same, "schema_version": version, "tables": len(tables), "rows": sum(t["rows"] for t in tables.values())}))
sys.exit(0 if all(same.values()) else 1)'

echo "== 6. one read through the API by a temporary read-only principal =="
IFS=$'\t' read -r GROUP_ID MEMBER CALENDAR < <(sql -At -F $'\t' -c "SELECT g.group_id, g.person_id, g.calendar_ref
  FROM fcs.calendar_grants g JOIN fcs.calendars c ON c.ref = g.calendar_ref JOIN fcs.connections n ON n.id = c.connection_id
  WHERE n.enabled AND g.scopes ? 'agenda:read' ORDER BY 1, 2, 3 LIMIT 1" </dev/null)
python3 -I -c 'import secrets, sys; sys.stdout.write(secrets.token_urlsafe(48))' > "$WORK/drill.key"
owner agent-key --id restore-drill --group "$GROUP_ID" --member "$MEMBER" --calendar "$CALENDAR" --days 1 \
  --token-file /dev/stdin --operation-id restore-drill < "$WORK/drill.key" >/dev/null
restore exec -T calendar-api python -I -c '
import json, os, sys, urllib.request
with open(os.path.join(os.environ["FCS_DATA_DIR"], "service", "gateway.json")) as file:
    config = json.load(file)
port = int(config["port"])
host = (config.get("allowed_hosts") or [f"127.0.0.1:{port}"])[0]
bind = config.get("bind", "127.0.0.1")
address = "127.0.0.1" if bind in ("", "0.0.0.0", "::") else f"[{bind}]" if ":" in bind else bind
request = urllib.request.Request(f"http://{address}:{port}/v1/calendars", data=b"{}", headers={
    "Host": host, "Content-Type": "application/json", "Authorization": "Bearer " + sys.stdin.read()})
with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as response:
    print(json.dumps({"status": response.status, "calendars": len(json.load(response)["calendars"])}))' < "$WORK/drill.key"
sql -At <<'SQL'
DELETE FROM fcs.agent_calendars WHERE principal_id = 'restore-drill';
DELETE FROM fcs.principal_operations WHERE operation_id = 'restore-drill';
DELETE FROM fcs.principals WHERE id = 'restore-drill';
SELECT 'temporary principals left: ' || count(*) FROM fcs.principals WHERE id = 'restore-drill';
SQL

echo "== restore proved =="
