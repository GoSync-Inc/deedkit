#!/usr/bin/env bash
# Run a DeedKit test or build suite on the Mac mini (owner D41/D42) instead of the laptop.
#
#   scripts/remote-test.sh <worktree> <suite> [args...]   run <suite> there, stream its output, exit with its status
#   scripts/remote-test.sh --cleanup <worker>             remove the worker's copies and databases on the mini
#   scripts/remote-test.sh --load                         print the mini's load.log
#   scripts/remote-test.sh --list <worktree>              print the files a run sends
#
# Suites: root calendar gateway calls go build frontend node. Extra args replace the suite's default test
# selection (pytest paths/options; go packages).
#
# A run copies the worktree's tracked and untracked-but-not-ignored files, never a secret-shaped one
# (is_secret), to /Users/Shared/deedkit-ci/stage/<worker> on the mini as egoregerev, then runs the suite as
# the service user _deedkitci (sudo, docs/operations/remote-test-owner-steps.md) in its own copy
# ~_deedkitci/deedkit-ci/<worker>/src, with a disposable PostgreSQL 18 on 127.0.0.1, and streams the output
# back; nothing of it uses docker there, so tests that need docker skip, as in the local flow without
# ANYMSG_TEST_POSTGRES_CONTAINER (CI covers them). The mini runs at most MAX_RUNS DeedKit suites at once and
# gives way to Between: when a Between container runs or the host load is high, the slots are taken, the disk is short, the service user is missing or the
# mini is unreachable, the same suite runs on this machine instead (the local flow; DEEDKIT_REMOTE_FALLBACK=0
# exits 90 refused, 91 unreachable, 92 not set up). Each mini run appends
# `worker suite start wall_s cpu_s pg_cpu_s exit` to ~_deedkitci/deedkit-ci/load.log.
#
# Caps: /Users/Shared/deedkit-ci/caps.conf on the mini (KEY=value lines, defaults below), so they change
# without a pull request. Worker name: DEEDKIT_REMOTE_WORKER, else the worktree's last two path components.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
HOST="${DEEDKIT_REMOTE_HOST:-egoregerev@100.88.40.111}"
SERVICE_USER="${DEEDKIT_REMOTE_USER:-_deedkitci}"
SHIM="${DEEDKIT_REMOTE_SHIM:-/usr/local/libexec/deedkit-remote-test}"
SHARED="${DEEDKIT_REMOTE_SHARED:-/Users/Shared/deedkit-ci}"
FALLBACK="${DEEDKIT_REMOTE_FALLBACK:-1}"
SUITES=" root calendar gateway calls go build frontend node "
REFUSED=90
UNREACHABLE=91
NOT_SET_UP=92
# The laptop's database, as the local flow: CI's postgres service image (checks.yml).
PG_IMAGE=postgres:18-bookworm@sha256:afc7e2d441324c0388fa80c3d24f733b4194a4eb7f47dd8ee2b08eb1a24a647c
LABEL=deedkit.remote-test.worker

# Defaults of the caps; caps.conf overrides them (owner D42: Between has priority, one DeedKit run).
MAX_RUNS=1          # DeedKit runs at once on the mini
PARALLEL=2          # root pytest shards (each with its own database), go test -p and GOMAXPROCS
MIN_FREE_GB=15      # disk guard
WAIT_SECONDS=0      # how long to wait for a free slot before falling back
BUSY_CHECK=1        # 1: fall back while a Between container runs or the host load is above MAX_LOAD
MAX_LOAD=8          # 1-minute load average of the mini's 12 cores
NICE=10             # the mini run's scheduling priority, below Between's

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)

die() { echo "remote-test: $*" >&2; exit 2; }
say() { echo "remote-test: $*" >&2; }

valid_worker() { [[ "$1" =~ ^[a-z0-9][a-z0-9_]{0,62}$ ]]; }

