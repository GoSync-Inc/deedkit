#!/usr/bin/env bash
# Run a DeedKit test or build suite on the Mac mini (owner D41/D42) instead of the laptop.
#
#   scripts/remote-test.sh [--full] <worktree> <suite> [args...]   run <suite> there, stream its output, exit with its status
#   scripts/remote-test.sh --cleanup <worker>             remove the worker's copies and databases on the mini
#   scripts/remote-test.sh --load                         print the mini's load.log
#   scripts/remote-test.sh --list <worktree>              print the files a run sends
#
# Suites: root calendar gateway calls go build frontend node. GitHub CI runs the full suites (owner D46), so by
# default a run takes only what the worktree touched against main: ruff on the suite's changed Python files and
# its changed test modules (go: the whatsmeow tests when a whatsmeow file changed). Args replace that selection
# (pytest paths/options; go packages); --full runs the whole suite. build, frontend and node always run whole.
#
# A run copies the worktree's tracked and untracked-but-not-ignored files, never a secret-shaped one
# (is_secret), to /Users/Shared/deedkit-ci/stage/<worker> on the mini as the admin account, then runs the suite as
# the service user _deedkitci (sudo, docs/operations/remote-test-mini-setup.md) in its own copy
# ~_deedkitci/deedkit-ci/<worker>/src, with a disposable PostgreSQL 18 on 127.0.0.1, and streams the output
# back. Nothing of it uses docker there, so tests that need docker skip, as in the local flow without
# ANYMSG_TEST_POSTGRES_CONTAINER (CI covers them).
#
# The mini runs at most MAX_RUNS DeedKit suites at once and gives way to Between: while a Between container
# runs, the host load or free memory is past its cap, or the slots are taken, a run waits in the mini queue
# up to WAIT_SECONDS. Nothing runs on the laptop (owner 10-29): a run that cannot start exits 90 (mini busy
# or disk short), 91 (mini unreachable) or 92 (service user not set up), saying "CI covers": the pull
# request's GitHub-hosted CI runs every suite. Each mini run appends
# `worker suite start wall_s cpu_s pg_cpu_s exit` to ~_deedkitci/deedkit-ci/load.log (cpu_s: all of the
# run's processes, PostgreSQL included).
#
# Caps: /Users/Shared/deedkit-ci/caps.conf on the mini (KEY=value lines, defaults below), so they change
# without a pull request. Worker name: DEEDKIT_REMOTE_WORKER, else the worktree's last two path components.
set -euo pipefail

SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
HOST="${DEEDKIT_REMOTE_HOST:-}"   # else need_host reads HOST_FILE: the repository names no host or account
HOST_FILE="${HOME:-}/.config/deedkit/mini-host"
SERVICE_USER="${DEEDKIT_REMOTE_USER:-_deedkitci}"
SHIM="${DEEDKIT_REMOTE_SHIM:-/usr/local/libexec/deedkit-remote-test}"
SHARED="${DEEDKIT_REMOTE_SHARED:-/Users/Shared/deedkit-ci}"
SUITES=" root calendar gateway calls go build frontend node "
FULL=0
BUSY=90
UNREACHABLE=91
NOT_SET_UP=92
QUEUED=93   # between the laptop and the mini only: a run that did not start yet and asks for another try
QUIET_QUEUE=0

# Defaults of the caps; caps.conf overrides them (owner D42: Between has priority, one DeedKit run).
MAX_RUNS=1          # DeedKit runs at once on the mini
PARALLEL=2          # root pytest shards (each with its own database), go test -p and GOMAXPROCS
MIN_FREE_GB=15      # disk guard
MIN_FREE_MEM_GB=3   # free + inactive memory a run needs, so it never pushes the mini into swap
WAIT_SECONDS=1200   # how long a run waits in the mini queue before it gives up (CI covers)
POLL_SECONDS=15     # how often a waiting run looks again
BUSY_CHECK=1        # 1: wait while a Between container runs or the load or memory is past its cap
MAX_LOAD=8          # 1-minute load average of the mini's 12 cores
NICE=10             # the mini run's scheduling priority, below Between's
STOP_SECONDS=30     # how long a stop waits for TERM (PostgreSQL: QUIT) before it sends KILL
RUN_SECONDS=7200    # a run's lease: its watchdog sends the runner TERM past it, so a run whose laptop is gone
                    # (ssh dropped and the stop never arrived) still ends

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

