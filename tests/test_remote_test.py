"""scripts/remote-test.sh: DeedKit suites on the Mac mini as _deedkitci (owner D41/D42).

The mini is this machine: fakes of ssh, sudo, docker, uv, go, npm, node, df, sysctl and the PostgreSQL
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
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'remote-test.sh'
SHIM = ROOT / 'deploy' / 'mac-mini' / 'deedkit-remote-test'
WORKER = 'infra_remote_test'

pytestmark = pytest.mark.skipif(not shutil.which('rsync') or not Path('/bin/bash').exists(),
                                reason='remote-test.sh needs /bin/bash and rsync')

BURN = 'python3 -c "import time; t = time.process_time()\nwhile time.process_time() - t < 0.3: pass"'
FAKES = {
    # Every ssh runs its command here, in the owner's home, with the caller's stdin.
    'ssh': '''#!/usr/bin/env bash
echo "ssh $*" >> "$FAKE_LOG"
[ "${FAKE_SSH_DOWN:-0}" = 1 ] && exit 255
case "$*" in *"${FAKE_SSH_FAIL:-no such command}"*) exit 255 ;; esac
while [ $# -gt 0 ]; do case "$1" in -o) shift 2 ;; -[ntq]) shift ;; *) break ;; esac; done
shift
cd "$FAKE_OWNER_HOME" && HOME="$FAKE_OWNER_HOME" exec bash -c "$*"
''',
    'sudo': '''#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
[ "${FAKE_NO_SERVICE:-0}" = 1 ] && { echo "sudo: unknown user _deedkitci" >&2; exit 1; }
while [ $# -gt 0 ]; do case "$1" in -n) shift ;; -u) shift 2 ;; *) break ;; esac; done
exec "$@"
''',
    'docker': '''#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$1" in
  ps) printf '%s' "${FAKE_DOCKER_PS:-}" ;;
  run) while [ $# -gt 0 ]; do [ "$1" = --name ] && echo "cid-$2"; shift; done ;;
  port) echo 127.0.0.1:55432 ;;
esac
exit 0
''',
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
}
PG_FAKES = {
    'initdb': '''#!/usr/bin/env bash
echo "initdb $*" >> "$FAKE_LOG"
while [ $# -gt 0 ]; do [ "$1" = -D ] && mkdir -p "$2"; shift; done
''',
    'postgres': f'''#!/usr/bin/env bash
echo "postgres $*" >> "$FAKE_LOG"
echo $$ > "$2/postmaster.pid"
trap '{BURN}; echo "postgres stopped by QUIT" >> "$FAKE_LOG"; exit 0' QUIT
while :; do sleep 0.1; done
''',
    'pg_isready': '#!/usr/bin/env bash\nexit 0\n',
    'createdb': '#!/usr/bin/env bash\necho "createdb $*" >> "$FAKE_LOG"\n',
}


def write(path, text, mode=0o755):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


@pytest.fixture
def mini(tmp_path):
    for name, text in FAKES.items():
        write(tmp_path / 'bin' / name, text)
    for name, text in PG_FAKES.items():
        write(tmp_path / 'pgbin' / name, text)
    for name in ('owner', 'service', 'shared/stage', 'local'):
        (tmp_path / name).mkdir(parents=True)
    # The shipped shim, with this machine's user and a temporary home in place of _deedkitci's.
    shim = SHIM.read_text().replace('/Users/_deedkitci', str(tmp_path / 'service'))
    write(tmp_path / 'shim', shim.replace('_deedkitci', getpass.getuser()))
    log = tmp_path / 'log'
    log.touch()
    env = dict(os.environ, PATH=f"{tmp_path / 'bin'}:{os.environ['PATH']}", FAKE_LOG=str(log),
               FAKE_OWNER_HOME=str(tmp_path / 'owner'), DEEDKIT_REMOTE_HOST='mini',
               DEEDKIT_REMOTE_SHIM=str(tmp_path / 'shim'), DEEDKIT_REMOTE_SHARED=str(tmp_path / 'shared'),
               DEEDKIT_REMOTE_STAGE=str(tmp_path / 'stage'), DEEDKIT_CI_LOCAL_ROOT=str(tmp_path / 'local'),
               DEEDKIT_RUNNER_PATH_DIRS=str(tmp_path / 'bin'), DEEDKIT_PG_BIN=str(tmp_path / 'pgbin'),
               DEEDKIT_REMOTE_WORKER=WORKER)
    for name in ('DEEDKIT_CI_ROOT', 'PROBE_TEST_DSN_FILE', 'FCS_TEST_DSN', 'DEEDKIT_TEST_SHARD', 'LOCAL_RUN'):
        env.pop(name, None)
    return tmp_path, env


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
    git = ['git', '-C', str(tree), '-c', 'user.name=t', '-c', 'user.email=t@invalid', '-c', 'commit.gpgsign=false']
    subprocess.run(['git', 'init', '-q', str(tree)], check=True)
    subprocess.run([*git, 'add', '-A'], check=True)
    subprocess.run([*git, 'commit', '-qm', 'synthetic'], check=True)
    (tree / 'gone.py').unlink()
    # Untracked, not ignored: the working diff, and secrets a worker might leave lying around.
    untracked = {'new_module.py': 'x = 1\n', '.env': 'K=synthetic\n', 'local.env': 'K=1\n', 'id_ed25519': 'k\n',
                 '.tiden/session.json': '{}\n', '.venv/lib/site.py': '', 'node_modules/a/index.js': '',
                 'creds/credentials': 'k\n', 'tls.key': 'k\n', '.netrc': 'k\n', '.npmrc': 'k\n',
                 'test.dsn': 'postgresql://x\n', 'telegram.session': 'k\n', 'ignored.log': 'x\n',
                 'whatsapp/session.sqlite': 'k\n', 'secrets/token.txt': 'k\n',
                 'credentials/google.json': '{}\n', '.AWS/sso/cache/entry.json': '{}\n',
                 'whatsapp/session.sqlite-wal': 'k\n', 'data/store.db-journal': 'k\n', 'Secrets/notes.txt': 'k\n',
                 'TOKENS/github': 'k\n', 'state/.ENV': 'K=1\n'}
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


def files_under(root):
    return {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and '.git' not in p.parts}


def log_lines(home, prefix):
    return [line for line in (home / 'log').read_text().splitlines() if line.startswith(prefix)]


def run_dir(home):
    return home / 'service' / 'deedkit-ci' / WORKER


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


def suite_pid(home):
    return int(log_lines(home, 'uv run pytest')[0].split('pid=')[1])


def test_the_list_holds_tracked_and_untracked_files_but_no_secret_environment_or_deleted_file(mini, worktree):
    _, env = mini
    result = remote(env, '--list', worktree)
    assert result.returncode == 0, result.stderr
    assert set(result.stdout.split()) == SENT


def test_a_run_sends_exactly_the_listed_files_and_runs_the_suite_through_the_shim(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'gateway')
    assert result.returncode == 0, result.stderr
    assert 'FAKE SUITE OUTPUT' in result.stdout
    assert files_under(home / 'stage' / 'src') == SENT
    assert files_under(home / 'shared' / 'stage' / WORKER) == SENT
    copy = run_dir(home) / 'src'
    assert files_under(copy) == SENT
    # The copy is a one-commit repository of itself, for the tests that read `git ls-files`.
    tracked = subprocess.run(['git', '-C', str(copy), 'ls-files'], capture_output=True, text=True, check=True)
    assert set(tracked.stdout.split()) == SENT
    assert any(f'-u _deedkitci {home / "shim"} --run {WORKER} gateway' in line for line in log_lines(home, 'sudo'))
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')


def test_a_second_run_removes_what_the_worktree_no_longer_has_and_keeps_the_environment(mini, worktree):
    home, env = mini
    assert remote(env, worktree, 'gateway').returncode == 0
    copy = run_dir(home) / 'src'
    write(copy / '.venv' / 'kept.py', '', 0o644)
    (worktree / 'new_module.py').unlink()
    assert remote(env, worktree, 'gateway').returncode == 0
    assert files_under(copy) == SENT - {'new_module.py'} | {'.venv/kept.py'}


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
    assert (run_dir(home) / 'test-1.dsn').stat().st_mode & 0o777 == 0o600
    # Stopped at exit (immediate shutdown) and its cluster removed.
    assert log_lines(home, 'postgres stopped by QUIT')
    assert not list(run_dir(home).glob('pg*'))
    assert 'skipped: needs docker (CI covers)' in result.stderr


def test_the_full_root_suite_runs_parallel_shards_each_with_its_own_database(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root')
    assert result.returncode == 0, result.stderr
    lines = log_lines(home, 'uv run python scripts/python_shards.py')
    assert sorted(line.split('shard=')[1].split()[0] for line in lines) == ['1/2', '2/2']
    assert len({line.split('dsn=')[1].split()[0] for line in lines}) == 2
    assert len(log_lines(home, 'postgres -D')) == 2
    assert '[1/2] FAKE SUITE OUTPUT 1/2' in result.stdout and '[2/2] FAKE SUITE OUTPUT 2/2' in result.stdout


def test_caps_on_the_mini_drop_the_root_suite_to_one_process_without_a_code_change(mini, worktree):
    home, env = mini
    write(home / 'shared' / 'caps.conf', 'PARALLEL=1\nMAX_RUNS=1\n', 0o644)
    assert remote(env, worktree, 'root').returncode == 0
    assert not log_lines(home, 'uv run python scripts/python_shards.py')
    assert len(log_lines(home, 'uv run pytest --tb=short')) == 1


def test_the_go_frontend_and_node_suites_run_the_ci_commands(mini, worktree):
    home, env = mini
    for suite in ('go', 'frontend', 'node'):
        result = remote(env, worktree, suite)
        assert result.returncode == 0, result.stderr
    assert log_lines(home, 'go vet ./... | cwd=whatsmeow gomaxprocs=2')
    assert log_lines(home, 'go test -race -p 2 ./... | cwd=whatsmeow gomaxprocs=2')
    assert [line.split(' |')[0] for line in log_lines(home, 'npm')] == [
        'npm ci --ignore-scripts --no-audit --no-fund', 'npm test', 'npm run build', 'npm test --prefix packages/npm']
    assert log_lines(home, 'node --test whatsapp/tests/a.test.mjs')


def test_the_build_suite_brings_dist_back_to_the_worktree(mini, worktree):
    _, env = mini
    result = remote(env, worktree, 'build')
    assert result.returncode == 0, result.stderr
    assert (worktree / 'dist' / 'anymsg-0.0.0.whl').read_text() == 'synthetic'


def test_the_disk_guard_refuses_under_15_gigabytes_before_anything_runs(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'root', FAKE_FREE_GB='14', DEEDKIT_REMOTE_FALLBACK='0')
    assert result.returncode == 90
    assert 'disk guard: 14 G free, need 15 G' in result.stderr
    assert not log_lines(home, 'postgres') and not log_lines(home, 'uv')
    assert not (home / 'shared' / 'stage' / WORKER).exists()


def test_a_failing_suite_stops_postgres_and_returns_the_suite_status(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'calendar', FAKE_UV_EXIT='3')
    assert result.returncode == 3
    assert log_lines(home, 'postgres stopped by QUIT')
    assert 'fcs=postgresql://probe_owner:' in log_lines(home, 'uv run pytest -m not live')[0]


def test_a_terminated_run_stops_its_children_and_postgres(mini, worktree):
    home, env = mini
    process = subprocess.Popen(['bash', str(SCRIPT), str(worktree), 'root', 'tests/test_app.py'],
                               env=dict(env, FAKE_UV_SLEEP='60'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    wait_for(lambda: log_lines(home, 'uv run pytest'), 60)
    os.kill(int((run_dir(home) / 'run.pid').read_text()), signal.SIGTERM)
    _, stderr = process.communicate(timeout=30)
    assert process.returncode == 143, stderr
    assert log_lines(home, 'postgres stopped by QUIT')
    assert not alive(suite_pid(home))
    assert not (run_dir(home) / 'run.pid').exists()
    assert load_rows(home)[-1][1] == 'root' and load_rows(home)[-1][-1] == '143'


def test_a_killed_runner_leaves_no_database_child_or_held_slot_behind(mini, worktree):
    home, env = mini
    process = subprocess.Popen(['bash', str(SCRIPT), str(worktree), 'root', 'tests/test_app.py'],
                               env=dict(env, FAKE_UV_SLEEP='60'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    wait_for(lambda: log_lines(home, 'uv run pytest'), 60)
    os.kill(int((run_dir(home) / 'run.pid').read_text()), signal.SIGKILL)
    process.communicate(timeout=30)
    # The watchdog stops the orphaned suite and PostgreSQL; the slot lock died with the runner.
    wait_for(lambda: log_lines(home, 'postgres stopped by QUIT'))
    wait_for(lambda: not alive(suite_pid(home)))
    wait_for(lambda: not list(run_dir(home).glob('pg*')))
    again = remote(env, worktree, 'gateway', DEEDKIT_REMOTE_FALLBACK='0')
    assert again.returncode == 0, again.stderr


def test_each_mini_run_appends_one_load_line_with_measured_cpu(mini, worktree):
    home, env = mini
    assert remote(env, worktree, 'root', 'tests/test_app.py').returncode == 0
    assert remote(env, worktree, 'gateway').returncode == 0
    rows = load_rows(home)
    assert [(row[0], row[1], row[-1]) for row in rows] == [(WORKER, 'root', '0'), (WORKER, 'gateway', '0')]
    (_, _, start, wall, cpu, pg, _), (_, _, _, _, gateway_cpu, gateway_pg, _) = rows
    assert start.endswith('Z') and int(wall) >= 0
    # The suite and the database each burn 0.3 s of CPU in their fakes.
    assert float(pg) >= 0.2 and float(cpu) >= float(pg) + 0.2
    assert float(gateway_cpu) >= 0.2 and gateway_pg == '0'
    assert remote(env, '--load').stdout.count(WORKER) == 2


def test_a_taken_slot_sends_the_run_to_the_laptop(mini, worktree):
    home, env = mini
    locks = home / 'service' / 'deedkit-ci' / '.locks'
    locks.mkdir(parents=True)
    with open(locks / 'slot1', 'a') as slot:
        fcntl.flock(slot, fcntl.LOCK_EX | fcntl.LOCK_NB)
        refused = remote(env, worktree, 'gateway', DEEDKIT_REMOTE_FALLBACK='0')
        assert refused.returncode == 90 and 'all 1 DeedKit slot(s) taken' in refused.stderr
        fallback = remote(env, worktree, 'gateway')
    assert fallback.returncode == 0, fallback.stderr
    assert 'running gateway on this machine' in fallback.stderr
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')
    assert not (home / 'shared' / 'stage' / WORKER).exists()


def test_local_fallback_databases_are_named_per_worker_and_only_its_own_are_stopped(mini, worktree):
    home, env = mini
    for worker in ('worker_a', 'worker_b'):
        result = remote(env, worktree, 'root', 'tests/test_app.py', FAKE_NO_SERVICE='1', DEEDKIT_REMOTE_WORKER=worker)
        assert result.returncode == 0, result.stderr
    runs = log_lines(home, 'docker run')
    assert [line.split('--name ')[1].split()[0] for line in runs] == ['deedkit_worker_a_pg1', 'deedkit_worker_b_pg1']
    assert all('-p 127.0.0.1::5432' in line and '--rm' in line for line in runs)
    stops = [line.split()[-1] for line in log_lines(home, 'docker stop')]
    assert stops == ['cid-deedkit_worker_a_pg1', 'cid-deedkit_worker_b_pg1']


@pytest.mark.parametrize('extra', [{'FAKE_DOCKER_PS': 'between-ci-1234-test \n'},
                                   {'FAKE_DOCKER_PS': 'app-ready-1 io.between.local-ci.run=abc\n'},
                                   {'FAKE_LOAD': '9.50'}], ids=['between-container', 'between-label', 'load'])
def test_between_activity_refuses_the_mini(mini, worktree, extra):
    home, env = mini
    result = remote(env, worktree, 'gateway', DEEDKIT_REMOTE_FALLBACK='0', **extra)
    assert result.returncode == 90 and 'mini busy' in result.stderr
    assert not log_lines(home, 'sudo')


@pytest.mark.parametrize('extra,code', [({'FAKE_SSH_DOWN': '1'}, 91), ({'FAKE_NO_SERVICE': '1'}, 92),
                                        ({'FAKE_SSH_FAIL': 'mkdir -p'}, 91)],
                         ids=['unreachable', 'no-service-user', 'copy-fails'])
def test_a_mini_that_cannot_start_the_suite_falls_back_to_the_local_flow(mini, worktree, extra, code):
    home, env = mini
    assert remote(env, worktree, 'gateway', DEEDKIT_REMOTE_FALLBACK='0', **extra).returncode == code
    result = remote(env, worktree, 'gateway', **extra)
    assert result.returncode == 0, result.stderr
    assert 'running gateway on this machine' in result.stderr
    assert log_lines(home, 'uv run pytest -q | cwd=gateway')
    assert not (run_dir(home) / 'src').exists()


def test_an_ssh_drop_mid_run_stops_the_run_on_the_mini_and_does_not_rerun_locally(mini, worktree):
    home, env = mini
    result = remote(env, worktree, 'gateway', FAKE_SSH_FAIL=f'--run {WORKER}')
    assert result.returncode == 255
    assert 'failed mid-run' in result.stderr and 'on this machine' not in result.stderr
    assert any(f'--here stop {WORKER}' in line for line in log_lines(home, 'sudo'))


def test_cleanup_removes_the_workers_copies_and_leftover_databases_only(mini, worktree):
    home, env = mini
    assert remote(env, worktree, 'gateway').returncode == 0
    other = home / 'service' / 'deedkit-ci' / 'other_worker' / 'src'
    write(other / 'kept.py', '', 0o644)
    cluster = run_dir(home) / 'pg1' / 'cluster'
    cluster.mkdir(parents=True)
    leftover = subprocess.Popen([str(home / 'pgbin' / 'postgres'), '-D', str(cluster)], env=env)
    wait_for(lambda: (cluster / 'postmaster.pid').exists())
    result = remote(env, '--cleanup', WORKER)
    assert result.returncode == 0, result.stderr
    assert leftover.wait(timeout=10) == 0 and log_lines(home, 'postgres stopped by QUIT')
    assert not run_dir(home).exists()
    assert not (home / 'shared' / 'stage' / WORKER).exists()
    assert (other / 'kept.py').exists()


@pytest.mark.parametrize('name', ['../owner', 'A', '', 'a/b', '_x'])
def test_a_worker_name_that_could_leave_its_directory_is_refused(mini, name):
    home, env = mini
    result = remote(env, '--cleanup', name)
    assert result.returncode == 2
    assert not log_lines(home, 'ssh')