worker_of() {
  local name="${DEEDKIT_REMOTE_WORKER:-}"
  if [ -z "$name" ]; then
    name="$(basename "$(dirname "$1")")_$(basename "$1")"
    name="$(printf '%s' "$name" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9' '_' | sed 's/^_*//')"
  fi
  valid_worker "$name" || die "worker name '$name' must match [a-z0-9][a-z0-9_]{0,62}"
  printf '%s' "$name"
}

# A path that may hold a secret, a session or a local environment never leaves this machine.
is_secret() {
  local base="${1##*/}" part parts
  IFS=/ read -ra parts <<< "$1"
  for part in "${parts[@]}"; do
    case "$part" in
      .git|.venv|venv|node_modules|.tiden|.ssh|.gnupg|.aws|.azure|.kube|.docker|__pycache__|.pytest_cache|.ruff_cache|.mypy_cache) return 0 ;;
    esac
  done
  shopt -s nocasematch
  local found=1
  case "$base" in
    .env|.env.*|*.env|.envrc|*.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|*.gpg|*.agekey|*.ovpn|*.kdbx|*.dsn) found=0 ;;
    id_rsa*|id_dsa*|id_ecdsa*|id_ed25519*|auth.json|.netrc|.npmrc|.pypirc|.git-credentials|.htpasswd) found=0 ;;
    *.session|*.session-journal|*.sqlite|*.sqlite3|*.db) found=0 ;;
    *credential*|*token*|*secret*|*passw*)
      # Source and docs that only name the subject (tests/test_credential_expiry.py) stay; data files go.
      case "$base" in
        *.py|*.go|*.mjs|*.js|*.ts|*.tsx|*.jsx|*.md|*.sh) ;;
        *) found=0 ;;
      esac ;;
  esac
  shopt -u nocasematch
  return "$found"
}

# Tracked files that still exist plus untracked files git does not ignore, minus secrets: NUL-separated.
list_files() {
  local tree="$1" path
  git -C "$tree" ls-files -z --cached --others --exclude-standard | while IFS= read -r -d '' path; do
    if [ -f "$tree/$path" ] || [ -L "$tree/$path" ]; then
      is_secret "$path" || printf '%s\0' "$path"
    fi
  done | sort -zu
}

# rsync options shared by both copies: --delete keeps the copy equal to its source, and these receiver
# paths (environments, caches, build output) survive it; the secret shapes are a second net after is_secret.
RSYNC_COPY=(-a --delete --exclude=.git/ --exclude=.venv/ --exclude=node_modules/ --exclude=__pycache__/
  --exclude=.pytest_cache/ --exclude=.ruff_cache/ --exclude=dist/ --exclude='.env*' --exclude='*.pem'
  --exclude='*.key' --exclude=auth.json --exclude=.tiden/)

read_caps() {
  local file="$1" key value
  [ -f "$file" ] || return 0
  while IFS='=' read -r key value; do
    value="${value%%#*}"; value="${value//[[:space:]]/}"
    case "$key" in
      MAX_RUNS|PARALLEL|MIN_FREE_GB|WAIT_SECONDS|BUSY_CHECK|MAX_LOAD|NICE) [[ "$value" =~ ^[0-9]+$ ]] || continue ;;
      *) continue ;;
    esac
    eval "$key=\$value"
  done < "$file"
  [ "$MAX_RUNS" -ge 1 ] || MAX_RUNS=1
  [ "$PARALLEL" -ge 1 ] || PARALLEL=1
}

# --- On the runner (the mini as _deedkitci, or this machine as the fallback) -------------------------

ROOT_DIR=""
LOCAL=0
WORKER=""
SUITE=""
CHILDREN=""
DATABASES=""
CONTAINERS=""
ADMITTED=0
START_EPOCH=0
START_ISO=""

runner_env() {
  local dir
  ROOT_DIR="${DEEDKIT_CI_ROOT:-$HOME/deedkit-ci}"
  # On the mini the shim's PATH lacks Homebrew; node@24 is keg-only there (the repo's Node 24).
  if [ "$LOCAL" = 0 ]; then
    for dir in ${DEEDKIT_RUNNER_PATH_DIRS-/usr/local/bin /opt/homebrew/sbin /opt/homebrew/bin /opt/homebrew/opt/node@24/bin}; do
      if [ -d "$dir" ]; then PATH="$dir:$PATH"; fi
    done
    export PATH
  fi
  read_caps "$SHARED/caps.conf"
}