# A path that may hold a secret, a session, a local environment or build output never leaves this machine. Every path
# component counts, in any letter case: a directory named like a secret takes all of its files with it.
# Source and docs that only name the subject in their own file name (tests/test_credential_expiry.py) stay;
# a path with a control character (a newline would split it for a line-based reader) never leaves.
is_secret() {
  local rest="$1" part last found=1
  case "$1" in *[[:cntrl:]]*) return 0 ;; esac
  shopt -s nocasematch
  while [ "$found" = 1 ]; do
    part="${rest%%/*}"
    if [ "$part" = "$rest" ]; then last=1; else last=0; fi
    case "$part" in
      .git|.venv|venv|node_modules|.tiden|.ssh|.gnupg|.aws|.azure|.kube|.docker|.gcloud|.config|__pycache__|.pytest_cache|.ruff_cache|.mypy_cache|dist) found=0 ;;
      .env*|*.env|*.pem|*.key|*.p8|*.der|*.crt|*.cer|*.p7b|*.p7c|*.csr|*.p12|*.pfx|*.jks|*.keystore|*.keytab) found=0 ;;
      *.ppk|*.jwk|*.gpg|*.asc|*.age|*.agekey|*.ovpn|*.kdbx|*.kdb|*.dsn) found=0 ;;
      id_rsa*|id_dsa*|id_ecdsa*|id_ed25519*|auth.json|.netrc|.npmrc|.pypirc|.git-credentials|.htpasswd) found=0 ;;
      *.session|*.session-*|*.sqlite|*.sqlite3|*.sqlite-*|*.sqlite3-*|*.db|*.db-*|*-journal|*-wal|*-shm) found=0 ;;
      *credential*|*token*|*secret*|*passw*)
        found=0
        if [ "$last" = 1 ]; then
          case "$part" in *.py|*.go|*.mjs|*.js|*.ts|*.tsx|*.jsx|*.md|*.sh) found=1 ;; esac
        fi ;;
    esac
    [ "$last" = 0 ] || break
    rest="${rest#*/}"
  done
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
# paths (environments, caches, build output) survive it; the secret shapes are a second net after is_secret
# (every pattern here is one is_secret drops too, so `--list` stays exactly what arrives).
RSYNC_COPY=(-a --delete --exclude=.git/ --exclude=.venv/ --exclude=node_modules/ --exclude=__pycache__/
  --exclude=.pytest_cache/ --exclude=.ruff_cache/ --exclude=dist/ --exclude='.env*' --exclude='*.pem'
  --exclude='*.key' --exclude=auth.json --exclude=.tiden/)

read_caps() {
  local file="$1" key value
  [ -f "$file" ] || return 0
  while IFS='=' read -r key value; do
    value="${value%%#*}"; value="${value//[[:space:]]/}"
    case "$key" in
      MAX_RUNS|PARALLEL|MIN_FREE_GB|MIN_FREE_MEM_GB|WAIT_SECONDS|POLL_SECONDS|BUSY_CHECK|MAX_LOAD|NICE|STOP_SECONDS|RUN_SECONDS)
        [[ "$value" =~ ^[0-9]+$ ]] || continue ;;
      *) continue ;;
    esac
    eval "$key=\$value"
  done < "$file"
  [ "$MAX_RUNS" -ge 1 ] || MAX_RUNS=1
  [ "$PARALLEL" -ge 1 ] || PARALLEL=1
  [ "$STOP_SECONDS" -ge 1 ] || STOP_SECONDS=1
  [ "$POLL_SECONDS" -ge 1 ] || POLL_SECONDS=1
}

# --- On the mini (as the admin account for the Between check, as _deedkitci for everything else) --------------

ROOT_DIR=""
WORKER=""
SUITE=""
MODE=full
RUFF=()
WORKER_DIR=""
RUN_DIR=""
HOLDER=""
WATCHDOG=""
CHILDREN=""
DATABASES=""
START_EPOCH=0
START_ISO=""

runner_env() {
  local dir
  ROOT_DIR="${DEEDKIT_CI_ROOT:-$HOME/deedkit-ci}"
  # The shim's PATH lacks Homebrew; node@24 is keg-only there (the repo's Node 24).
  for dir in ${DEEDKIT_RUNNER_PATH_DIRS-/usr/local/bin /opt/homebrew/sbin /opt/homebrew/bin /opt/homebrew/opt/node@24/bin}; do
    if [ -d "$dir" ]; then PATH="$dir:$PATH"; fi
  done
  export PATH
  read_caps "$SHARED/caps.conf"
}

load_1m() {
  local load
  load="$(sysctl -n vm.loadavg 2>/dev/null | awk '{print $2}')"
  if [ -z "$load" ] && [ -r /proc/loadavg ]; then load="$(cut -d' ' -f1 /proc/loadavg)"; fi
  printf '%s' "${load:-0}"
}

# Free + inactive + speculative pages (macOS), else MemAvailable (Linux), in whole GiB.
free_mem_gb() {
  local gb
  gb="$(vm_stat 2>/dev/null | awk '/page size of/ { for (i = 1; i <= NF; i++) if ($i == "of") size = $(i + 1) }
    /^Pages (free|inactive|speculative):/ { gsub(/\./, "", $NF); pages += $NF }
    END { if (size) print int(pages * size / 1073741824) }')"
  if [ -z "$gb" ] && [ -r /proc/meminfo ]; then gb="$(awk '/^MemAvailable:/ {print int($2 / 1048576)}' /proc/meminfo)"; fi
  printf '%s' "${gb:-0}"
}

