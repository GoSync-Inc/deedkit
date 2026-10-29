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
    'docker': '#!/usr/bin/env bash\necho "docker $*" >> "$FAKE_LOG"\n[ "$1" = ps ] && printf "%s" "${FAKE_DOCKER_PS:-}"\nexit 0\n',
    'uv': f'''#!/usr/bin/env bash
dsn=""; [ -n "${{PROBE_TEST_DSN_FILE:-}}" ] && dsn="$(cat "$PROBE_TEST_DSN_FILE")"
echo "uv $* | cwd=${{PWD##*/}} dsn=$dsn shard=${{DEEDKIT_TEST_SHARD:-}} fcs=${{FCS_TEST_DSN:-}} pid=$$" >> "$FAKE_LOG"
[ "$1" = run ] || exit 0
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
}
PG_FAKES = {
    'initdb': '''#!/usr/bin/env bash
echo "initdb $* pid=$$" >> "$FAKE_LOG"
[ "${FAKE_INITDB_SLEEP:-0}" = 0 ] || sleep "$FAKE_INITDB_SLEEP"
while [ $# -gt 0 ]; do [ "$1" = -D ] && mkdir -p "$2"; shift; done
''',
    'postgres': f'''#!/usr/bin/env bash
echo "postgres $* pid=$$" >> "$FAKE_LOG"
echo $$ > "$2/postmaster.pid"
trap '{BURN}; echo "postgres stopped by QUIT pid=$$" >> "$FAKE_LOG"; exit 0' QUIT
while :; do sleep 0.1; done
''',
    'pg_isready': '#!/usr/bin/env bash\nexit 0\n',
    'createdb': '#!/usr/bin/env bash\necho "createdb $*" >> "$FAKE_LOG"\n',
}
CAPS = 'WAIT_SECONDS=0\nPOLL_SECONDS=1\n'


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
                 'credential.py/credential.py': 'k\n'}
    for name, text in untracked.items():
        write(tree / name, text, 0o644)
    return tree


SENT = {'app.py', 'tests/test_app.py', 'services/gateway/tests/test_credential_expiry.py',
        'services/calendar/pyproject.toml', 'services/calls/pyproject.toml', 'services/calls/frontend/package.json',
        'whatsapp/tests/a.test.mjs', 'whatsapp/whatsmeow/go.mod', 'packages/npm/package.json',
        'scripts/python_shards.py', 'scripts/build.py', '.gitignore', 'new_module.py'}


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
    port = postgres.split(' -p ')[1].split()[0]
    [pytest_line] = log_lines(home, 'uv run pytest tests/test_app.py')
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
@pytest.mark.parametrize('stage', ['copy', 'initdb', 'suite'])
def test_a_killed_runner_leaves_no_process_database_or_held_slot_behind(mini, worktree, stage):
    home, env = mini
    slow, marker = {'copy': ('FAKE_RSYNC_SLEEP', 'runner-rsync'), 'initdb': ('FAKE_INITDB_SLEEP', 'initdb'),
                    'suite': ('FAKE_UV_SLEEP', 'uv run pytest')}[stage]
    process = background(env, worktree, 'root', 'tests/test_app.py', **{slow: '60'})
    wait_for(lambda: log_lines(home, marker), 60)
    os.kill(runner_pid(home), signal.SIGKILL)
    process.communicate(timeout=30)
    # The watchdog stops the orphan and any PostgreSQL; the slot died with the runner.
    wait_for(lambda: not alive(pid_of(log_lines(home, marker)[0])))
    wait_for(lambda: not list((worker_dir(home) / 'runs').iterdir()))
    for line in log_lines(home, 'postgres -D'):
        assert not alive(pid_of(line))
    write(home / 'shared' / 'caps.conf', 'WAIT_SECONDS=10\nPOLL_SECONDS=1\n', 0o644)
    again = remote(env, '--full', worktree, 'gateway')
    assert again.returncode == 0, again.stderr


# spec: ANYM-638
def test_a_run_started_right_after_a_killed_one_is_not_touched_by_the_old_watchdog(mini, worktree):
    home, env = mini
    write(home / 'shared' / 'caps.conf', 'WAIT_SECONDS=10\nPOLL_SECONDS=1\n', 0o644)
    process = background(env, worktree, 'root', 'tests/test_app.py', FAKE_UV_SLEEP='60')
    wait_for(lambda: log_lines(home, 'uv run pytest'), 60)
    os.kill(runner_pid(home), signal.SIGKILL)
    process.communicate(timeout=30)
    # At once: the old watchdog wakes while this run's suite and database are up, and must leave them be.
    again = remote(env, worktree, 'root', 'tests/test_app.py', FAKE_UV_SLEEP='4')
    assert again.returncode == 0, again.stderr
    assert load_rows(home)[-1][-1] == '0'


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
        assert refused.returncode == 90 and 'all 1 DeedKit slot(s) taken), CI covers' in refused.stderr
        assert not log_lines(home, 'uv run pytest') and not (home / 'shared' / 'stage' / WORKER).exists()
        write(home / 'shared' / 'caps.conf', 'WAIT_SECONDS=30\nPOLL_SECONDS=1\n', 0o644)
        threading.Timer(2, lambda: fcntl.flock(slot, fcntl.LOCK_UN)).start()
        waited = remote(env, '--full', worktree, 'gateway')
    assert waited.returncode == 0, waited.stderr
    assert 'waiting in the mini queue' in waited.stderr
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


# spec: ANYM-638
@pytest.mark.parametrize('name', ['../owner', 'A', '', 'a/b', '_x'])
def test_a_worker_name_that_could_leave_its_directory_is_refused(mini, name):
    home, env = mini
    result = remote(env, '--cleanup', name)
    assert result.returncode == 2
    assert not log_lines(home, 'ssh')
