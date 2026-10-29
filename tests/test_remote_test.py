"""scripts/remote-test.sh: DeedKit suites on the Mac mini as _deedkitci (owner D41/D42/D46).

The mini is this machine: fakes of ssh, sudo, docker, uv, go, npm, node, df, sysctl, vm_stat and the PostgreSQL
binaries stand in for it, and the shipped shim runs with only its user and home replaced. rsync, git, tar,
python3 and bash are real, so the copy, the secret filter, the traps and the accounting are the script's own.
"""
import fcntl
import getpass
import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'remote-test.sh'
SHIM = ROOT / 'deploy' / 'mac-mini' / 'deedkit-remote-test'
WORKER = 'infra_remote_test'
RSYNC = shutil.which('rsync')

pytestmark = pytest.mark.skipif(not RSYNC or not Path('/bin/bash').exists(),
                                reason='remote-test.sh needs /bin/bash and rsync')

BURN = 'python3 -c "import time; t = time.process_time()\nwhile time.process_time() - t < 0.3: pass"'
FAKES = {
    # Every ssh runs its command here, in the owner's home, with the caller's stdin. FAKE_SSH_DROP makes the
    # --run session end with 255 on SIGTERM while the run itself goes on, as a lost connection does.
    'ssh': '''#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_LOG"
[ "${FAKE_SSH_DOWN:-0}" = 1 ] && exit 255
case "$*" in *"${FAKE_SSH_FAIL:-no such command}"*) exit 255 ;; esac
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -[ntq]) shift ;; *) break ;; esac; done
shift
cd "$FAKE_OWNER_HOME" || exit 255
export HOME="$FAKE_OWNER_HOME"
case "${FAKE_SSH_DROP:-0}:$*" in
  1:*--run*) set -m; bash -c "$*" <&0 & echo "ssh-session pid=$$" >> "$FAKE_LOG"; trap 'exit 255' TERM; wait $! ;;
  *) exec bash -c "$*" ;;
esac
''',
    'sudo': '''#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
[ "${FAKE_NO_SERVICE:-0}" = 1 ] && { echo "sudo: unknown user _deedkitci" >&2; exit 1; }
while [ $# -gt 0 ]; do case "$1" in -n) shift ;; -u) shift 2 ;; *) break ;; esac; done
exec "$@"
''',
    # FAKE_DOCKER_PS_FILE: containers that can start or stop while a run waits.
    'docker': '''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
[ "$1" = ps ] && printf "%s" "${FAKE_DOCKER_PS:-}" && cat "${FAKE_DOCKER_PS_FILE:-/nonexistent}" 2>/dev/null
exit 0
''',
    'uv': f'''#!/usr/bin/env bash
[ "${{FAKE_UV_IGNORE_TERM:-0}}" = 0 ] || trap '' TERM
dsn=""; mode=""
if [ -n "${{PROBE_TEST_DSN_FILE:-}}" ]; then dsn="$(cat "$PROBE_TEST_DSN_FILE")"; mode="$(ls -l "$PROBE_TEST_DSN_FILE" | cut -c1-10)"; fi
echo "uv $* | cwd=${{PWD##*/}} dsn=$dsn dsnmode=$mode shard=${{DEEDKIT_TEST_SHARD:-}} fcs=${{FCS_TEST_DSN:-}} pid=$$" >> "$FAKE_LOG"
[ "$1" = run ] || exit 0
if [ "${{FAKE_UV_ORPHAN:-0}}" = 1 ]; then sleep 300 >/dev/null 2>&1 & echo "orphan pid=$!" >> "$FAKE_LOG"; fi
echo "FAKE SUITE OUTPUT ${{DEEDKIT_TEST_SHARD:-}}"
{BURN}
[ "${{FAKE_UV_SLEEP:-0}}" = 0 ] || sleep "$FAKE_UV_SLEEP"
exit "${{FAKE_UV_EXIT:-0}}"
''',
    'go': '#!/usr/bin/env bash\necho "go $* | cwd=${PWD##*/} gomaxprocs=${GOMAXPROCS:-}" >> "$FAKE_LOG"\n',
    'npm': '#!/usr/bin/env bash\necho "npm $* | cwd=${PWD##*/}" >> "$FAKE_LOG"\n',
    'node': '#!/usr/bin/env bash\necho "node $*" >> "$FAKE_LOG"\n',
    'df': '''#!/usr/bin/env bash
echo "Filesystem 1024-blocks Used Available Capacity Mounted"
echo "/dev/fake 999999999 1 $((${FAKE_FREE_GB:-100} * 1048576)) 1% /"
''',
    'sysctl': '#!/usr/bin/env bash\necho "{ ${FAKE_LOAD:-1.00} 1.00 1.00 }"\n',
    'vm_stat': '''#!/usr/bin/env bash
echo "Mach Virtual Memory Statistics: (page size of 1048576 bytes)"
echo "Pages free:                               $((${FAKE_FREE_MEM_GB:-16} * 1024))."
echo "Pages inactive:                                0."
echo "Pages speculative:                             0."
''',
}
# Only on the runner's PATH: a copy and a cluster setup that can be made slow, to kill the runner during them.
RUNNER_FAKES = {
    'rsync': f'#!/usr/bin/env bash\necho "runner-rsync pid=$$" >> "$FAKE_LOG"\n'
             f'[ "${{FAKE_RSYNC_SLEEP:-0}}" = 0 ] || sleep "$FAKE_RSYNC_SLEEP"\nexec {RSYNC} "$@"\n',
    # A start-time lookup (how the runner records a step) waits while the file FAKE_PS_HOLD exists.
    'ps': '''#!/usr/bin/env bash
case "$*" in *lstart=*)
  if [ -n "${FAKE_PS_HOLD:-}" ] && [ -e "$FAKE_PS_HOLD" ]; then
    echo "ps-hold pid=$$" >> "$FAKE_LOG"
    while [ -e "$FAKE_PS_HOLD" ]; do sleep 0.05; done
  fi ;;
esac
exec /bin/ps "$@"
''',
}
PG_FAKES = {
    'initdb': '''#!/usr/bin/env bash
echo "initdb $* pid=$$" >> "$FAKE_LOG"
[ "${FAKE_INITDB_SLEEP:-0}" = 0 ] || sleep "$FAKE_INITDB_SLEEP"
while [ $# -gt 0 ]; do [ "$1" = -D ] && mkdir -p "$2"; shift; done
''',
    # A Unix socket path past macOS's 104 bytes (`<dir>/.s.PGSQL.<port>`) fails startup, as it does there.
    'postgres': f'''#!/usr/bin/env bash
echo "postgres $* pid=$$" >> "$FAKE_LOG"
case " $* " in *" -k "*) k="${{*##* -k }}"; k="${{k%% *}}"
  [ "${{#k}}" -le 88 ] || {{ echo "postgres: socket path too long" >> "$FAKE_LOG"; exit 1; }} ;; esac
echo $$ > "$2/postmaster.pid"
trap '{BURN}; echo "postgres stopped by QUIT pid=$$" >> "$FAKE_LOG"; exit 0' QUIT
touch "$FAKE_LOG.ready.$(printf '%s\\n' "$*" | sed -n 's/.* -p \\([0-9][0-9]*\\) .*/\\1/p')"
while :; do sleep 0.1; done
''',
    # Ready once that server has started (and so recorded itself), as the real one.
    'pg_isready': '''#!/usr/bin/env bash
while [ $# -gt 0 ]; do [ "$1" = -p ] && port="$2"; shift; done
[ -e "$FAKE_LOG.ready.${port:-}" ]
''',
    # FAKE_ARM_PS_HOLD: the next start-time lookup after the database exists (the suite step's record) waits.
    'createdb': '''#!/usr/bin/env bash
echo "createdb $*" >> "$FAKE_LOG"
[ "${FAKE_ARM_PS_HOLD:-0}" = 0 ] || touch "$FAKE_PS_HOLD"
''',
}
CAPS = 'WAIT_SECONDS=0\nPOLL_SECONDS=1\nSTOP_SECONDS=3\n'