# As the admin account: Between's containers on the default VM, a host load above MAX_LOAD (Between's own VM is
# invisible to this docker; its load shows on the host), or too little free memory.
between_busy() {
  local load mem
  if docker ps --format '{{.Names}} {{.Labels}}' 2>/dev/null | grep -Eq '^between-|io\.between\.'; then
    echo "a Between container is running"; return 0
  fi
  load="$(load_1m)"
  if awk -v l="$load" -v m="$MAX_LOAD" 'BEGIN { exit !(l > m) }'; then echo "host load $load is above $MAX_LOAD"; return 0; fi
  mem="$(free_mem_gb)"
  if [ "$mem" -lt "$MIN_FREE_MEM_GB" ]; then echo "${mem} GiB memory free, a run needs $MIN_FREE_MEM_GB"; return 0; fi
  return 1
}

# --here busy [deadline]: as the admin account, before every start attempt of a run. The first call sets the run's one
# queue deadline (now + WAIT_SECONDS, mini clock) and prints it; later calls, after a queued attempt, wait under
# that same deadline and refuse once it has passed.
wait_not_busy() {
  local deadline="$1" reason told=0 again=1
  if [ -z "$deadline" ]; then deadline=$(($(date +%s) + WAIT_SECONDS)); again=0; fi
  printf 'deadline=%s\n' "$deadline"
  while :; do
    reason=""
    if [ "$BUSY_CHECK" = 1 ]; then reason="$(between_busy)" || reason=""; fi
    if [ -z "$reason" ]; then
      if [ "$again" = 0 ] || [ "$(date +%s)" -lt "$deadline" ]; then return 0; fi
      reason="the DeedKit slot(s) stayed taken"
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then say "mini busy ($reason), CI covers"; return "$BUSY"; fi
    if [ "$told" = 0 ]; then say "waiting in the mini queue: $reason"; told=1; fi
    sleep "$POLL_SECONDS"
  done
}

free_gb() { df -Pk "$1" | awk 'NR==2 {print int($4 / 1048576)}'; }

# A slot is a flock held by a small python process of its own, never by a descriptor of this shell, so no
# child inherits it; the holder exits (and the slot frees) as soon as its runner is gone, even after SIGKILL.
LOCK_HOLDER='import fcntl, os, sys, time
f = open(sys.argv[1], "a")
try:
    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    print("busy", flush=True)
    sys.exit(1)
print("held", flush=True)
parent = int(sys.argv[2])
while parent and os.getppid() == parent:
    time.sleep(0.5)'

take_slot() {
  local slot note
  mkdir -p "$ROOT_DIR/.locks"
  for slot in $(seq 1 "$MAX_RUNS"); do
    note="$ROOT_DIR/.locks/holder.$$"
    rm -f "$note"
    python3 -c "$LOCK_HOLDER" "$ROOT_DIR/.locks/slot$slot" $$ > "$note" 2>/dev/null </dev/null &
    HOLDER=$!
    while [ ! -s "$note" ] && kill -0 "$HOLDER" 2>/dev/null; do sleep 0.1; done
    if [ "$(cat "$note" 2>/dev/null)" = held ]; then rm -f "$note"; return 0; fi
    wait "$HOLDER" 2>/dev/null || true
    HOLDER=""
    rm -f "$note"
  done
  return 1
}

# Admission as _deedkitci: the disk guard, then (a run) one try for a slot and, holding it, the load and memory
# caps again. A run that gets no slot or finds the mini busy holds nothing, waits POLL_SECONDS and returns
# QUEUED; the laptop then repeats the Between check (as the admin account) before it tries again, under one deadline.
admit() {
  local mode="$1" free reason=""
  mkdir -p "$ROOT_DIR"
  free="$(free_gb "$ROOT_DIR")"
  if [ "$free" -lt "$MIN_FREE_GB" ]; then say "mini disk guard: ${free} G free, need ${MIN_FREE_GB} G; CI covers"; return "$BUSY"; fi
  [ "$mode" = run ] || return 0
  if ! take_slot run; then
    reason="all $MAX_RUNS DeedKit slot(s) taken"
  elif [ "$BUSY_CHECK" = 1 ] && reason="$(between_busy)"; then
    kill "$HOLDER" 2>/dev/null || true
    wait "$HOLDER" 2>/dev/null || true
    HOLDER=""
  else
    return 0
  fi
  if [ "$QUIET_QUEUE" = 0 ]; then say "waiting in the mini queue: $reason"; fi
  sleep "$POLL_SECONDS"
  return "$QUEUED"
}

# CPU seconds (user + system) of this shell's finished children, from a `times` snapshot taken in the
# owning shell (in a subshell `times` would report the subshell's own children).
cpu_of() {
  tail -1 "$1" | awk '{ t = 0; for (i = 1; i <= 2; i++) { split($i, a, "m"); sub("s", "", a[2]); t += a[1] * 60 + a[2] }
    printf "%.1f", t }'
}