load_1m() {
  if [ -r /proc/loadavg ]; then cut -d' ' -f1 /proc/loadavg; else sysctl -n vm.loadavg | awk '{print $2}'; fi
}

# As egoregerev on the mini: Between's containers on the default VM, or a host load above MAX_LOAD (Between's
# own VM is invisible to this docker; its load shows on the host).
between_busy() {
  local load
  if docker ps --format '{{.Names}} {{.Labels}}' 2>/dev/null | grep -Eq '^between-|io\.between\.'; then
    echo "a Between container is running"; return 0
  fi
  load="$(load_1m)"
  if awk -v l="$load" -v m="$MAX_LOAD" 'BEGIN { exit !(l > m) }'; then echo "host load $load is above $MAX_LOAD"; return 0; fi
  return 1
}

free_gb() { df -Pk "$1" | awk 'NR==2 {print int($4 / 1048576)}'; }

# Hold one of MAX_RUNS slot locks on fd 9 for the life of this process (and its children).
take_slot() {
  local slot
  mkdir -p "$ROOT_DIR/.locks"
  for slot in $(seq 1 "$MAX_RUNS"); do
    exec 9>>"$ROOT_DIR/.locks/slot$slot"
    if python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null; then return 0; fi
    exec 9>&-
  done
  return 1
}

# Admission as _deedkitci: disk, then a slot. `probe` releases the slot again.
admit() {
  local mode="$1" deadline free
  mkdir -p "$ROOT_DIR"
  free="$(free_gb "$ROOT_DIR")"
  if [ "$free" -lt "$MIN_FREE_GB" ]; then say "mini disk guard: ${free} G free, need ${MIN_FREE_GB} G"; return "$REFUSED"; fi
  deadline=$((SECONDS + WAIT_SECONDS))
  until take_slot; do
    if [ "$SECONDS" -ge "$deadline" ]; then say "mini busy: all $MAX_RUNS DeedKit slot(s) taken"; return "$REFUSED"; fi
    sleep 5
  done
  if [ "$mode" = probe ]; then exec 9>&-; fi
  return 0
}

seconds_of() { awk '{ split($0, a, "m"); sub("s", "", a[2]); printf "%.2f", a[1] * 60 + a[2] }' <<< "$1"; }