def write(path, text, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


@pytest.fixture
def mini(tmp_path):
    for folder, fakes in (('bin', FAKES), ('rbin', RUNNER_FAKES), ('pgbin', PG_FAKES)):
        for name, text in fakes.items():
            write(tmp_path / folder / name, text)
    for name in ('owner', 'service', 'shared/stage'):
        (tmp_path / name).mkdir(parents=True)
    write(tmp_path / 'shared' / 'caps.conf', CAPS, 0o644)
    # The shipped shim, with this machine's user and a temporary home in place of _deedkitci's.
    shim = SHIM.read_text().replace('/Users/_deedkitci', str(tmp_path / 'service'))
    write(tmp_path / 'shim', shim.replace('_deedkitci', getpass.getuser()))
    log = tmp_path / 'log'
    log.touch()
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(log),
               FAKE_OWNER_HOME=str(tmp_path / 'owner'), DEEDKIT_REMOTE_HOST='mini',
               DEEDKIT_REMOTE_SHIM=str(tmp_path / 'shim'), DEEDKIT_REMOTE_SHARED=str(tmp_path / 'shared'),
               DEEDKIT_REMOTE_STAGE=str(tmp_path / 'stage'),
               DEEDKIT_RUNNER_PATH_DIRS=f"{tmp_path / 'bin'} {tmp_path / 'rbin'}",
               DEEDKIT_PG_BIN=str(tmp_path / 'pgbin'), DEEDKIT_REMOTE_WORKER=WORKER)
    for name in ('DEEDKIT_CI_ROOT', 'PROBE_TEST_DSN_FILE', 'FCS_TEST_DSN', 'DEEDKIT_TEST_SHARD'):
        env.pop(name, None)
    return tmp_path, env


def git(tree, *args):
    subprocess.run(['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@invalid',
                    '-c', 'commit.gpgsign=false', *args], check=True, capture_output=True)


@pytest.fixture
def worktree(tmp_path):
    tree = tmp_path / 'worktrees' / 'infra' / 'remote-test'
    files = {'app.py': 'print(1)\n', 'tests/test_app.py': 'def test_app(): pass\n',
             'services/gateway/tests/test_credential_expiry.py': 'def test_expiry(): pass\n',
             'services/calendar/pyproject.toml': '', 'services/calls/pyproject.toml': '',
             'services/calls/frontend/package.json': '{}\n', 'whatsapp/tests/a.test.mjs': '',
             'whatsapp/whatsmeow/go.mod': 'module x\n', 'packages/npm/package.json': '{}\n',
             'scripts/python_shards.py': '', 'gone.py': 'deleted below\n', '.gitignore': 'ignored.log\ndist/\n',
             'scripts/build.py': 'from pathlib import Path\nPath("dist").mkdir(exist_ok=True)\n'
                                 'Path("dist/anymsg-0.0.0.whl").write_text("synthetic")\n',
             # Secret-shaped files that are tracked anyway.
             'deploy/.env.example': 'A=1\n', 'deploy/telegram-credentials.example.json': '{}\n',
             'keys/server.pem': 'synthetic\n', 'config/api_token.json': '{}\n', 'x/auth.json': '{}\n'}
    for name, text in files.items():
        write(tree / name, text, 0o644)
    subprocess.run(['git', '-c', 'init.defaultBranch=main', 'init', '-q', str(tree)], check=True)
    git(tree, 'add', '-A')
    git(tree, 'commit', '-qm', 'synthetic')
    (tree / 'gone.py').unlink()
    # Untracked, not ignored: the working diff, and secrets a worker might leave lying around.
    untracked = {'new_module.py': 'x = 1\n', '.env': 'K=synthetic\n', 'local.env': 'K=1\n', 'id_ed25519': 'k\n',
                 '.tiden/session.json': '{}\n', '.venv/lib/site.py': '', 'node_modules/a/index.js': '',
                 'creds/credentials': 'k\n', 'tls.key': 'k\n', '.netrc': 'k\n', '.npmrc': 'k\n',
                 'test.dsn': 'postgresql://x\n', 'telegram.session': 'k\n', 'ignored.log': 'x\n',
                 'whatsapp/session.sqlite': 'k\n', 'secrets/token.txt': 'k\n',
                 'credentials/google.json': '{}\n', '.AWS/sso/cache/entry.json': '{}\n',
                 'whatsapp/session.sqlite-wal': 'k\n', 'data/store.db-journal': 'k\n', 'Secrets/notes.txt': 'k\n',
                 'TOKENS/github': 'k\n', 'state/.ENV': 'K=1\n', 'safe\nname/session.sqlite': 'k\n',
                 'credential.py/credential.py': 'k\n', 'client.p8': 'k\n', 'tls.der': 'k\n', '.envbackup': 'K=1\n',
                 'certs/ca.crt': 'k\n'}
    for name, text in untracked.items():
        write(tree / name, text, 0o644)
    # An ordinary file only its owner may read: _deedkitci, another user, must still read its staged copy.
    write(tree / 'private_notes.txt', 'synthetic\n', 0o600)
    return tree