# Every process group and PostgreSQL a run starts is recorded in its own run directory with its start time,
# so a later stop (the watchdog, the next run, --cleanup) signals it only while that same process holds the id.
started() { ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' '_' || true; }
own() { printf '%s %s %s\n' "$1" "$2" "$(started "$2")" >> "$RUN_DIR/owned"; }

# Every recorded id leads its own process group (set -m). It is still this run's while its leader is the
# recorded process or, the leader gone, while members of its group remain: the system never hands out a pid
# that is still a process group id.
ours() {
  local now
  now="$(started "$1")"
  if [ -n "$now" ]; then [ "$now" = "$2" ]; return; fi
  kill -0 -- "-$1" 2>/dev/null
}

# signal_recorded <run dir> stop|kill: stop sends TERM to each group and QUIT (immediate shutdown) to
# PostgreSQL; kill sends KILL to every group. Only to what is still this run's.
signal_recorded() {
  local dir="$1" how="$2" kind id stamp
  [ -f "$dir/owned" ] || return 0
  while read -r kind id stamp; do
    [ -n "$stamp" ] && ours "$id" "$stamp" || continue
    case "$how:$kind" in
      stop:group) kill -TERM -- "-$id" 2>/dev/null || true ;;
      stop:postgres) kill -QUIT "$id" 2>/dev/null || true ;;
      kill:*) kill -KILL -- "-$id" 2>/dev/null || kill -KILL "$id" 2>/dev/null || true ;;
    esac
  done < "$dir/owned"
}

recorded_alive() {
  local dir="$1" kind id stamp
  [ -f "$dir/owned" ] || return 1
  while read -r kind id stamp; do
    [ -n "$stamp" ] && ours "$id" "$stamp" && return 0
  done < "$dir/owned"
  return 1
}

# Stop what a run recorded: TERM/QUIT, up to STOP_SECONDS, then KILL, up to 5 s more. Fails while any of it
# still runs, so the caller keeps the run directory (the record) for the next run or --cleanup to try again.
stop_recorded() {
  local dir="$1" i
  signal_recorded "$dir" stop
  for i in $(seq 1 "$STOP_SECONDS"); do recorded_alive "$dir" || return 0; sleep 1; done
  signal_recorded "$dir" kill
  for i in $(seq 1 5); do recorded_alive "$dir" || return 0; sleep 1; done
  return 1
}

# Runs apart from the runner: when the runner dies without its trap (SIGKILL), it stops what this run
# recorded, appends the run's load line (exit `killed`, CPU unknown: `-`) and removes this run's directory
# once all of it is gone, and touches nothing of another run; `finish` kills it first otherwise.
# It also holds the run's lease: past RUN_SECONDS it sends the runner TERM once (its trap stops the run), and
# KILL once the runner's own stop has had its bound, so a run the laptop lost (ssh down, no stop) ends.
start_watchdog() {
  local runner=$$ dir="$1" start
  start="$(date +%s)"
  ( trap '' HUP INT
    while kill -0 "$runner" 2>/dev/null; do
      age=$(($(date +%s) - start))
      if [ "$age" -ge $((RUN_SECONDS + 2 * STOP_SECONDS + 30)) ]; then kill -KILL "$runner" 2>/dev/null || true
      elif [ "$age" -ge "$RUN_SECONDS" ] && [ -z "${leased:-}" ]; then leased=1; kill -TERM "$runner" 2>/dev/null || true
      fi
      sleep 1
    done
    stopped=0
    stop_recorded "$dir" && stopped=1
    printf '%s\t%s\t%s\t%s\t-\t-\tkilled\n' "$WORKER" "$SUITE" "$START_ISO" "$(($(date +%s) - start))" \
      >> "$ROOT_DIR/load.log"
    if [ "$stopped" = 1 ]; then rm -rf "$dir"; fi ) </dev/null >/dev/null 2>&1 &
  WATCHDOG=$!
}

# What earlier runs of this worker left when neither their trap nor their watchdog finished: their own run
# directories only, never the current one. A directory whose processes do not stop stays for the next try,
# and the function then fails.
stop_leftovers() {
  local dir pidfile pid i kept=0
  for dir in "$WORKER_DIR"/runs/*; do
    [ -d "$dir" ] && [ "$dir" != "$RUN_DIR" ] || continue
    stop_recorded "$dir" || { say "a process of $dir does not stop; it stays recorded"; kept=1; continue; }
    # A PostgreSQL started just before its run died, so never recorded: known by its data directory.
    for pidfile in "$dir"/pg[0-9]*/cluster/postmaster.pid; do
      [ -f "$pidfile" ] || continue
      pid="$(head -1 "$pidfile")"
      [[ "$pid" =~ ^[0-9]+$ ]] || continue
      kill -0 "$pid" 2>/dev/null && ps -o command= -p "$pid" 2>/dev/null | grep -qF "$dir/pg" || continue
      kill -QUIT "$pid" 2>/dev/null || true
      for i in $(seq 1 "$STOP_SECONDS"); do ps -o command= -p "$pid" 2>/dev/null | grep -qF "$dir/pg" || continue 2; sleep 1; done
      ps -o command= -p "$pid" 2>/dev/null | grep -qF "$dir/pg" && kill -KILL "$pid" 2>/dev/null || true
    done
    rm -rf "$dir"
  done
  return "$kept"
}

# First thing in a step's own subshell: the step records itself (kind: group or postgres), then goes on only
# while the runner ($$) lives. A runner SIGKILLed the moment after starting a step so never leaves the step
# unrecorded: the record is written before the check, and the watchdog reads it only once the runner is gone.
own_self() {
  local pid
  pid="$(exec sh -c 'echo $PPID')"
  own "$1" "$pid"
  kill -0 $$ 2>/dev/null || exit 143
}

