#!/usr/bin/env bash
# Daily updater for a PERSONAL DeedKit install (docs/personal-install.md). Not the maintainers' production
# tool: no production.py, no tailnet, no backup server. Run as root from the checkout, normally via
# deploy/deedkit-personal-update.timer.
#
# Release policy: DEEDKIT_TRACK=main (default) follows origin/main, because no release tag newer than the
# 0.9.9 one exists, but only to the newest commit whose GitHub `CI gate` check is green and whose
# `checks / verify` did not fail (read from the public GitHub API, no token; unreadable = no update).
# DEEDKIT_TRACK=tag follows the newest v* tag once releases are cut again.
#
# Order, stopping at the first failure (before step 6 the old containers keep running untouched):
#   1 refuse a modified checkout (tracked files)   2 fetch, pick target, require a fast-forward
#   3 local pg_dump of each database (the only "backup" of this lite path: a cheap rollback for a bad migration)
#   4 check out the target   5 build images   6 migrate + re-apply role SQL   7 docker compose up -d
#   8 wait for health.  Exactly one summary line per run goes to the log.
#
# Settings (environment, or private/personal-update.env next to this script):
#   DEEDKIT_TRACK=main|tag  DEEDKIT_REMOTE=origin  DEEDKIT_DUMP_DIR=/var/backups/deedkit-local
#   DEEDKIT_DUMP_KEEP=3  DEEDKIT_UPDATE_LOG=/var/log/deedkit-update.log  DEEDKIT_HEALTH_WAIT=180
#   DEEDKIT_MIN_FREE_MB=2048
# Rollback after a bad update: git checkout <from-sha>; rebuild; docker compose up -d. If a migration ran,
# restore the matching dump first (pg_restore into a fresh database; see the log line for the dump path).
set -uo pipefail
# No global umask: git checkout and docker build must keep files readable (images run as uid 10001).