SENT = {'app.py', 'tests/test_app.py', 'services/gateway/tests/test_credential_expiry.py',
        'services/calendar/pyproject.toml', 'services/calls/pyproject.toml', 'services/calls/frontend/package.json',
        'whatsapp/tests/a.test.mjs', 'whatsapp/whatsmeow/go.mod', 'packages/npm/package.json',
        'scripts/python_shards.py', 'scripts/build.py', '.gitignore', 'new_module.py', 'private_notes.txt'}


def remote(env, *args, **extra):
    return subprocess.run(['bash', str(SCRIPT), *map(str, args)], env=dict(env, **extra), capture_output=True,
                          text=True, timeout=120)


def background(env, *args, **extra):
    return subprocess.Popen(['bash', str(SCRIPT), *map(str, args)], env=dict(env, **extra), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)


def files_under(root):
    return {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts}


def log_lines(home, prefix):
    return [line for line in (home / 'log').read_text().splitlines() if line.startswith(prefix)]


def pid_of(line):
    return int(line.split('pid=')[1].split()[0])


def worker_dir(home):
    return home / 'service' / 'deedkit-ci' / WORKER


def runner_pid(home):
    return int((worker_dir(home) / 'run.pid').read_text().split()[0])


def recorded(home, pid):
    """Whether a run has recorded this step. A step records itself before it runs anything, so this holds once
    the step has logged; test_a_runner_killed_the_moment_a_step_starts_leaves_no_step_behind kills without it."""
    runs = worker_dir(home) / 'runs'
    return runs.exists() and any(f'group {pid} ' in (run / 'owned').read_text()
                                 for run in runs.iterdir() if (run / 'owned').exists())


def load_rows(home):
    return [line.split('\t') for line in (home / 'service' / 'deedkit-ci' / 'load.log').read_text().splitlines()]


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(condition, seconds=30):
    deadline = time.time() + seconds
    while not condition():
        assert time.time() < deadline, 'timed out'
        time.sleep(0.2)


# spec: ANYM-638
def test_the_list_holds_tracked_and_untracked_files_but_no_secret_environment_or_deleted_file(mini, worktree):
    _, env = mini
    result = remote(env, '--list', worktree)
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split('\n')) - {''} == SENT


# spec: ANYM-638
def test_a_run_sends_exactly_the_listed_files_and_runs_the_suite_as_the_service_user(mini, worktree):
    home, env = mini
    result = remote(env, '--full', worktree, 'gateway')
    assert result.returncode == 0, result.stderr
    assert 'FAKE SUITE OUTPUT' in result.stdout
    assert files_under(home / 'stage' / 'src') == SENT
    assert files_under(home / 'shared' / 'stage' / WORKER) == SENT
    # The mini stage belongs to egoregerev and is read by _deedkitci: every staged file is readable by others.
    assert (home / 'shared' / 'stage' / WORKER / 'private_notes.txt').stat().st_mode & 0o044 == 0o044
    assert (worktree / 'private_notes.txt').stat().st_mode & 0o777 == 0o600
    copy = worker_dir(home) / 'src'
    assert files_under(copy) == SENT
    # The copy is a one-commit repository of itself, for the tests that read `git ls-files`.
    tracked = subprocess.run(['git', '-C', str(copy), 'ls-files'], capture_output=True, text=True, check=True)
    assert set(tracked.stdout.split()) == SENT
    assert any(f'-u _deedkitci {home / "shim"} --run {WORKER} gateway full' in line for line in log_lines(home, 'sudo'))
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')


# spec: ANYM-638
def test_a_second_run_removes_what_the_worktree_no_longer_has_and_keeps_the_environment(mini, worktree):
    home, env = mini
    assert remote(env, '--full', worktree, 'gateway').returncode == 0
    copy = worker_dir(home) / 'src'
    write(copy / '.venv' / 'kept.py', '', 0o644)
    (worktree / 'new_module.py').unlink()
    assert remote(env, '--full', worktree, 'gateway').returncode == 0
    assert files_under(copy) == SENT - {'new_module.py'} | {'.venv/kept.py'}


# spec: ANYM-638
def test_by_default_a_run_takes_ruff_and_the_touched_test_modules_of_its_suite(mini, worktree):
    home, env = mini
    git(worktree, 'checkout', '-qb', 'feature')
    (worktree / 'tests' / 'test_app.py').write_text('def test_app(): assert True\n')
    (worktree / 'app.py').write_text('print(2)\n')
    write(worktree / 'services' / 'calendar' / 'tests' / 'service' / 'test_cal.py', 'def test_cal(): pass\n', 0o644)
    assert remote(env, worktree, 'root').returncode == 0
    [ruff] = log_lines(home, 'uv run --project tools/dev --locked ruff check')
    assert ruff.split(' |')[0].split('ruff check ')[1].split() == ['app.py', 'new_module.py', 'tests/test_app.py']
    [pytest_line] = log_lines(home, 'uv run pytest')
    assert pytest_line.split(' |')[0] == 'uv run pytest tests/test_app.py'
    assert remote(env, worktree, 'calendar').returncode == 0
    assert log_lines(home, 'uv run pytest tests/service/test_cal.py | cwd=calendar')
    assert log_lines(home, 'uv run --project tools/dev --locked ruff check services/calendar/tests/service/test_cal.py')


# spec: ANYM-638
def test_a_suite_with_nothing_touched_runs_nothing_and_says_ci_covers_it(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'gateway')
    assert result.returncode == 0
    assert 'nothing touched for gateway' in result.stderr and 'runs in CI' in result.stderr
    assert not log_lines(home, 'ssh')