# Run a command as a background job (its own process group under set -m, so a stop reaches all of it) and
# wait for it; a trapped signal interrupts the wait at once.
run() {
  ( own_self group
    if [ "$(type -t "$1")" = function ]; then "$@"; else exec "$@"; fi ) &
  local pid=$!
  CHILDREN="$CHILDREN $pid"
  wait "$pid"
  CHILDREN="${CHILDREN% $pid}"
}

in_dir() { cd "$1" && shift && exec "$@"; }

PG_DSN=""
PG_BIN="${DEEDKIT_PG_BIN:-/opt/homebrew/opt/postgresql@18/bin}"

# PostgreSQL 18 from Homebrew as a child of this run, its cluster in the run directory, listening on
# 127.0.0.1 only. A child, so its CPU (and its backends') counts in `times` once it is reaped.
pg_start() {
  local name="$1" data password port i pid
  data="$RUN_DIR/$name"
  password="$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  mkdir -p "$data"
  chmod 700 "$data"
  (umask 077 && printf '%s\n' "$password" > "$data/pw")
  run "$PG_BIN/initdb" -D "$data/cluster" -U probe_owner --pwfile="$data/pw" --auth=scram-sha-256 -E UTF8 \
    --locale=en_US.UTF-8 >/dev/null
  rm -f "$data/pw"
  port="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
  # TCP only: a Unix socket under the run directory can pass macOS's 104-byte socket path limit. UTC, as CI's
  # postgres:18 container: initdb would take the mini's own zone, and timestamps would render in it.
  ( own_self postgres
    exec "$PG_BIN/postgres" -D "$data/cluster" -c listen_addresses=127.0.0.1 -p "$port" -c unix_socket_directories= \
      -c timezone=UTC -c log_timezone=UTC \
      -c shared_buffers=128MB -c fsync=off -c synchronous_commit=off -c full_page_writes=off ) > "$data/log" 2>&1 &
  pid=$!
  DATABASES="$DATABASES $pid"
  for i in $(seq 1 60); do
    if "$PG_BIN/pg_isready" -q -h 127.0.0.1 -p "$port"; then break; fi
    if [ "$i" = 60 ]; then say "postgres $name did not become ready"; cat "$data/log" >&2; return 1; fi
    sleep 1
  done
  PGPASSWORD="$password" "$PG_BIN/createdb" -h 127.0.0.1 -p "$port" -U probe_owner messaging_probe_test
  PG_DSN="postgresql://probe_owner:$password@127.0.0.1:$port/messaging_probe_test"
}

# Whether any process of these groups (this shell's own jobs) still runs.
groups_alive() {
  local pid
  for pid in "$@"; do kill -0 -- "-$pid" 2>/dev/null && return 0; done
  return 1
}

# Reap these jobs after KILL, waiting at most 5 s for each: a process stuck in the kernel never holds the run.
# Quiet: bash 3.2 would report each killed job ("Killed: 9") on the run's output.
reap_jobs() {
  local pid i
  for pid in "$@"; do
    for i in 1 2 3 4 5; do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null || wait "$pid" 2>/dev/null || true
  done
} 2>/dev/null

finish() {
  local status=$? pid pg=0 i stopped=1
  trap - EXIT INT TERM HUP
  if [ -n "$WATCHDOG" ]; then kill -KILL "$WATCHDOG" 2>/dev/null || true; reap_jobs "$WATCHDOG"; fi
  for pid in $CHILDREN; do kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true; done
  # Up to STOP_SECONDS for the groups to end, then KILL: a child that ignores TERM never holds the run open.
  for i in $(seq 1 "$STOP_SECONDS"); do groups_alive $CHILDREN || break; sleep 1; done
  for pid in $CHILDREN; do kill -KILL -- "-$pid" 2>/dev/null || true; done
  reap_jobs $CHILDREN
  times > "$RUN_DIR/.times-before"
  for pid in $DATABASES; do kill -QUIT "$pid" 2>/dev/null || true; done  # immediate shutdown
  for pid in $DATABASES; do
    for i in $(seq 1 "$STOP_SECONDS"); do groups_alive "$pid" || break; sleep 1; done
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
    reap_jobs "$pid"
  done
  times > "$RUN_DIR/.times"
  # Whatever recorded still runs (a group whose leader already ended, say) gets the same bounded stop. When
  # something does not stop, the run fails (an otherwise green run exits 1, and its load line says so) and its
  # directory, the record, stays for the next run or --cleanup.
  if ! stop_recorded "$RUN_DIR"; then
    stopped=0
    say "a process of this run does not stop; $RUN_DIR stays recorded"
    [ "$status" != 0 ] || status=1
  fi
  if [ -n "$DATABASES" ]; then
    pg="$(awk -v a="$(cpu_of "$RUN_DIR/.times")" -v b="$(cpu_of "$RUN_DIR/.times-before")" 'BEGIN { printf "%.1f", a - b }')"
  fi
  if [ "$SUITE" = root ]; then
    say "skipped: needs docker (CI covers): the modules that use ANYMSG_TEST_POSTGRES_CONTAINER or the docker CLI;" \
      "this is not the full suite"
  fi
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$WORKER" "$SUITE" "$START_ISO" "$((SECONDS - START_EPOCH))" \
    "$(cpu_of "$RUN_DIR/.times")" "$pg" "$status" >> "$ROOT_DIR/load.log"
  if [ "$(cat "$WORKER_DIR/run.pid" 2>/dev/null)" = "$$ $(started $$)" ]; then rm -f "$WORKER_DIR/run.pid"; fi
  if [ "$stopped" = 1 ]; then rm -rf "$RUN_DIR"; fi
  if [ -n "$HOLDER" ]; then kill "$HOLDER" 2>/dev/null || true; fi
  exit "$status"
}