main() {
  local here repo conf
  here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || return 1
  repo=$(cd "$here/.." && pwd); REPO=$repo
  conf=$here/private/personal-update.env
  # shellcheck disable=SC1090
  [ -r "$conf" ] && . "$conf"
  TRACK=${DEEDKIT_TRACK:-main}; REMOTE=${DEEDKIT_REMOTE:-origin}
  DUMPS=${DEEDKIT_DUMP_DIR:-/var/backups/deedkit-local}; KEEP=${DEEDKIT_DUMP_KEEP:-3}
  LOG=${DEEDKIT_UPDATE_LOG:-/var/log/deedkit-update.log}; WAIT=${DEEDKIT_HEALTH_WAIT:-180}
  MIN_MB=${DEEDKIT_MIN_FREE_MB:-2048}
  FROM=unknown; TO=unknown; SCHEMA=-; DUMPED=-
  mkdir -p -m 700 "$DUMPS" "$(dirname "$LOG")" 2>/dev/null
  ERR=$DUMPS/last-error.log; : > "$ERR" 2>/dev/null   # output of the failing command; overwritten each run
  for tool in git docker flock curl python3; do command -v "$tool" >/dev/null || { say failed "missing $tool"; return 1; }; done
  exec 9>"$DUMPS/.lock" || { say failed "cannot write $DUMPS"; return 1; }
  flock -n 9 || { say skipped "another update is running"; return 0; }
  cd "$repo" || { say failed "no checkout at $repo"; return 1; }

  # 1. clean checkout (untracked files such as deploy/private and .env are fine)
  [ -z "$(git status --porcelain --untracked-files=no)" ] || { say failed "checkout has local changes to tracked files; update refused"; return 1; }
  FROM=$(git rev-parse HEAD) || { say failed "not a git checkout"; return 1; }

  # 2. target
  git fetch --quiet --tags --force "$REMOTE" || { say failed "git fetch failed"; return 1; }
  if [ "$TRACK" = tag ]; then
    local tag; tag=$(git tag -l 'v[0-9]*' --sort=-v:refname | grep -v -- '-' | head -n 1)
    [ -n "$tag" ] || { say failed "no v* tag found"; return 1; }
    TO=$(git rev-parse "$tag^{commit}")
    [ "$TO" != "$FROM" ] || { uptodate_check "already at the newest tag"; return $?; }
    git merge-base --is-ancestor "$FROM" "$TO" || { say failed "target is not a fast-forward of the checkout; update refused"; return 1; }
  else
    local tip c rc; tip=$(git rev-parse "$REMOTE/main") || { say failed "no $REMOTE/main"; return 1; }
    [ "$tip" != "$FROM" ] || { uptodate_check "already at origin/main"; return $?; }
    git merge-base --is-ancestor "$FROM" "$tip" || { say failed "origin/main is not a fast-forward of the checkout; update refused"; return 1; }
    TO=unknown
    for c in $(git rev-list --first-parent "$FROM..$tip" | head -n 20); do  # newest first
      green_ci "$c"; rc=$?
      [ "$rc" -ne 2 ] || { say failed "cannot read the GitHub CI status; no update"; return 1; }
      if [ "$rc" -eq 0 ]; then TO=$c; break; fi
    done
    [ "$TO" != unknown ] || { uptodate_check "no newer origin/main commit with green CI among the newest 20"; return $?; }
  fi
  local free; free=$(df -Pm "$DUMPS" | awk 'NR==2{print $4}')
  [ "${free:-0}" -ge "$MIN_MB" ] || { say failed "less than ${MIN_MB} MB free in $DUMPS"; return 1; }

  cd "$repo/deploy" || return 1
  local msg gw cal
  msg=$(docker compose ps -aq db 2>/dev/null); gw=$(docker compose -f compose.gateway.yaml ps -aq gateway 2>/dev/null)
  cal=$(docker compose -f compose.calendar.yaml ps -aq calendar-api 2>/dev/null)
  [ -n "$msg$gw$cal" ] || { say failed "no DeedKit containers found in $repo/deploy"; return 1; }

  # 3. local dumps
  if [ -n "$msg" ]; then dump messaging "" db probe_owner messaging_probe || return 1; fi
  if [ -n "$cal" ]; then dump calendar "-f compose.calendar.yaml" calendar-db fcs_owner family_calendar || return 1; fi

  # 4-5. new code, new images; a failed build restores the old checkout and touches no container
  git checkout --quiet --detach "$TO" || { say failed "checkout of target failed"; return 1; }
  cd "$repo/deploy" || return 1
  build_all "$msg" "$gw" "$cal" || { revert; say failed "image build failed; old containers untouched"; return 1; }

  # 6. migrations and roles run on the new image while the old containers still serve
  # A failure up to here reverts the checkout to the old commit, so the next run tries again.
  if [ -n "$msg" ]; then migrate_messaging || { revert; return 1; }; fi
  if [ -n "$cal" ]; then migrate_calendar || { revert; return 1; }; fi

  # 7-8. replace containers, then wait for health (collectors restart: their provider sessions reconnect)
  # Only services that are running now are brought up: a calendar worker you never started, or a collector you stopped on purpose, stays off.
  [ -z "$msg" ] || up_existing messaging || return 1
  [ -z "$cal" ] || up_existing calendar -f compose.calendar.yaml || return 1
  [ -z "$gw" ] || up_existing gateway -f compose.gateway.yaml || return 1
  [ -z "$msg" ] || wait_healthy messaging || return 1
  [ -z "$cal" ] || wait_healthy calendar -f compose.calendar.yaml || return 1
  [ -z "$gw" ] || wait_healthy gateway -f compose.gateway.yaml || return 1
  say ok "updated"
}

revert() { git checkout --quiet --detach "$FROM"; }