# spec: ANYM-638
def test_the_root_suite_gets_a_postgres_on_localhost_and_a_private_dsn_file(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root', 'tests/test_app.py')
    assert result.returncode == 0, result.stderr
    [postgres] = log_lines(home, 'postgres -D')
    assert '-c listen_addresses=127.0.0.1' in postgres
    assert '-c unix_socket_directories= ' in postgres and ' -k ' not in postgres
    assert '-c timezone=UTC ' in postgres  # as CI's postgres:18 container, whatever the mini's own zone
    port = postgres.split(' -p ')[1].split()[0]
    [pytest_line] = log_lines(home, 'uv run pytest tests/test_app.py')
    assert 'dsnmode=-rw------- ' in pytest_line
    dsn_text = pytest_line.split('dsn=')[1].split()[0]
    assert dsn_text.startswith('postgresql://probe_owner:')
    assert dsn_text.endswith(f'@127.0.0.1:{port}/messaging_probe_test')
    syncs = [line.split('cwd=')[1].split()[0] for line in log_lines(home, 'uv sync --frozen --group dev')]
    assert syncs == ['src', 'gateway', 'calendar']
    # Stopped at exit (immediate shutdown); the run's directory, its cluster and DSN file are gone.
    assert log_lines(home, 'postgres stopped by QUIT')
    assert not list((worker_dir(home) / 'runs').iterdir())
    assert 'skipped: needs docker (CI covers)' in result.stderr


# spec: ANYM-638
def test_a_long_worker_name_still_gets_its_database(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root', 'tests/test_app.py', DEEDKIT_REMOTE_WORKER='w' * 63)
    assert result.returncode == 0, result.stderr
    assert not log_lines(home, 'postgres: socket path too long')
    assert log_lines(home, 'uv run pytest tests/test_app.py')


# spec: ANYM-638
def test_the_full_root_suite_runs_parallel_shards_each_with_its_own_database(mini, worktree):
    home, env = mini
    result = remote(env, '--full', worktree, 'root')
    assert result.returncode == 0, result.stderr
    lines = log_lines(home, 'uv run python scripts/python_shards.py')
    assert sorted(line.split('shard=')[1].split()[0] for line in lines) == ['1/2', '2/2']
    assert len({line.split('dsn=')[1].split()[0] for line in lines}) == 2
    assert len(log_lines(home, 'postgres -D')) == 2
    assert '[1/2] FAKE SUITE OUTPUT 1/2' in result.stdout and '[2/2] FAKE SUITE OUTPUT 2/2' in result.stdout


# spec: ANYM-638
def test_caps_on_the_mini_drop_the_root_suite_to_one_process_without_a_code_change(mini, worktree):
    home, env = mini
    write(home / 'shared' / 'caps.conf', CAPS + 'PARALLEL=1\nMAX_RUNS=1\n', 0o644)
    assert remote(env, '--full', worktree, 'root').returncode == 0
    assert not log_lines(home, 'uv run python scripts/python_shards.py')
    assert len(log_lines(home, 'uv run pytest --tb=short')) == 1


# spec: ANYM-638
def test_the_go_frontend_and_node_suites_run_the_ci_commands(mini, worktree):
    home, env = mini
    for args in (('--full', worktree, 'go'), (worktree, 'frontend'), (worktree, 'node')):
        result = remote(env, *args)
        assert result.returncode == 0, result.stderr
    assert log_lines(home, 'go vet ./... | cwd=whatsmeow gomaxprocs=2')
    assert log_lines(home, 'go test -race -p 2 ./... | cwd=whatsmeow gomaxprocs=2')
    assert [line.split(' |')[0] for line in log_lines(home, 'npm')] == [
        'npm ci --ignore-scripts --no-audit --no-fund', 'npm test', 'npm run build', 'npm test --prefix packages/npm']
    assert log_lines(home, 'node --test whatsapp/tests/a.test.mjs')


# spec: ANYM-638
def test_the_build_suite_brings_dist_back_to_the_worktree(mini, worktree):
    _, env = mini
    result = remote(env, worktree, 'build')
    assert result.returncode == 0, result.stderr
    assert (worktree / 'dist' / 'anymsg-0.0.0.whl').read_text() == 'synthetic'


# spec: ANYM-638
def test_the_disk_guard_refuses_under_15_gigabytes_before_anything_runs(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root', FAKE_FREE_GB='14')
    assert result.returncode == 90
    assert 'disk guard: 14 G free, need 15 G' in result.stderr and 'CI covers' in result.stderr
    assert not log_lines(home, 'postgres') and not log_lines(home, 'uv')
    assert not (home / 'shared' / 'stage' / WORKER).exists()


# spec: ANYM-638
def test_a_failing_suite_stops_postgres_and_returns_the_suite_status(mini, worktree):
    home, env = mini
    result = remote(env, '--full', worktree, 'calendar', FAKE_UV_EXIT='3')
    assert result.returncode == 3
    assert log_lines(home, 'postgres stopped by QUIT')
    assert 'fcs=postgresql://probe_owner:' in log_lines(home, 'uv run pytest -m not live')[0]


# spec: ANYM-638
def test_a_terminated_run_stops_its_children_and_postgres(mini, worktree):
    home, env = mini
    process = background(env, worktree, 'root', 'tests/test_app.py', FAKE_UV_SLEEP='60')
    wait_for(lambda: log_lines(home, 'uv run pytest'), 60)
    os.kill(runner_pid(home), signal.SIGTERM)
    _, stderr = process.communicate(timeout=30)
    assert process.returncode == 143, stderr
    assert log_lines(home, 'postgres stopped by QUIT')
    assert not alive(pid_of(log_lines(home, 'uv run pytest')[0]))
    assert not (worker_dir(home) / 'run.pid').exists()
    assert load_rows(home)[-1][1] == 'root' and load_rows(home)[-1][-1] == '143'


# spec: ANYM-638
@pytest.mark.parametrize('signal_name', ['TERM', 'KILL'])
def test_a_suite_that_ignores_term_is_killed_once_the_stop_bound_passes(mini, worktree, signal_name):
    home, env = mini
    process = background(env, worktree, 'root', 'tests/test_app.py', FAKE_UV_SLEEP='120', FAKE_UV_IGNORE_TERM='1')
    wait_for(lambda: log_lines(home, 'uv run pytest'), 60)
    suite = pid_of(log_lines(home, 'uv run pytest')[0])
    wait_for(lambda: recorded(home, suite))
    os.kill(runner_pid(home), getattr(signal, f'SIG{signal_name}'))
    process.communicate(timeout=60)
    # STOP_SECONDS=3: TERM, then KILL; then PostgreSQL, the run directory and the load line.
    wait_for(lambda: not alive(suite), 30)
    wait_for(lambda: not list((worker_dir(home) / 'runs').iterdir()), 30)
    assert log_lines(home, 'postgres stopped by QUIT')
    assert load_rows(home)[-1][-1] == {'TERM': '143', 'KILL': 'killed'}[signal_name]


# spec: ANYM-639
def test_a_runner_killed_the_moment_a_step_starts_leaves_no_step_behind(mini, worktree):
    home, env = mini
    hold = home / 'ps-hold'
    # The suite step's record waits on the hold; the runner dies inside that window, then the record goes on.
    process = background(env, worktree, 'root', 'tests/test_app.py', FAKE_PS_HOLD=str(hold), FAKE_ARM_PS_HOLD='1',
                         FAKE_UV_SLEEP='120', FAKE_UV_IGNORE_TERM='1')
    wait_for(lambda: log_lines(home, 'ps-hold'), 60)
    os.kill(runner_pid(home), signal.SIGKILL)
    hold.unlink()
    process.communicate(timeout=60)
    wait_for(lambda: not list((worker_dir(home) / 'runs').iterdir()), 30)
    # Recorded, the step found its runner gone: no suite started, so none runs.
    assert not any(alive(pid_of(line)) for line in log_lines(home, 'uv run pytest'))
    assert not log_lines(home, 'uv run pytest')


# spec: ANYM-639
def test_a_descendant_left_by_a_finished_step_is_stopped_before_the_run_ends(mini, worktree):
    home, env = mini
    result = remote(env, '--full', worktree, 'gateway', FAKE_UV_ORPHAN='1')
    assert result.returncode == 0, result.stderr
    # The step's leader exited; its background child still held the step's process group.
    orphans = [pid_of(line) for line in log_lines(home, 'orphan')]
    assert orphans
    wait_for(lambda: not any(alive(pid) for pid in orphans), 10)
    assert not list((worker_dir(home) / 'runs').iterdir())


# spec: ANYM-638
@pytest.mark.parametrize('stage', ['copy', 'initdb', 'suite'])
def test_a_killed_runner_leaves_no_process_database_or_held_slot_behind(mini, worktree, stage):
    home, env = mini
    slow, marker = {'copy': ('FAKE_RSYNC_SLEEP', 'runner-rsync'), 'initdb': ('FAKE_INITDB_SLEEP', 'initdb'),
                    'suite': ('FAKE_UV_SLEEP', 'uv run pytest')}[stage]
    process = background(env, worktree, 'root', 'tests/test_app.py', **{slow: '60'})
    wait_for(lambda: log_lines(home, marker), 60)
    wait_for(lambda: recorded(home, pid_of(log_lines(home, marker)[0])))
    os.kill(runner_pid(home), signal.SIGKILL)
    process.communicate(timeout=30)
    # The watchdog stops the orphan and any PostgreSQL; the slot died with the runner.
    wait_for(lambda: not alive(pid_of(log_lines(home, marker)[0])))
    wait_for(lambda: not list((worker_dir(home) / 'runs').iterdir()))
    for line in log_lines(home, 'postgres -D'):
        assert not alive(pid_of(line))
    # The watchdog accounts for the killed run; its CPU is unknown.
    assert load_rows(home)[-1][:2] == [WORKER, 'root'] and load_rows(home)[-1][4:] == ['-', '-', 'killed']
    write(home / 'shared' / 'caps.conf', 'WAIT_SECONDS=10\nPOLL_SECONDS=1\n', 0o644)
    again = remote(env, '--full', worktree, 'gateway')
    assert again.returncode == 0, again.stderr


# spec: ANYM-638
def test_a_run_started_right_after_a_killed_one_is_not_touched_by_the_old_watchdog(mini, worktree):
    home, env = mini
    write(home / 'shared' / 'caps.conf', 'WAIT_SECONDS=10\nPOLL_SECONDS=1\n', 0o644)
    process = background(env, worktree, 'root', 'tests/test_app.py', FAKE_UV_SLEEP='60')
    wait_for(lambda: log_lines(home, 'uv run pytest'), 60)
    wait_for(lambda: recorded(home, pid_of(log_lines(home, 'uv run pytest')[0])))
    os.kill(runner_pid(home), signal.SIGKILL)
    process.communicate(timeout=30)
    # At once: the old watchdog wakes while this run's suite and database are up, and must leave them be.
    again = remote(env, worktree, 'root', 'tests/test_app.py', FAKE_UV_SLEEP='4')
    assert again.returncode == 0, again.stderr
    # This run's line and the killed run's (from its watchdog), in either order.
    wait_for(lambda: sorted(row[-1] for row in load_rows(home)) == ['0', 'killed'])


# spec: ANYM-638
def test_each_mini_run_appends_one_load_line(mini, worktree):
    home, env = mini
    assert remote(env, worktree, 'root', 'tests/test_app.py').returncode == 0
    assert remote(env, '--full', worktree, 'gateway').returncode == 0
    rows = load_rows(home)
    assert [(row[0], row[1], row[-1]) for row in rows] == [(WORKER, 'root', '0'), (WORKER, 'gateway', '0')]
    (_, _, start, wall, cpu, pg, _), (_, _, _, _, gateway_cpu, gateway_pg, _) = rows
    assert start.endswith('Z') and int(wall) >= 0
    # The suite and the database each burn 0.3 s of CPU in their fakes.
    assert float(pg) >= 0.2 and float(cpu) >= float(pg) + 0.2
    assert float(gateway_cpu) >= 0.2 and gateway_pg == '0'
    assert remote(env, '--load').stdout.count(WORKER) == 2


# spec: ANYM-638
def test_a_taken_slot_makes_the_run_wait_in_the_mini_queue_then_ci_covers(mini, worktree):
    home, env = mini
    locks = home / 'service' / 'deedkit-ci' / '.locks'
    locks.mkdir(parents=True)
    with open(locks / 'slot1', 'a') as slot:
        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = remote(env, '--full', worktree, 'gateway')
        assert refused.returncode == 90, refused.stderr
        assert 'all 1 DeedKit slot(s) taken' in refused.stderr and 'CI covers' in refused.stderr
        assert not log_lines(home, 'uv run pytest')
        write(home / 'shared' / 'caps.conf', 'WAIT_SECONDS=30\nPOLL_SECONDS=1\n', 0o644)
        threading.Timer(2, lambda: fcntl.flock(slot, fcntl.LOCK_UN)).start()
        waited = remote(env, '--full', worktree, 'gateway')
    assert waited.returncode == 0, waited.stderr
    assert 'waiting in the mini queue' in waited.stderr
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')


# spec: ANYM-638
def test_a_run_that_waited_for_a_slot_checks_for_between_again_before_it_starts(mini, worktree):
    home, env = mini
    write(home / 'shared' / 'caps.conf', 'WAIT_SECONDS=60\nPOLL_SECONDS=1\n', 0o644)
    between = home / 'between-containers'
    locks = home / 'service' / 'deedkit-ci' / '.locks'
    locks.mkdir(parents=True)
    with open(locks / 'slot1', 'a') as slot:
        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        process = background(env, '--full', worktree, 'gateway', FAKE_DOCKER_PS_FILE=str(between))
        wait_for(lambda: any(' --run ' in line for line in log_lines(home, 'sudo')), 60)
        # Between starts while the run waits for the slot; then the slot frees.
        between.write_text('between-ci-77-test \n')
        fcntl.flock(slot, fcntl.LOCK_UN)
        time.sleep(4)
        assert not log_lines(home, 'uv run pytest')
        between.unlink()
        _, stderr = process.communicate(timeout=60)
    assert process.returncode == 0, stderr
    assert 'a Between container is running' in stderr
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')


# spec: ANYM-638
@pytest.mark.parametrize('extra', [{'FAKE_DOCKER_PS': 'between-ci-1234-test \n'},
                                   {'FAKE_DOCKER_PS': 'app-ready-1 io.between.local-ci.run=abc\n'},
                                   {'FAKE_LOAD': '9.50'}, {'FAKE_FREE_MEM_GB': '2'}],
                         ids=['between-container', 'between-label', 'load', 'memory'])
def test_between_activity_or_low_memory_keeps_the_run_off_the_mini(mini, worktree, extra):
    home, env = mini
    result = remote(env, '--full', worktree, 'gateway', **extra)
    assert result.returncode == 90 and 'mini busy' in result.stderr and 'CI covers' in result.stderr
    assert not log_lines(home, 'sudo')


# spec: ANYM-638
@pytest.mark.parametrize('extra,code', [({'FAKE_SSH_DOWN': '1'}, 91), ({'FAKE_NO_SERVICE': '1'}, 92),
                                        ({'FAKE_SSH_FAIL': 'mkdir -p'}, 91), ({'FAKE_SSH_FAIL': 'rsync --server'}, 91)],
                         ids=['unreachable', 'no-service-user', 'mkdir-fails', 'rsync-fails'])
def test_a_mini_that_cannot_start_the_suite_leaves_it_to_ci_and_runs_nothing_locally(mini, worktree, extra, code):
    home, env = mini
    result = remote(env, '--full', worktree, 'gateway', **extra)
    assert result.returncode == code
    assert 'CI covers' in result.stderr
    assert not log_lines(home, 'uv')
    assert not (worker_dir(home) / 'src').exists()


# spec: ANYM-638
def test_an_ssh_drop_mid_run_stops_the_run_on_the_mini_and_does_not_rerun_locally(mini, worktree):
    home, env = mini
    process = background(env, worktree, 'root', 'tests/test_app.py', FAKE_UV_SLEEP='60', FAKE_SSH_DROP='1')
    wait_for(lambda: log_lines(home, 'uv run pytest') and log_lines(home, 'ssh-session'), 60)
    os.kill(pid_of(log_lines(home, 'ssh-session')[0]), signal.SIGTERM)
    _, stderr = process.communicate(timeout=60)
    assert process.returncode == 255, stderr
    assert 'failed mid-run' in stderr
    assert any(f'--here stop {WORKER}' in line for line in log_lines(home, 'sudo'))
    wait_for(lambda: log_lines(home, 'postgres stopped by QUIT'))
    assert not alive(pid_of(log_lines(home, 'uv run pytest')[0]))
    assert len(log_lines(home, 'uv run pytest')) == 1


# spec: ANYM-638
def test_cleanup_removes_the_workers_copies_and_leftover_databases_only(mini, worktree):
    home, env = mini
    assert remote(env, '--full', worktree, 'gateway').returncode == 0
    other = home / 'service' / 'deedkit-ci' / 'other_worker' / 'src'
    write(other / 'kept.py', '', 0o644)
    cluster = worker_dir(home) / 'runs' / '1-1' / 'pg1' / 'cluster'
    cluster.mkdir(parents=True)
    leftover = subprocess.Popen([str(home / 'pgbin' / 'postgres'), '-D', str(cluster)], env=env)
    wait_for(lambda: (cluster / 'postmaster.pid').exists())
    result = remote(env, '--cleanup', WORKER)
    assert result.returncode == 0, result.stderr
    assert leftover.wait(timeout=10) == 0 and log_lines(home, 'postgres stopped by QUIT')
    assert not worker_dir(home).exists()
    assert not (home / 'shared' / 'stage' / WORKER).exists()
    assert (other / 'kept.py').exists()


def make_zombie():
    """A process no signal ends (it stays until its parent, this test, reaps it) and its record line."""
    zombie = subprocess.Popen(['true'], start_new_session=True)
    wait_for(lambda: subprocess.run(['ps', '-o', 'stat=', '-p', str(zombie.pid)], capture_output=True,
                                    text=True).stdout.strip().startswith('Z'))
    stamp = subprocess.run(['bash', '-c', f'ps -o lstart= -p {zombie.pid} | tr -s " " "_"'], capture_output=True,
                           text=True, check=True).stdout.strip('\n')
    return zombie, f'group {zombie.pid} {stamp}\n'


# spec: ANYM-639
def test_cleanup_keeps_the_record_of_a_run_whose_process_does_not_stop(mini, worktree):
    home, env = mini
    assert remote(env, '--full', worktree, 'gateway').returncode == 0
    zombie, record = make_zombie()
    run_dir = worker_dir(home) / 'runs' / '1-1'
    run_dir.mkdir(parents=True)
    (run_dir / 'owned').write_text(record)
    kept = remote(env, '--cleanup', WORKER)
    assert kept.returncode == 1 and 'does not stop' in kept.stderr, kept.stderr
    assert (run_dir / 'owned').exists() and not (worker_dir(home) / 'src').exists()
    zombie.wait()
    again = remote(env, '--cleanup', WORKER)
    assert again.returncode == 0, again.stderr
    assert not worker_dir(home).exists()


# spec: ANYM-639
def test_a_run_whose_recorded_process_does_not_stop_fails_and_keeps_its_record(mini, worktree):
    home, env = mini
    process = background(env, '--full', worktree, 'gateway', FAKE_UV_SLEEP='3')
    wait_for(lambda: log_lines(home, 'uv run pytest'), 60)
    zombie, record = make_zombie()
    (run_dir,) = (worker_dir(home) / 'runs').iterdir()
    with (run_dir / 'owned').open('a') as owned:
        owned.write(record)
    _, stderr = process.communicate(timeout=60)
    # The suite passed but its stop did not finish: the run fails, its load line says so and the record stays.
    assert process.returncode == 1 and 'does not stop' in stderr, stderr
    assert load_rows(home)[-1][1] == 'gateway' and load_rows(home)[-1][-1] == '1'
    assert (run_dir / 'owned').exists()
    zombie.wait()


# spec: ANYM-639
def test_finish_ends_within_its_bound_when_the_watchdog_outlives_its_kill(tmp_path):
    # The script's functions without its dispatch, and a `kill` that drops the watchdog's KILL: a watchdog stuck
    # in the kernel. An unbounded wait on it would hold the run for the watchdog's 60 s.
    functions = SCRIPT.read_text().rsplit('\ncase "${1:-}" in', 1)[0]
    run_dir = tmp_path / 'runs' / '1-1'
    run_dir.mkdir(parents=True)
    driver = functions + f'''
kill() {{ if [ "$1" = -KILL ] && [ "$2" = "$WATCHDOG" ]; then return 0; fi; builtin kill "$@"; }}
set -m
sleep 60 </dev/null >/dev/null 2>&1 & WATCHDOG=$!
echo "$WATCHDOG" > {tmp_path}/watchdog.pid
WORKER=w SUITE=gateway START_ISO=t START_EPOCH=$SECONDS ROOT_DIR={tmp_path} WORKER_DIR={tmp_path}
RUN_DIR={run_dir} CHILDREN="" DATABASES="" HOLDER="" STOP_SECONDS=1
trap finish EXIT
exit 0
'''
    started = time.time()
    result = subprocess.run(['bash', '-c', driver], capture_output=True, text=True, timeout=30)
    watchdog = int((tmp_path / 'watchdog.pid').read_text())
    try:
        assert result.returncode == 0, result.stderr
        assert time.time() - started < 15
        assert alive(watchdog) and not run_dir.exists()
        assert (tmp_path / 'load.log').read_text().split('\t')[-1] == '0\n'
    finally:
        os.kill(watchdog, signal.SIGKILL)


# docs/operations/remote-test-mini-setup.md section 1, run as written: its blocks with the system paths moved under
# a temporary root, and fakes of ssh/scp (the mini's shell runs here), sudo, dscl, install, chown and visudo.
SETUP_DOC = ROOT / 'docs' / 'operations' / 'remote-test-mini-setup.md'
SETUP_SHELLS = ['bash'] + (['zsh'] if shutil.which('zsh') else [])  # zsh: the mini's login shell
SETUP_FAKES = {
    'ssh': '''#!/usr/bin/env bash
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -*) shift ;; *) break ;; esac; done
shift
cd "$FAKE_OWNER_HOME" || exit 255
HOME="$FAKE_OWNER_HOME" exec "$FAKE_SHELL" -c "$*"
''',
    'scp': '''#!/usr/bin/env bash
dest="${@: -1}"; dest="${dest#*:}"
case "$dest" in /*) ;; *) dest="$FAKE_OWNER_HOME/$dest" ;; esac
cp "${@:1:$#-1}" "$dest"
''',
    # FAKE_FAIL: the sudo command line that fails, as a denied or broken step would.
    'sudo': '''#!/usr/bin/env bash
while [ $# -gt 0 ]; do case "$1" in -n) shift ;; -u) shift 2 ;; *) break ;; esac; done
if [ -n "${FAKE_FAIL:-}" ]; then case "$*" in *"$FAKE_FAIL"*) echo "sudo: synthetic failure: $*" >&2; exit 1 ;; esac; fi
exec "$@"
''',
    'dscl': '''#!/usr/bin/env bash
op="$2" rec="${3#"$FAKE_ROOT"}"; shift 3
f="$FAKE_DS$rec"
case "$op" in
  -read) [ -f "$f" ] && cat "$f" ;;
  -create) mkdir -p "${f%/*}"; touch "$f"; if [ $# -gt 0 ]; then echo "$1: ${2:-}" >> "$f"; fi ;;
  -delete) rm -f "$f" ;;
  -list) for g in "$f"/*; do [ -f "$g" ] || continue; v="$(sed -n "s/^$1: //p" "$g")"; [ -z "$v" ] || echo "${g##*/} $v"; done ;;
esac
''',
    'install': '''#!/usr/bin/env bash
mode=644
while [ $# -gt 2 ]; do case "$1" in -m) mode="$2"; shift 2 ;; -o|-g) shift 2 ;; *) break ;; esac; done
cp "$1" "$2" && chmod "$mode" "$2"
''',
    'chown': '#!/usr/bin/env bash\nexit 0\n',
    # FAKE_VISUDO_OUT: what a full `visudo -c` also reports about other sudoers files (it then exits 1).
    # FAKE_VISUDO_SILENT: a full `visudo -c` fails without a word; FAKE_VISUDO_EXIT: it fails after the first
    # file parsed (killed, say).
    'visudo': '''#!/usr/bin/env bash
if [ "$*" = -c ]; then
  [ "${FAKE_VISUDO_SILENT:-0}" = 0 ] || exit 1
  echo "/private/etc/sudoers: parsed OK"
  if [ -n "${FAKE_VISUDO_OUT:-}" ]; then printf '%s\\n' "$FAKE_VISUDO_OUT" >&2; exit 1; fi
  exit "${FAKE_VISUDO_EXIT:-0}"
fi
echo "${@: -1}: parsed OK"
''',
}


@pytest.fixture
def mini_setup(tmp_path):
    root = tmp_path / 'mini'
    for name in ('owner', 'ds', 'Users', 'tmp', 'sudoers.d'):
        (root / name).mkdir(parents=True)
    for name, text in SETUP_FAKES.items():
        write(tmp_path / 'setupbin' / name, text)
    section = SETUP_DOC.read_text().split('\n## 1.')[1].split('\n## ')[0]
    blocks = [part.split('\n', 1)[1].split('```')[0] for part in section.split('```bash')[1:]]
    for system, moved in (('/tmp/deedkit-setup', f'{root}/tmp/deedkit-setup'), ('/Users/$U', f'{root}/Users/$U'),
                          ('/usr/local/libexec', f'{root}/libexec'), ('/etc/sudoers.d', f'{root}/sudoers.d')):
        blocks = [block.replace(system, moved) for block in blocks]
    # preflight, steps 1-4, read-back (not run here), undo
    assert len(blocks) == 7 and 'preflight-ok' in blocks[0] and 'undo-ok' in blocks[6]
    env = dict(os.environ, PATH=f"{tmp_path / 'setupbin'}:{os.environ['PATH']}", FAKE_OWNER_HOME=str(root / 'owner'),
               FAKE_DS=str(root / 'ds'), FAKE_ROOT=str(root))
    return root, env, blocks


def setup_block(env, block, shell, **extra):
    return subprocess.run(['bash', '-c', block], cwd=ROOT, env=dict(env, FAKE_SHELL=shell, **extra),
                          capture_output=True, text=True, timeout=60)


def setup_leftovers(root):
    paths = [root / 'Users' / '_deedkitci', root / 'libexec' / 'deedkit-remote-test',
             root / 'sudoers.d' / 'deedkit-remote-test', root / 'ds' / 'Users' / '_deedkitci',
             root / 'ds' / 'Groups' / '_deedkitci', root / 'owner' / 'deedkit-ci-setup.created',
             root / 'owner' / 'deedkit-ci-setup.left']
    return [path for path in paths if path.exists()] + list((root / 'tmp').iterdir())


# spec: ANYM-638
@pytest.mark.parametrize('shell', SETUP_SHELLS)
def test_the_setup_creates_and_its_undo_removes_exactly_the_ledgered_resources(mini_setup, shell):
    root, env, blocks = mini_setup
    write(root / 'owner' / 'deedkit-remote-test', 'an owner file\n', 0o600)
    for block in blocks[:5]:
        result = setup_block(env, block, shell)
        assert result.returncode == 0, result.stdout + result.stderr
    ledger = (root / 'owner' / 'deedkit-ci-setup.created').read_text().splitlines()
    assert ledger[:3] == ['group', 'user', 'home'] and ledger[4:] == ['shim', 'sudoers']
    assert ledger[3].startswith(f'stage {root}/tmp/deedkit-setup.')
    assert 'UniqueID: 453' in (root / 'ds' / 'Users' / '_deedkitci').read_text()
    assert (root / 'libexec' / 'deedkit-remote-test').read_text() == SHIM.read_text()
    # The files travelled through a private directory, now gone; the owner's own file is untouched.
    assert (root / 'owner' / 'deedkit-remote-test').read_text() == 'an owner file\n'
    assert not list((root / 'tmp').iterdir())
    undo = setup_block(env, blocks[6], shell)
    assert undo.returncode == 0 and 'undo-ok' in undo.stdout, undo.stdout + undo.stderr
    assert setup_leftovers(root) == []


# spec: ANYM-638
@pytest.mark.parametrize('fail', ['PrimaryGroupID 453', 'UniqueID 453', 'chown', 'sudoers.d'],
                         ids=['group', 'user', 'home', 'sudoers'])
def test_a_setup_that_fails_midway_is_undone_completely(mini_setup, fail):
    root, env, blocks = mini_setup
    for block in blocks[:5]:
        if setup_block(env, block, SETUP_SHELLS[-1], FAKE_FAIL=fail).returncode:
            break
    else:
        pytest.fail(f'no step failed on {fail}')
    undo = setup_block(env, blocks[6], SETUP_SHELLS[-1])
    assert undo.returncode == 0 and 'undo-ok' in undo.stdout, undo.stdout + undo.stderr
    assert setup_leftovers(root) == []


# spec: ANYM-639
@pytest.mark.parametrize('shell', SETUP_SHELLS)
@pytest.mark.parametrize('visudo,ok', [
    ({'FAKE_VISUDO_OUT': '/private/etc/sudoers.d/ego: bad permissions, should be mode 0440'}, True),
    ({'FAKE_VISUDO_OUT': '>>> /etc/sudoers.d/other: syntax error near line 2 <<<'}, False),
    ({'FAKE_VISUDO_SILENT': '1'}, False), ({'FAKE_VISUDO_EXIT': '137'}, False)],
    ids=['permission-warning', 'syntax-error', 'silent-failure', 'killed'])
def test_the_setup_accepts_the_owners_sudoers_permission_warnings_and_no_other_failure(mini_setup, visudo, ok, shell):
    root, env, blocks = mini_setup
    results = [setup_block(env, block, shell, **visudo) for block in blocks[:5]]
    assert [result.returncode for result in results[:4]] == [0, 0, 0, 0]
    assert (results[4].returncode == 0) == ok, results[4].stdout + results[4].stderr
    undo = setup_block(env, blocks[6], shell, **visudo)
    if ok:
        assert undo.returncode == 0 and 'undo-ok' in undo.stdout, undo.stdout + undo.stderr
        assert setup_leftovers(root) == []
    else:
        assert undo.returncode == 1 and 'ledger kept' in undo.stdout, undo.stdout + undo.stderr
        assert (root / 'owner' / 'deedkit-ci-setup.created').exists()


# spec: ANYM-638
def test_an_undo_that_cannot_remove_a_resource_keeps_it_in_the_ledger_and_fails(mini_setup):
    root, env, blocks = mini_setup
    shell = SETUP_SHELLS[-1]
    for block in blocks[:5]:
        assert setup_block(env, block, shell).returncode == 0
    failed = setup_block(env, blocks[6], shell, FAKE_FAIL=f'-delete {root}/Users')
    assert failed.returncode == 1 and 'still present' in failed.stdout, failed.stdout + failed.stderr
    assert (root / 'owner' / 'deedkit-ci-setup.created').read_text() == 'user\n'
    assert (root / 'ds' / 'Users' / '_deedkitci').exists() and not (root / 'ds' / 'Groups' / '_deedkitci').exists()
    again = setup_block(env, blocks[6], shell)
    assert again.returncode == 0 and 'undo-ok' in again.stdout, again.stdout + again.stderr
    assert setup_leftovers(root) == []


# spec: ANYM-638
@pytest.mark.parametrize('name', ['../owner', 'A', '', 'a/b', '_x'])
def test_a_worker_name_that_could_leave_its_directory_is_refused(mini, name):
    home, env = mini
    result = remote(env, '--cleanup', name)
    assert result.returncode == 2
    assert not log_lines(home, 'ssh')