write_dsn() { (umask 077 && printf '%s' "$PG_DSN" > "$1"); }

sync_env() { cd "$1" && exec uv sync --frozen --group dev -q; }

prefix() { awk -v p="$1" '{ print p $0; fflush() }'; }

suite_root() {
  local shard pid pids="" status=0 rc
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
    write_dsn "$RUN_DIR/test-1.dsn"
    [ $# -gt 0 ] || set -- --tb=short "${ignores[@]}" tests
    run env PROBE_TEST_DSN_FILE="$RUN_DIR/test-1.dsn" uv run pytest "$@"
    return
  fi
  # The full suite: PARALLEL file-level shards (scripts/python_shards.py, as CI), each with its own database.
  for shard in $(seq 1 "$PARALLEL"); do
    pg_start "pg$shard"
    write_dsn "$RUN_DIR/test-$shard.dsn"
  done
  for shard in $(seq 1 "$PARALLEL"); do
    (own_self group
      env DEEDKIT_TEST_SHARD="$shard/$PARALLEL" PROBE_TEST_DSN_FILE="$RUN_DIR/test-$shard.dsn" uv run python \
      scripts/python_shards.py pytest --tb=short "${ignores[@]}" tests 2>&1 | prefix "[$shard/$PARALLEL] ") &
    pid=$!
    pids="$pids $pid"
  done
  CHILDREN="$CHILDREN $pids"
  for pid in $pids; do
    rc=0; wait "$pid" || rc=$?
    [ "$status" != 0 ] || status=$rc
  done
  return "$status"
}

run_suite() {
  if [ "${#RUFF[@]}" -gt 0 ]; then run uv run --project tools/dev --locked ruff check "${RUFF[@]}"; fi
  if [ "$MODE" = modules ] && [ $# = 0 ]; then say "no touched test modules for $SUITE"; return 0; fi
  case "$SUITE" in
    root) suite_root "$@" ;;
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

active_pid() {
  local pid stamp
  [ -f "$1/run.pid" ] || return 1
  read -r pid stamp < "$1/run.pid" || return 1
  [[ "$pid" =~ ^[0-9]+$ ]] && [ -n "$stamp" ] && [ "$(started "$pid")" = "$stamp" ] && echo "$pid"
}

# --run <worker> <suite> [args]: as _deedkitci, with this script on stdin.
runner_run() {
  local src
  WORKER="$1" SUITE="$2" MODE="$3"
  shift 3
  while [ $# -gt 0 ] && [ "$1" != -- ]; do
    case "$1" in --ruff) RUFF+=("$2"); shift 2 ;; --again) QUIET_QUEUE=1; shift ;; *) die "unknown runner option $1" ;; esac
  done
  [ "${1:-}" != -- ] || shift
  valid_worker "$WORKER" || die "bad worker name"
  case "$MODE" in full|modules) ;; *) die "unknown mode $MODE" ;; esac
  # The script itself arrived on stdin; nothing the suite starts reads the rest of it.
  exec 0</dev/null
  runner_env
  WORKER_DIR="$ROOT_DIR/$WORKER"
  admit run || exit $?
  mkdir -p "$WORKER_DIR/runs"
  if active_pid "$WORKER_DIR" >/dev/null; then say "a run of $WORKER is still active; CI covers"; exit "$BUSY"; fi
  RUN_DIR="$WORKER_DIR/runs/$(date +%s)-$$"
  mkdir -p "$RUN_DIR"
  echo "$$ $(started $$)" > "$WORKER_DIR/run.pid"
  # A leftover that does not stop stays recorded (stop_leftovers says so) and this run goes on: the next run and
  # --cleanup try again, and --cleanup exits 1 while it stays.
  stop_leftovers || true
  set -m
  trap finish EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  START_EPOCH=$SECONDS START_ISO="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  start_watchdog "$RUN_DIR"
  renice -n "$NICE" -p $$ >/dev/null 2>&1 || true
  src="$WORKER_DIR/src"
  mkdir -p "$src"
  run rsync "${RSYNC_COPY[@]}" "$SHARED/stage/$WORKER/" "$src/"
  cd "$src"
  # Tests read `git ls-files`; the copy has no history, so it gets a one-commit snapshot of itself.
  rm -rf .git
  run git init -q
  run git add -A
  run git -c user.name=remote-test -c user.email=remote-test@invalid -c commit.gpgsign=false commit -qm snapshot --no-verify
  say "running $SUITE for $WORKER on $(hostname -s)"
  run_suite "$@"
}