# User + system CPU of every finished child of this run so far (bash `times`: the rusage /usr/bin/time reads).
children_cpu() {
  local line
  line="$(times | tail -1)"
  awk -v u="$(seconds_of "${line%% *}")" -v s="$(seconds_of "${line##* }")" 'BEGIN { printf "%.1f", u + s }'
}

# Run a command as a background job (its own process group under set -m, so a stop reaches all of it) and
# wait for it; a trapped signal interrupts the wait at once.
run() {
  "$@" &
  local pid=$!
  CHILDREN="$CHILDREN $pid"
  wait "$pid"
  CHILDREN="${CHILDREN% $pid}"
}

in_dir() { cd "$1" && shift && exec "$@"; }

stop_group() { kill "-$1" -- "-$2" 2>/dev/null || kill "-$1" "$2" 2>/dev/null || true; }

PG_DSN=""
PG_BIN="${DEEDKIT_PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}"

# The mini: PostgreSQL 18 from Homebrew as a child of this run, its cluster in the worker's directory,
# listening on 127.0.0.1 only. A child, so its CPU (and its backends') counts in `times` once it is reaped.
pg_start_native() {
  local name="$1" data password port i
  data="$ROOT_DIR/$WORKER/$name"
  password="$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  rm -rf "$data"
  mkdir -p "$data"
  chmod 700 "$data"
  (umask 077 && printf '%s\n' "$password" > "$data.pw")
  "$PG_BIN/initdb" -D "$data/cluster" -U probe_owner --pwfile="$data.pw" --auth=scram-sha-256 -E UTF8 \
    --locale=en_US.UTF-8 >/dev/null
  rm -f "$data.pw"
  port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  "$PG_BIN/postgres" -D "$data/cluster" -c listen_addresses=127.0.0.1 -p "$port" -k "$data" \
    -c shared_buffers=128MB -c fsync=off -c synchronous_commit=off -c full_page_writes=off > "$data/log" 2>&1 &
  DATABASES="$DATABASES $!"
  for i in $(seq 1 60); do
    if "$PG_BIN/pg_isready" -q -h 127.0.0.1 -p "$port"; then break; fi
    if [ "$i" = 60 ]; then say "postgres $name did not become ready"; cat "$data/log" >&2; return 1; fi
    sleep 1
  done
  PGPASSWORD="$password" "$PG_BIN/createdb" -h 127.0.0.1 -p "$port" -U probe_owner messaging_probe_test
  PG_DSN="postgresql://probe_owner:$password@127.0.0.1:$port/messaging_probe_test"
}

# This machine: the local flow's `docker run --rm` postgres:18 on 127.0.0.1.
pg_start_docker() {
  local name="$1" password port i
  password="$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  CONTAINERS="$CONTAINERS $name"
  docker run --rm -d --name "$name" --label "$LABEL=$WORKER" -e POSTGRES_USER=probe_owner \
    -e "POSTGRES_PASSWORD=$password" -e POSTGRES_DB=messaging_probe_test -p 127.0.0.1::5432 "$PG_IMAGE" >/dev/null
  # TCP readiness: the image's init server listens on the socket only, so this waits for the real one.
  for i in $(seq 1 90); do
    if docker exec "$name" pg_isready -q -h 127.0.0.1 -U probe_owner -d messaging_probe_test 2>/dev/null; then break; fi
    if [ "$i" = 90 ]; then say "postgres $name did not become ready"; return 1; fi
    sleep 1
  done
  port="$(docker port "$name" 5432/tcp | head -1)"
  PG_DSN="postgresql://probe_owner:$password@127.0.0.1:${port##*:}/messaging_probe_test"
}

pg_start() { if [ "$LOCAL" = 1 ]; then pg_start_docker "$@"; else pg_start_native "$@"; fi; }

# Leftovers of an earlier run of this worker that was killed before its trap ran.
stop_leftovers() {
  local pidfile pid id
  for pidfile in "$ROOT_DIR/$1"/pg*/cluster/postmaster.pid; do
    [ -f "$pidfile" ] || continue
    pid="$(head -1 "$pidfile")"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then kill -QUIT "$pid" 2>/dev/null || true; fi
  done
  rm -rf "$ROOT_DIR/$1"/pg*
  if [ "$LOCAL" = 1 ]; then
    for id in $(docker ps -aq --filter "label=$LABEL=$1" 2>/dev/null); do docker stop -t 5 "$id" >/dev/null 2>&1 || true; done
  fi
}

finish() {
  local status=$? pid name before pg=0
  trap - EXIT INT TERM HUP
  for pid in $CHILDREN; do stop_group TERM "$pid"; done
  for pid in $CHILDREN; do wait "$pid" 2>/dev/null || true; done
  before="$(children_cpu)"
  for pid in $DATABASES; do kill -QUIT "$pid" 2>/dev/null || true; done  # immediate shutdown
  for pid in $DATABASES; do wait "$pid" 2>/dev/null || true; done
  if [ -n "$DATABASES" ]; then
    pg="$(awk -v a="$(children_cpu)" -v b="$before" 'BEGIN { printf "%.1f", a - b }')"
    rm -rf "$ROOT_DIR/$WORKER"/pg*
  fi
  for name in $CONTAINERS; do docker stop -t 5 "$name" >/dev/null 2>&1 || true; done
  if [ "$SUITE" = root ] && [ "$LOCAL" = 0 ]; then
    say "skipped: needs docker (CI covers): the modules that use ANYMSG_TEST_POSTGRES_CONTAINER or the docker CLI;" \
      "this is not the full suite"
  fi
  if [ "$ADMITTED" = 1 ]; then
    rm -f "$ROOT_DIR/$WORKER/run.pid"
    if [ "$LOCAL" = 0 ]; then
      printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$WORKER" "$SUITE" "$START_ISO" "$((SECONDS - START_EPOCH))" \
        "$(children_cpu)" "$pg" "$status" >> "$ROOT_DIR/load.log"
    fi
  fi
  exit "$status"
}

write_dsn() { (umask 077 && printf '%s' "$PG_DSN" > "$1"); }

sync_env() { cd "$1" && exec uv sync --frozen --group dev -q; }

prefix() { awk -v p="$1" '{ print p $0; fflush() }'; }

suite_root() {
  local dir="$1"; shift
  local shard pids="" status=0 pid rc
  local ignores=(--ignore=tests/test_backup_r2_verifier_native.py
    --deselect tests/test_collectors.py::test_session_lock_rejects_directory_chowned_to_foreign_uid_as_root
    --deselect tests/test_audio_native.py::test_optional_cpu_package_loads_without_gpu_or_model_download
    --deselect tests/test_audio_native.py::test_faster_whisper_decoder_compatibility_without_model)
  run sync_env .
  run sync_env services/gateway
  run sync_env services/calendar
  export OMP_THREAD_LIMIT=1
  if [ $# -gt 0 ] || [ "$PARALLEL" = 1 ]; then
    pg_start pg1
    write_dsn "$dir/test-1.dsn"
    [ $# -gt 0 ] || set -- --tb=short "${ignores[@]}" tests
    run env PROBE_TEST_DSN_FILE="$dir/test-1.dsn" uv run pytest "$@"
    return
  fi
  # The full suite: PARALLEL file-level shards (scripts/python_shards.py, as CI), each with its own database.
  for shard in $(seq 1 "$PARALLEL"); do
    pg_start "pg$shard"
    write_dsn "$dir/test-$shard.dsn"
  done
  for shard in $(seq 1 "$PARALLEL"); do
    (env DEEDKIT_TEST_SHARD="$shard/$PARALLEL" PROBE_TEST_DSN_FILE="$dir/test-$shard.dsn" uv run python \
      scripts/python_shards.py pytest --tb=short "${ignores[@]}" tests 2>&1 | prefix "[$shard/$PARALLEL] ") &
    pids="$pids $!"
  done
  CHILDREN="$CHILDREN $pids"
  for pid in $pids; do
    rc=0; wait "$pid" || rc=$?
    [ "$status" != 0 ] || status=$rc
  done
  return "$status"
}

run_suite() {
  local dir="$1"; shift
  case "$SUITE" in
    root) suite_root "$dir" "$@" ;;
    calendar)
      run sync_env services/gateway
      run sync_env services/calendar
      pg_start pg1
      [ $# -gt 0 ] || set -- -m 'not live'
      run in_dir services/calendar env FCS_TEST_DSN="$PG_DSN" uv run pytest "$@" ;;
    gateway)
      run sync_env services/gateway
      [ $# -gt 0 ] || set -- -q
      run in_dir services/gateway uv run pytest "$@" ;;
    calls)
      run sync_env services/gateway
      run sync_env services/calls
      [ $# -gt 0 ] || set -- -q
      run in_dir services/calls uv run pytest "$@" ;;
    go)
      [ $# -gt 0 ] || set -- ./...
      export GOMAXPROCS="$PARALLEL"
      run in_dir whatsapp/whatsmeow go vet ./...
      run in_dir whatsapp/whatsmeow go test -race -p "$PARALLEL" "$@" ;;
    build) run python3 scripts/build.py "$@" ;;
    frontend)
      run in_dir services/calls/frontend npm ci --ignore-scripts --no-audit --no-fund
      run in_dir services/calls/frontend npm test
      run in_dir services/calls/frontend npm run build ;;
    node)
      run node --test whatsapp/tests/*.test.mjs
      run npm test --prefix packages/npm ;;
    *) die "unknown suite '$SUITE'" ;;
  esac
}

# --run <worker> <suite> [args] (the mini, as _deedkitci) or --run-local <worker> <suite> <worktree> [args].
runner_run() {
  local dir src=""
  WORKER="$1" SUITE="$2"
  shift 2
  if [ "$LOCAL" = 1 ]; then src="$1"; shift; fi
  valid_worker "$WORKER" || die "bad worker name"
  # The script itself may have arrived on stdin; nothing the suite starts reads the rest of it.
  exec 0</dev/null
  runner_env
  dir="$ROOT_DIR/$WORKER"
  if [ "$LOCAL" = 0 ]; then admit run || exit $?; fi
  mkdir -p "$dir"
  if [ -f "$dir/run.pid" ] && kill -0 "$(cat "$dir/run.pid")" 2>/dev/null; then
    say "a run of $WORKER is still active"; exit "$REFUSED"
  fi
  set -m
  trap finish EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  ADMITTED=1 START_EPOCH=$SECONDS START_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo $$ > "$dir/run.pid"
  stop_leftovers "$WORKER"
  if [ "$LOCAL" = 0 ]; then
    renice -n "$NICE" -p $$ >/dev/null 2>&1 || true
    src="$dir/src"
    mkdir -p "$src"
    rsync "${RSYNC_COPY[@]}" "$SHARED/stage/$WORKER/" "$src/"
    cd "$src"
    # Tests read `git ls-files`; the copy has no history, so it gets a one-commit snapshot of itself.
    rm -rf .git
    git init -q
    git add -A
    git -c user.name=remote-test -c user.email=remote-test@invalid -c commit.gpgsign=false commit -qm snapshot --no-verify
  else
    cd "$src"
  fi
  say "running $SUITE for $WORKER on $(hostname -s)"
  run_suite "$dir" "$@"
}

here_stop() {
  local dir="$ROOT_DIR/$1" pid i
  [ -f "$dir/run.pid" ] || return 0
  pid="$(cat "$dir/run.pid")"
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -TERM "$pid" 2>/dev/null || return 0
  for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
}

# --here <action> [worker]: the small steps the laptop sends over ssh with this file on stdin.
runner_here() {
  local action="$1" worker="${2:-}" reason
  case "$action" in busy|load) ;; *) valid_worker "$worker" || die "bad worker name" ;; esac
  runner_env
  case "$action" in
    busy) if [ "$BUSY_CHECK" = 1 ] && reason="$(between_busy)"; then say "mini busy: $reason"; return "$REFUSED"; fi ;;
    probe) admit probe ;;
    stop) here_stop "$worker" ;;
    dist) tar -C "$ROOT_DIR/$worker/src/dist" -cf - . ;;
    load) cat "$ROOT_DIR/load.log" 2>/dev/null || true ;;
    cleanup)
      here_stop "$worker"
      stop_leftovers "$worker"
      rm -rf "${ROOT_DIR:?}/$worker"
      say "removed $ROOT_DIR/$worker" ;;
    *) die "unknown --here action" ;;
  esac
}

# --- On the laptop ------------------------------------------------------------------------------------

quoted() { local arg out=""; for arg in "$@"; do out="$out $(printf '%q' "$arg")"; done; printf '%s' "$out"; }

# As egoregerev on the mini, and as the service user through its shim (the only sudo it may run).
as_owner() { ssh "${SSH_OPTS[@]}" "$HOST" "bash -s --$(quoted "$@")" < "$SELF"; }
as_service() { ssh "${SSH_OPTS[@]}" "$HOST" "sudo -n -u $SERVICE_USER $SHIM$(quoted "$@")" < "$SELF"; }

local_run() {
  local tree="$1" suite="$2" worker="$3"
  shift 3
  say "running $suite on this machine (the local flow)"
  exec env LOCAL_RUN=1 DEEDKIT_CI_ROOT="${DEEDKIT_CI_LOCAL_ROOT:-${TMPDIR:-/tmp}/deedkit-ci}" \
    bash "$SELF" --run-local "$worker" "$suite" "$tree" "$@"
}

fallback() {
  local code="$1"
  shift
  [ "$FALLBACK" = 1 ] || exit "$code"
  local_run "$@"
}

laptop_run() {
  local tree suite="$2" worker stage status
  tree="$(cd "$1" && pwd)" || die "no worktree $1"
  shift 2
  git -C "$tree" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$tree is not a git worktree"
  worker="$(worker_of "$tree")"
  status=0
  as_owner --here busy || status=$?
  case "$status" in
    0) ;;
    "$REFUSED") fallback "$REFUSED" "$tree" "$suite" "$worker" "$@" ;;
    *) say "mini unreachable (ssh exit $status)"; fallback "$UNREACHABLE" "$tree" "$suite" "$worker" "$@" ;;
  esac
  status=0
  as_service --here probe "$worker" || status=$?
  case "$status" in
    0) ;;
    "$REFUSED") fallback "$REFUSED" "$tree" "$suite" "$worker" "$@" ;;
    255) say "mini unreachable"; fallback "$UNREACHABLE" "$tree" "$suite" "$worker" "$@" ;;
    *) say "the mini's service user $SERVICE_USER is not set up (exit $status)"; fallback "$NOT_SET_UP" "$tree" "$suite" "$worker" "$@" ;;
  esac
  stage="${DEEDKIT_REMOTE_STAGE:-${TMPDIR:-/tmp}/deedkit-remote-test/$worker}"
  rm -rf "$stage/src"
  mkdir -p "$stage/src"
  list_files "$tree" > "$stage/files"
  tar -C "$tree" --null -T "$stage/files" -cf - | tar -C "$stage/src" -xpf -
  ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $(printf '%q' "$SHARED/stage/$worker")"
  rsync "${RSYNC_COPY[@]}" -e "ssh ${SSH_OPTS[*]}" "$stage/src/" "$HOST:$SHARED/stage/$worker/"
  trap 'as_service --here stop "$worker" || true; exit 130' INT TERM HUP
  status=0
  as_service --run "$worker" "$suite" ${@+"$@"} || status=$?
  trap - INT TERM HUP
  case "$status" in
    "$REFUSED") fallback "$REFUSED" "$tree" "$suite" "$worker" "$@" ;;
    255) say "ssh to the mini failed mid-run; stopping it there"; as_service --here stop "$worker" || true ;;
  esac
  if [ "$suite" = build ] && [ "$status" = 0 ]; then
    mkdir -p "$tree/dist"
    as_service --here dist "$worker" | tar -C "$tree/dist" -xf -
  fi
  exit "$status"
}

case "${1:-}" in
  --run) shift; runner_run "$@" ;;
  --run-local)
    shift
    [ "${LOCAL_RUN:-0}" = 1 ] || die "--run-local is the fallback's own entry"
    LOCAL=1
    runner_run "$@" ;;
  --here) shift; runner_here "$@" ;;
  --list)
    [ $# = 2 ] || die "usage: $0 --list <worktree>"
    list_files "$2" | tr '\0' '\n' ;;
  --load) as_service --here load ;;
  --cleanup)
    [ $# = 2 ] || die "usage: $0 --cleanup <worker>"
    valid_worker "$2" || die "bad worker name"
    status=0
    as_service --here cleanup "$2" || status=$?
    ssh "${SSH_OPTS[@]}" "$HOST" "rm -rf $(printf '%q' "$SHARED/stage/$2")"
    exit "$status" ;;
  -*|"") die "usage: $0 <worktree> <suite> [args] | --cleanup <worker> | --load | --list <worktree>" ;;
  *)
    [ $# -ge 2 ] || die "usage: $0 <worktree> <suite> [args]"
    case "$SUITES" in *" $2 "*) ;; *) die "unknown suite '$2' (suites:$SUITES)" ;; esac
    laptop_run "$@" ;;
esac