uptodate_check() {  # nothing to update: still report a stack that is not running healthy (a failed earlier update)
  local reason=$1 WAIT=30
  cd "$REPO/deploy" || return 1
  if [ -n "$(docker compose ps -q db 2>/dev/null)" ]; then wait_healthy messaging || return 1; fi
  if [ -n "$(docker compose -f compose.calendar.yaml ps -q calendar-api 2>/dev/null)" ]; then wait_healthy calendar -f compose.calendar.yaml || return 1; fi
  if [ -n "$(docker compose -f compose.gateway.yaml ps -q gateway 2>/dev/null)" ]; then wait_healthy gateway -f compose.gateway.yaml || return 1; fi
  say uptodate "$reason"
}

say() {  # say <result> <detail>: the one summary line
  printf '%s result=%s from=%.12s to=%.12s schema=%s dumps=%s %s\n' "$(date -u +%FT%TZ)" "$1" "$FROM" "$TO" "$SCHEMA" "$DUMPED" "$2" | tee -a "$LOG" >&2
}

green_ci() {  # green_ci <sha>: 0 green, 1 not green (yet), 2 status unreadable. Public repo, no token.
  local slug json
  slug=$(git remote get-url "$REMOTE" | sed -n 's#^.*github\.com[:/]\([^/]*/[^/]*\)$#\1#p' | sed 's#\.git$##')
  [ -n "$slug" ] || return 2
  json=$(curl -fsS --max-time 30 -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$slug/commits/$1/check-runs?per_page=100") || return 2
  printf '%s' "$json" | python3 -I -c '
import json, sys
try:
    runs = json.load(sys.stdin)["check_runs"]
    by = {}
    for r in runs: by.setdefault(r["name"], []).append((r["status"], r["conclusion"]))
except Exception:
    sys.exit(2)
gate, verify = by.get("CI gate", []), by.get("checks / verify", [])
ok = bool(gate) and all(x == ("completed", "success") for x in gate)
ok = ok and all(s == "completed" and c in ("success", "skipped") for s, c in verify)
sys.exit(0 if ok else 1)'
  return $?
}

dump() {  # dump <name> "<compose -f args>" <service> <user> <db>
  local name=$1 args=$2 svc=$3 user=$4 db=$5 dir out
  dir=$DUMPS/$name; mkdir -p -m 700 "$dir"
  out=$dir/$name-$(date -u +%Y%m%dT%H%M%SZ)-${FROM:0:12}.dump
  # shellcheck disable=SC2086
  if ! (umask 077; docker compose $args exec -T "$svc" pg_dump -U "$user" -d "$db" -Fc > "$out.partial" 2>/dev/null) || [ ! -s "$out.partial" ] \
     || ! docker compose $args exec -T "$svc" pg_restore --list < "$out.partial" >/dev/null 2>&1; then
    rm -f "$out.partial"; say failed "pg_dump of $name failed; nothing changed"; return 1
  fi
  mv "$out.partial" "$out"; DUMPED=$dir
  # keep the newest $KEEP dumps of this database
  ls -1t "$dir"/*.dump | tail -n +"$((KEEP + 1))" | while read -r old; do rm -f "$old"; done
}

build_all() {
  local msg=$1 gw=$2 cal=$3 native
  [ -z "$msg" ] || docker compose build >>"$ERR" 2>&1 || return 1
  # The WhatsApp native image has no build section in compose: rebuild it when this install uses it.
  native=$(docker compose config --images 2>/dev/null | grep '^anymsg-whatsapp-native:' | sort -u | head -n 1)
  if [ -n "$native" ] && docker image inspect "$native" >/dev/null 2>&1; then
    docker build --quiet --target whatsapp-native -t "$native" .. >>"$ERR" 2>&1 || return 1
  fi
  [ -z "$gw" ] || docker compose -f compose.gateway.yaml build >>"$ERR" 2>&1 || return 1
  [ -z "$cal" ] || docker compose -f compose.calendar.yaml build calendar-api >>"$ERR" 2>&1 || return 1
}

migrate_messaging() {
  local out roles=../services/messaging/messaging_service
  out=$(docker compose -f compose.yaml -f compose.operator.yaml run --rm -T --no-deps operator migrate 2>>"$ERR") \
    || { say failed "messaging migrate failed; old containers still running, details in $ERR"; return 1; }
  SCHEMA=$(printf '%s' "$out" | sed -n 's/.*"schema_version": *\([0-9]*\).*/\1/p' | tail -n 1); SCHEMA=${SCHEMA:--}
  # Release notes sometimes require re-applying these (they are idempotent grants).
  docker compose exec -T db psql -v ON_ERROR_STOP=1 -q -U probe_owner -d messaging_probe < "$roles/gateway_role.sql" >>"$ERR" 2>&1 \
    || { say failed "gateway_role.sql failed; old containers still running"; return 1; }
  if [ "$(docker compose exec -T db psql -tAq -U probe_owner -d messaging_probe -c "SELECT 1 FROM pg_roles WHERE rolname='messaging_collector'" 2>/dev/null | tr -d '[:space:]')" = 1 ]; then
    docker compose exec -T db psql -v ON_ERROR_STOP=1 -q -U probe_owner -d messaging_probe < "$roles/collector_role.sql" >>"$ERR" 2>&1 \
      || { say failed "collector_role.sql failed; old containers still running"; return 1; }
  fi
}

migrate_calendar() {
  local image net state
  image=$(docker compose -f compose.calendar.yaml config --images 2>/dev/null | grep '^deedkit-calendar:' | head -n 1)
  net=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$(docker compose -f compose.calendar.yaml ps -aq calendar-db)" | tr ' ' '\n' | grep 'calendar-backend$' | head -n 1)
  state=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data/service/gateway.json"}}{{.Source}}{{end}}{{end}}' "$(docker compose -f compose.calendar.yaml ps -aq calendar-api)")
  state=$(dirname "$state")
  if [ -z "$image" ] || [ -z "$net" ] || [ "$state" = . ]; then say failed "calendar image, network or state directory not found"; return 1; fi
  docker run --rm -v "$state:/data/service" --network "$net" "$image" migrate >>"$ERR" 2>&1 \
    && docker run --rm -v "$state:/data/service" --network "$net" "$image" db-roles >>"$ERR" 2>&1 \
    && docker run --rm -v "$state:/data/service" --network "$net" "$image" db-roles --check >>"$ERR" 2>&1 \
    || { say failed "calendar migrate or db-roles failed; old containers still running (the messaging migration, if any, is already applied)"; return 1; }
}

up_existing() {  # up_existing <label> [compose args]
  local label=$1 services; shift
  services=$(docker compose "$@" ps --services 2>/dev/null | sort -u | tr '\n' ' ')
  [ -n "$services" ] || return 0   # a stack with nothing running stays as it is (an empty list would start everything)
  # shellcheck disable=SC2086
  docker compose "$@" up -d $services >>"$ERR" 2>&1 || { say failed "docker compose up failed ($label); new images are built, details in $ERR, see header for rollback"; return 1; }
}

wait_healthy() {  # wait_healthy <label> [compose args]
  local label=$1 deadline ids id st hs ok good=0; shift
  deadline=$(( $(date +%s) + WAIT ))
  while :; do
    ok=1; ids=$(docker compose "$@" ps -q 2>/dev/null)
    [ -n "$ids" ] || ok=0
    for id in $ids; do
      read -r st hs < <(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$id")
      [ "$st" = running ] || ok=0
      case $hs in none|healthy) ;; *) ok=0;; esac
    done
    if [ "$ok" = 1 ]; then good=$((good + 1)); [ "$good" -lt 2 ] || return 0; else good=0; fi   # healthy twice in a row
    [ "$(date +%s)" -lt "$deadline" ] || { say failed "$label not healthy after ${WAIT}s; new containers left running for inspection, see header for rollback"; return 1; }
    sleep 5
  done
}

main "$@"; exit $?