here_stop() {
  local pid i
  pid="$(active_pid "$ROOT_DIR/$1")" || return 0
  kill -TERM "$pid" 2>/dev/null || return 0
  for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || return 0; sleep 1; done
}

# --here <action> [worker]: the small steps the laptop sends over ssh with this file on stdin.
runner_here() {
  local action="$1" worker="${2:-}"
  case "$action" in busy|load) ;; *) valid_worker "$worker" || die "bad worker name" ;; esac
  runner_env
  case "$action" in
    busy) [[ "$worker" =~ ^[0-9]*$ ]] || die "bad deadline"; wait_not_busy "$worker" ;;
    probe) admit probe ;;
    stop) here_stop "$worker" ;;
    dist) tar -C "$ROOT_DIR/$worker/src/dist" -cf - . ;;
    load) cat "$ROOT_DIR/load.log" 2>/dev/null || true ;;
    cleanup)
      here_stop "$worker"
      WORKER_DIR="$ROOT_DIR/$worker" RUN_DIR=""
      if ! stop_leftovers; then
        # The run records stay, so a later --cleanup (or the worker's next run) can stop what is left.
        rm -rf "${ROOT_DIR:?}/$worker/src"
        say "kept $ROOT_DIR/$worker/runs: a recorded process does not stop; run --cleanup again later"
        exit 1
      fi
      rm -rf "${ROOT_DIR:?}/$worker"
      say "removed $ROOT_DIR/$worker" ;;
    *) die "unknown --here action" ;;
  esac
}

# --- On the laptop ------------------------------------------------------------------------------------

quoted() { local arg out=""; for arg in "$@"; do out="$out $(printf '%q' "$arg")"; done; printf '%s' "$out"; }

# The mini's ssh destination (user@address or an ssh alias): DEEDKIT_REMOTE_HOST, else the first line of
# HOST_FILE, kept on the laptop only. Without one, or with one ssh would read as an option, nothing runs: CI covers.
need_host() {
  [ -n "$HOST" ] || HOST="$(head -1 "$HOST_FILE" 2>/dev/null || true)"
  case "$HOST" in
    "") say "no mini address (DEEDKIT_REMOTE_HOST or $HOST_FILE), CI covers"; exit "$UNREACHABLE" ;;
    -*|*[[:space:]]*) die "bad mini address in DEEDKIT_REMOTE_HOST or $HOST_FILE" ;;
  esac
}

# As the admin account on the mini, and as the service user through its shim (the only sudo it may run).
as_owner() { ssh "${SSH_OPTS[@]}" "$HOST" "bash -s --$(quoted "$@")" < "$SELF"; }
# From /: the admin account's home, the ssh session's directory, is closed to the service user.
as_service() { ssh "${SSH_OPTS[@]}" "$HOST" "cd / && sudo -n -u $SERVICE_USER $SHIM$(quoted "$@")" < "$SELF"; }

# Files changed against the merge base with main (committed or not) plus new untracked ones, one per line.
touched_files() {
  local tree="$1" base path
  base="$(git -C "$tree" merge-base HEAD origin/main 2>/dev/null || git -C "$tree" merge-base HEAD main 2>/dev/null || true)"
  { if [ -n "$base" ]; then git -C "$tree" diff -z --name-only --diff-filter=d "$base"; fi
    git -C "$tree" ls-files -z --others --exclude-standard; } | while IFS= read -r -d '' path; do
    if [ -f "$tree/$path" ] && ! is_secret "$path"; then printf '%s\n' "$path"; fi
  done | sort -u
}

# Owner D46: GitHub CI runs the full suites; by default the mini runs ruff on the suite's changed Python
# files and its touched test modules only. SELECTED gets the test args, RUFF the ruff paths (repo-relative).
SELECTED=()
select_touched() {
  local tree="$1" suite="$2" path dir
  case "$suite" in
    root) dir="" ;;
    calendar|gateway|calls) dir="services/$suite/" ;;
    go)
      if touched_files "$tree" | grep -Eq '^whatsapp/whatsmeow/(.*\.go|go\.mod|go\.sum|fork\.patch)$'; then SELECTED=(./...); fi
      return ;;
    *) return ;;
  esac
  while IFS= read -r path; do
    case "$path" in
      services/*) [ -n "$dir" ] || continue ;;
    esac
    case "$path" in "$dir"*) ;; *) continue ;; esac
    case "$path" in *.py) RUFF+=("$path") ;; *) continue ;; esac
    case "${path#"$dir"}" in tests/test_*.py|tests/*/test_*.py) SELECTED+=("${path#"$dir"}") ;; esac
  done < <(touched_files "$tree")
}

# The Between check as the admin account before each start attempt. The first call gets the run's one queue deadline
# (mini clock, WAIT_SECONDS from caps.conf) and every later call waits under it.
DEADLINE=""
wait_turn() {
  local out status=0
  out="$(as_owner --here busy ${DEADLINE:+"$DEADLINE"})" || status=$?
  case "$status" in
    0) ;;
    "$BUSY") return "$BUSY" ;;
    *) say "mini unreachable (ssh exit $status), CI covers"; return "$UNREACHABLE" ;;
  esac
  DEADLINE="$(printf '%s\n' "$out" | sed -n 's/^deadline=\([0-9][0-9]*\)$/\1/p' | tail -1)"
  [ -n "$DEADLINE" ] || { say "the mini sent no queue deadline, CI covers"; return "$UNREACHABLE"; }
}

laptop_run() {
  local tree suite="$2" worker stage status mode=modules
  tree="$(cd "$1" && pwd)" || die "no worktree $1"
  shift 2
  git -C "$tree" rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "$tree is not a git worktree"
  worker="$(worker_of "$tree")"
  case "$suite" in build|frontend|node) FULL=1 ;; esac
  if [ "$FULL" = 1 ]; then
    mode=full
  elif [ $# -gt 0 ]; then
    SELECTED=("$@")
  else
    select_touched "$tree" "$suite"
    if [ "${#SELECTED[@]}" = 0 ] && [ "${#RUFF[@]}" = 0 ]; then
      say "nothing touched for $suite (no changed Python file or test module); the full suite runs in CI (--full here)"
      exit 0
    fi
  fi
  need_host
  wait_turn || exit $?
  status=0
  as_service --here probe "$worker" || status=$?
  case "$status" in
    0) ;;
    "$BUSY") exit "$BUSY" ;;
    255) say "mini unreachable, CI covers"; exit "$UNREACHABLE" ;;
    *) say "the mini's service user $SERVICE_USER is not set up (exit $status), CI covers"; exit "$NOT_SET_UP" ;;
  esac
  # The local stage (kept until the next run of this worker) mirrors exactly the files that may leave.
  stage="${DEEDKIT_REMOTE_STAGE:-${TMPDIR:-/tmp}/deedkit-remote-test/$worker}"
  rm -rf "$stage/src"
  mkdir -p "$stage/src"
  list_files "$tree" > "$stage/files"
  tar -C "$tree" --null -T "$stage/files" -cf - | tar -C "$stage/src" -xpf -
  # The mini stage is read by _deedkitci, not its owner, the admin account: readable to all (rsync -a keeps modes).
  chmod -R go+rX "$stage/src"
  if ! ssh "${SSH_OPTS[@]}" "$HOST" "mkdir -p $(printf '%q' "$SHARED/stage/$worker")" ||
     ! rsync "${RSYNC_COPY[@]}" -e "ssh ${SSH_OPTS[*]}" "$stage/src/" "$HOST:$SHARED/stage/$worker/"; then
    say "copying to the mini failed before the suite started, CI covers"
    exit "$UNREACHABLE"
  fi
  local runner_args=() path again=""
  if [ "${#RUFF[@]}" -gt 0 ]; then for path in "${RUFF[@]}"; do runner_args+=(--ruff "$path"); done; fi
  runner_args+=(--)
  if [ "$mode" = full ]; then
    if [ $# -gt 0 ]; then runner_args+=("$@"); fi
  elif [ "${#SELECTED[@]}" -gt 0 ]; then
    runner_args+=("${SELECTED[@]}")
  fi
  trap 'as_service --here stop "$worker" || true; exit 130' INT TERM HUP
  while :; do
    status=0
    as_service --run "$worker" "$suite" "$mode" ${again:+--again} "${runner_args[@]}" || status=$?
    [ "$status" = "$QUEUED" ] || break
    again=1
    wait_turn || { status=$?; break; }
  done
  trap - INT TERM HUP
  if [ "$status" = 255 ]; then
    say "ssh to the mini failed mid-run; stopping it there"
    as_service --here stop "$worker" || true
  fi
  if [ "$suite" = build ] && [ "$status" = 0 ]; then
    mkdir -p "$tree/dist"
    as_service --here dist "$worker" | tar -C "$tree/dist" -xf -
  fi
  exit "$status"
}

case "${1:-}" in
  --run) shift; runner_run "$@" ;;
  --here) shift; runner_here "$@" ;;
  --list)
    [ $# = 2 ] || die "usage: $0 --list <worktree>"
    list_files "$2" | tr '\0' '\n' ;;
  --load) need_host; as_service --here load ;;
  --cleanup)
    [ $# = 2 ] || die "usage: $0 --cleanup <worker>"
    valid_worker "$2" || die "bad worker name"
    need_host
    status=0
    as_service --here cleanup "$2" || status=$?
    ssh "${SSH_OPTS[@]}" "$HOST" "rm -rf $(printf '%q' "$SHARED/stage/$2")"
    exit "$status" ;;
  --full|*)
    if [ "${1:-}" = --full ]; then FULL=1; shift; fi
    case "${1:-}" in -*|"") die "usage: $0 [--full] <worktree> <suite> [args] | --cleanup <worker> | --load | --list <worktree>" ;; esac
    [ $# -ge 2 ] || die "usage: $0 [--full] <worktree> <suite> [args]"
    case "$SUITES" in *" $2 "*) ;; *) die "unknown suite '$2' (suites:$SUITES)" ;; esac
    laptop_run "$@" ;;
esac
