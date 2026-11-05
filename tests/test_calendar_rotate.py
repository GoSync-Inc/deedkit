"""scripts/calendar_rotate.py (ANYM-784): a synthetic host and a fake docker whose database checks scram for real.

The tool runs as a real process, so a kill at a boundary is a real SIGKILL: no `finally` runs, the kernel drops the
locks. The fake's database keeps only verifiers; a login is checked against them with the SCRAM computation below.
"""
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time

import pytest

from scripts import calendar_rotate as rotate
from scripts import production as p
from test_production import Deployer

ROOT = Path(__file__).resolve().parents[1]
OLD = {'fcs_owner': 'old-owner-canary-Zq7', 'fcs_gateway': 'old-gateway-canary-Zq7', 'fcs_worker': 'old-worker-canary-Zq7'}
PROFILE = {'dsn': None, 'port': 13232, 'bind': '0.0.0.0', 'allowed_hosts': ['calendar-api:13232', 'localhost:13232'],
           'allowed_origins': ['https://calendar.example.invalid']}
DB, API, WORKER = 'a' * 64, 'b' * 64, 'c' * 64
IMAGE = 'sha256:' + 'd' * 64
KEYS_KEPT = ('port', 'bind', 'allowed_hosts', 'allowed_origins')

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""The docker the tool sees. State and call log live in one JSON file beside it."""
import base64, fcntl, hashlib, hmac, json, os, re, signal, sys, time, urllib.parse
HOME = os.path.dirname(os.path.abspath(__file__))
FENCE = r"rolname = '(\w+)'\) <> '([^']*)'"
PATTERN = r'''+"\"\"\"ALTER ROLE \"(\\w+)\" PASSWORD '([^']+)';\"\"\""+r'''
STATE = os.path.join(HOME, 'state.json')
def locked(change):
    with open(STATE + '.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with open(STATE) as file:
            state = json.load(file)
        result = change(state)
        with open(STATE + '.tmp', 'w') as file:
            json.dump(state, file)
        os.replace(STATE + '.tmp', STATE)
        return result
def login(verifier, password):
    match = re.fullmatch(r'SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)', verifier)
    salted = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(match[2]), int(match[1]))
    client = hmac.new(salted, b'Client Key', hashlib.sha256).digest()
    return base64.b64encode(hashlib.sha256(client).digest()).decode() == match[3]
argv = sys.argv[1:]
stdin = sys.stdin.read() if not sys.stdin.isatty() and (argv[:1] == ['run'] and '-i' in argv or argv[:2] == ['exec', '-i']) else ''
locked(lambda state: state['log'].append({'argv': argv, 'stdin': stdin}))
state = locked(lambda state: dict(state))
def matches(ident, info):
    for wanted in (argv[i + 1] for i, a in enumerate(argv) if a == '--filter'):
        kind, _, value = wanted.partition('=')
        if kind == 'network':
            if value not in info['networks']:
                return False
        else:
            key, _, expected = value.partition('=')
            if info['labels'].get(key) != expected:
                return False
    return True
def containers():
    return state['containers']
command = argv[0]
if command == 'ps':
    every = '-a' in argv or '-aq' in argv
    for ident, info in containers().items():
        if matches(ident, info) and (every or info['running']):
            print(ident)
elif command == 'inspect':
    info = containers().get(argv[1])
    if info is None:
        sys.exit(1)
    env = [] if not info.get('calls') else ['VOICE_DATA_DIR=/data']
    print(json.dumps([{'Id': argv[1], 'Image': info['image'], 'State': {'Running': info['running']},
                       'Config': {'Labels': info['labels'], 'Env': env, 'User': ''},
                       'Mounts': [{'Destination': '/data', 'Type': 'volume', 'Name': 'calls'}],
                       'NetworkSettings': {'Networks': {name: {'IPAddress': '10.9.0.2'} for name in info['networks']}}}]))
elif command == 'network':
    print(json.dumps([{'Internal': state['networks'].get(argv[2], False)}]))
elif command == 'exec' and argv[1] == '-i' and 'ALTER' not in stdin and 'pg_authid' in stdin:
    for role, stored in ((role, stored) for role, stored in state['roles'].items() if role in stdin):
        print(f"{role}|{'changed-meanwhile' if state.get('fence_race') else stored}")
elif command == 'exec' and argv[1] == '-i':
    changes = re.findall(PATTERN, stdin)
    def commit(state):
        # The transaction's own fence: every role still has the verifier the ALTER expects.
        if any(state['roles'].get(role) != expected for role, expected in re.findall(FENCE, stdin)):
            return False
        state['roles'].update(changes)
        return True
    if 'ALTER' in stdin and state.get('alter') in ('late', 'orphan'):
        # The exec starts when the gate goes, long after its client ended (`late`) or the tool died (`orphan`).
        if state['alter'] == 'orphan':
            os.kill(os.getppid(), signal.SIGKILL)
        elif os.fork():
            sys.exit(0)
        else:
            os.setsid()
            for number in (0, 1, 2):
                os.close(number)
        while os.path.exists(state['gate']):
            time.sleep(0.02)
        sys.exit(0 if locked(commit) else 3)
    if state.get('stop_tool_on') == 'alter' and 'ALTER' in stdin:
        os.kill(os.getppid(), signal.SIGTERM); os.kill(os.getppid(), signal.SIGHUP); time.sleep(0.2)
    while 'ALTER' in stdin and state.get('pause_alter') and os.path.exists(state['pause_alter']):
        time.sleep(0.05)
    mode = state.get('alter') if 'ALTER' in stdin else None
    if mode == 'error':
        sys.exit(1)
    if mode == 'hang':
        time.sleep(60)
    if not locked(commit):
        sys.exit(3)
    if mode == 'commit_then_error':
        sys.exit(1)
elif command == 'exec':
    api = containers()[argv[1]]
    sys.exit(0 if api['running'] and state['health'] else 1)
elif command == 'run' and 'sh' in argv and argv[argv.index('--entrypoint') + 1] == 'sh':
    role = re.search(r'user=(\w+)', argv[-1])[1]
    value = stdin.rstrip('\n').split(':', 4)[4].replace('\\:', ':').replace('\\\\', '\\')
    if state.get('probe') == 'down':
        print('error')
    else:
        print('accepted' if login(state['roles'][role], value) else 'refused')
elif command == 'run':
    source = next(item for item in argv if item.startswith('type=bind,src=')).split(',')[1][4:]
    with open(source) as file:
        dsn = json.load(file)['dsn']
    value = urllib.parse.unquote(re.fullmatch(r'postgresql://fcs_owner:([^@]+)@.*', dsn)[1])
    accepted = login(state['roles']['fcs_owner'], value) and state.get('db_roles', True)
    print(json.dumps({'db_roles_check': 'ok', 'schema_version': 8} if accepted else {'error': 'failed'}))
    sys.exit(0 if accepted else 1)
elif command == 'stop':
    locked(lambda state: state['containers'][argv[1]].update(running=False))
elif command == 'start' and argv[1] == '--attach':
    print(json.dumps({'calls': state['calls']}))
elif command == 'start':
    if state.get('start_fails'):
        sys.exit(1)
    locked(lambda state: state['containers'][argv[1]].update(running=True))
elif command == 'create':
    print('e' * 64)
elif command == 'kill':
    pass
elif command == 'rm':
    locked(lambda state: state['containers'].pop(argv[-1], None))
else:
    sys.exit(125)
'''

FAKE_RUNNER = r'''"""The installed runner as the tool starts it: the real admission and the real locks, a fake capture that really
logs in through owner.dsn. Marker words the tool looks for: rotation_in_progress."""
import json, os, re, subprocess, sys, urllib.parse
sys.path.insert(0, os.environ['ROTATE_TEST_SOURCE'])
from messaging_service import backup_runner as real
config = sys.argv[sys.argv.index('--config') + 1]
cfg = json.load(open(config))
if '--recover-stop' in sys.argv:
    if not json.load(open(os.environ['ROTATE_TEST_STATE'])).get('worker_running'):
        os.unlink(cfg['host_lock'] + '.unsettled')
        sys.exit(0)
    sys.exit(1)
status = os.path.join(cfg['directory'], 'status.json')
def done(state, id=None, code=None):
    with open(status, 'w') as file:
        json.dump({'state': state, 'id': id, 'error_code': code}, file)
try:
    real.rotation_admission(cfg)
    with real.hold_locks(cfg, 0):
        real.rotation_admission(cfg)
        state = json.load(open(os.environ['ROTATE_TEST_STATE']))
        if state.get('backup') == 'fail':
            raise RuntimeError('backup_job_failed')
        value = urllib.parse.unquote(re.fullmatch(r'postgresql://fcs_owner:([^@]+)@\S+', open(cfg['dsn_file']).read().strip())[1])
        answer = subprocess.run(['docker', 'run', '-i', '--entrypoint', 'sh', 'image', 'user=fcs_owner'], input=f'*:*:*:*:{value}\n',
                                capture_output=True, text=True, check=True).stdout.strip()
        if answer != 'accepted':
            raise RuntimeError('backup_capture_failed')
    done('ok', '20261105T012010Z-' + os.urandom(8).hex())
except RuntimeError as exc:
    done('error', code=str(exc))
    print(json.dumps({'status': 'error', 'code': str(exc)}))
    sys.exit(1)
'''

DRIVER = r'''"""One run of the tool with the host's constants pointed at the synthetic host."""
import errno, json, os, signal, sys
spec = json.load(open(sys.argv[1]))
sys.path.insert(0, spec['scripts'])
import calendar_rotate as rotate
for key, value in spec['constants'].items():
    setattr(rotate, key, tuple(rotate.Path(item) for item in value) if isinstance(value, list) else
            rotate.Path(value) if key not in ('RUNNER_PYTHON', 'ALTER_TIMEOUT', 'HEALTH_WAIT', 'HEALTH_POLL', 'PRODUCTION_PROJECT') else value)
rotate.running_as_root = lambda: True
armed = [False]
def checkpoint(name):
    if name == spec.get('kill_at'):
        os.kill(os.getpid(), signal.SIGKILL)
    if name == spec.get('write_fails_after'):
        armed[0] = True
rotate.checkpoint = checkpoint
real_write = os.write
def write(descriptor, data):
    if descriptor > 2 and armed[0]:
        armed[0] = False
        real_write(descriptor, bytes(data)[:len(data) // 2])
        if spec.get('write_failure') == 'kill':
            os.kill(os.getpid(), signal.SIGKILL)
        raise OSError(errno.ENOSPC, 'no space')
    if descriptor > 2 and spec.get('short_writes') and len(data) > 3:
        return real_write(descriptor, bytes(data)[:3])
    return real_write(descriptor, data)
os.write = write
if spec.get('audit_opens'):
    opened = []
    def audit(event, args):
        if event == 'open' and isinstance(args[0], str):
            opened.append(args[0])
    sys.addaudithook(audit)
code = rotate.main(spec['argv'])
if spec.get('audit_opens'):
    with open(spec['audit_opens'], 'w') as file:
        json.dump(opened, file)
sys.exit(code)
'''


def verifier(password):
    return rotate.scram_verifier(password, salt=hashlib.sha256(password.encode()).digest()[:16])


class Host:
    """The production layout under tmp_path, the fake docker first on PATH, each subcommand a real process."""

    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.bin = tmp_path / 'bin'
        self.bin.mkdir()
        (self.bin / 'docker').write_text(FAKE_DOCKER.replace('#!/usr/bin/env python3', '#!' + sys.executable, 1))
        (self.bin / 'docker').chmod(0o755)
        self.backup = tmp_path / 'calendar-backup'
        self.service = tmp_path / 'calendar-state' / 'service'
        self.global_dir = tmp_path / 'deedkit-production'
        self.plan_a = tmp_path / 'plan-a-state'
        self.messaging = tmp_path / 'messaging'
        for directory in (self.backup, self.backup / 'backups', self.service, self.global_dir, self.plan_a, self.messaging):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.owner_dsn = self.backup / 'owner.dsn'
        self.files = {'postgres.env': self.service / 'postgres.env', 'runtime.json': self.service / 'runtime.json',
                      'owner.dsn': self.owner_dsn, 'gateway.json': self.service / 'gateway.json',
                      'worker.json': self.service / 'worker.json'}
        self.write_profile(self.service, self.owner_dsn)
        self.runner_json = self.backup / 'runner.json'
        self.runner_json.write_text(json.dumps({
            'profile': 'calendar', 'directory': str(self.backup / 'backups'), 'project': 'deedkit-calendar',
            'network': 'deedkit-calendar_calendar-backend', 'dsn_file': str(self.owner_dsn),
            'state_mounts': {'vault': str(self.service / 'vault'), 'profile': str(self.service)},
            'host_lock': str(self.messaging / '.runner.lock')}))
        self.production_json = tmp_path / 'production.json'
        self.production_json.write_text(json.dumps({'state_directory': str(self.plan_a), 'projects': [
            {'name': 'deedkit-voice', 'services': {'voice': 'calls'}}]}))
        self.tool_file = tmp_path / 'installed-production.py'
        self.tool_file.write_text('rotation_running\n')
        self.runner_program = tmp_path / 'backup_runner.py'
        self.runner_program.write_text(FAKE_RUNNER)
        self.driver = tmp_path / 'driver.py'
        self.driver.write_text(DRIVER)
        self.state_path = self.bin / 'state.json'
        labels = {'com.docker.compose.project': 'deedkit-calendar'}
        self.state_path.write_text(json.dumps({
            'log': [], 'roles': {role: verifier(value) for role, value in OLD.items()}, 'health': True, 'calls': [],
            'networks': {'deedkit-calendar_calendar-backend': True},
            'containers': {
                DB: {'running': True, 'image': IMAGE, 'networks': ['deedkit-calendar_calendar-backend'],
                     'labels': {**labels, 'com.docker.compose.service': 'calendar-db'}},
                API: {'running': True, 'image': IMAGE, 'networks': ['deedkit-calendar_calendar-backend'],
                      'labels': {**labels, 'com.docker.compose.service': 'calendar-api'}},
                WORKER: {'running': True, 'image': IMAGE, 'networks': ['deedkit-calendar_calendar-backend'],
                         'labels': {**labels, 'com.docker.compose.service': 'calendar-worker'}}}}))
        self.journal_dir = self.backup / 'rotation'
        self.marker = self.journal_dir / 'rotation.unsettled'
        self.journal = self.journal_dir / 'journal.json'
        self.retired = self.backup / 'retired-passwords'
        self.runs = 0

    @staticmethod
    def write_profile(service, owner_dsn=None):
        def private(path, text):
            path.write_text(text)
            path.chmod(0o600)
        private(service / 'postgres.env', f"POSTGRES_USER=fcs_owner\nPOSTGRES_DB=family_calendar\nPOSTGRES_PASSWORD={OLD['fcs_owner']}\n")
        private(service / 'runtime.json', json.dumps({'dsn': f"postgresql://fcs_owner:{OLD['fcs_owner']}@calendar-db:5432/family_calendar", 'port': 13232}))
        for name, role in (('gateway', 'fcs_gateway'), ('worker', 'fcs_worker')):
            private(service / f'{name}.json', json.dumps({**PROFILE, 'dsn': f'postgresql://{role}:{OLD[role]}@calendar-db:5432/family_calendar'}))
        if owner_dsn:
            private(owner_dsn, f"postgresql://fcs_owner:{OLD['fcs_owner']}@calendar-db:5432/family_calendar\n")

    def constants(self):
        return {'RUNNER_CONFIG': str(self.runner_json), 'PRODUCTION_CONFIG': str(self.production_json),
                'GLOBAL_LOCK_DIR': str(self.global_dir), 'RUNNER_PROGRAM': str(self.runner_program),
                'INSTALLED_TOOL': str(self.tool_file), 'RUNNER_PYTHON': sys.executable, 'ALTER_TIMEOUT': 2,
                'HEALTH_WAIT': 0, 'HEALTH_POLL': 0, 'PRODUCTION_ROOTS': [str(self.backup), str(self.service.parent)]}

    def env(self):
        return {**os.environ, 'PATH': f"{self.bin}{os.pathsep}{os.environ['PATH']}", 'PYTHONDONTWRITEBYTECODE': '1',
                'ROTATE_TEST_SOURCE': str(ROOT / 'services' / 'messaging'), 'ROTATE_TEST_STATE': str(self.state_path)}

    def start(self, *argv, stdout=subprocess.PIPE, **options):
        self.runs += 1
        spec = self.tmp / f'spec-{self.runs}.json'
        spec.write_text(json.dumps({'scripts': str(ROOT / 'scripts'), 'constants': self.constants(), 'argv': list(argv), **options}))
        return subprocess.Popen([sys.executable, str(self.driver), str(spec)], stdout=stdout, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, env=self.env(), text=True)

    def tool(self, *argv, **options):
        process = self.start(*argv, **options)
        output, _ = process.communicate(timeout=60)
        self.last_output = (getattr(self, 'last_output', '')) + output
        lines = [json.loads(line) for line in output.splitlines() if line.startswith('{')]
        return process.returncode, (lines[-1] if lines else {}), output

    def ok(self, *argv, **options):
        code, line, output = self.tool(*argv, **options)
        assert code == 0, output
        return line

    def state(self):
        return json.loads(self.state_path.read_text())

    def change(self, **fields):
        state = self.state()
        state.update(fields)
        self.state_path.write_text(json.dumps(state))

    def accepted(self, role, value):
        match = re.fullmatch(r'SCRAM-SHA-256\$(\d+):([^$]+)\$([^:]+):(.+)', self.state()['roles'][role])
        salted = hashlib.pbkdf2_hmac('sha256', value.encode(), base64.b64decode(match[2]), int(match[1]))
        client = hmac.new(salted, b'Client Key', hashlib.sha256).digest()
        return base64.b64encode(hashlib.sha256(client).digest()).decode() == match[3]

    def owner_values(self):
        env = re.search(r'^POSTGRES_PASSWORD=(.*)$', self.files['postgres.env'].read_text(), re.M)[1]
        runtime = re.fullmatch(r'postgresql://fcs_owner:([^@]+)@.*', json.loads(self.files['runtime.json'].read_text())['dsn'])[1]
        dsn = re.fullmatch(r'postgresql://fcs_owner:([^@]+)@\S+\n', self.files['owner.dsn'].read_text())[1]
        return {env, runtime, dsn}

    def role_value(self, name):
        return re.fullmatch(r'postgresql://\w+:([^@]+)@.*', json.loads(self.files[name].read_text())['dsn'])[1]

    def assert_owner_rotated(self):
        """The database and all files agree on one new owner value, and the replaced one is refused."""
        values = self.owner_values()
        assert len(values) == 1
        value = values.pop()
        assert value != OLD['fcs_owner'] and self.accepted('fcs_owner', value) and not self.accepted('fcs_owner', OLD['fcs_owner'])
        return value

    def assert_roles_rotated(self):
        for name, role in (('gateway.json', 'fcs_gateway'), ('worker.json', 'fcs_worker')):
            value = self.role_value(name)
            assert value != OLD[role] and self.accepted(role, value) and not self.accepted(role, OLD[role])

    def operator_credential_works(self):
        """At every moment one value on disk (a profile file, a copy, the staged file) still logs the owner in."""
        found = set(self.owner_values())
        for path in self.journal_dir.rglob('*'):
            if path.is_file() and path.name == 'new.json':
                found.add(json.loads(path.read_text())['fcs_owner'])
            if path.is_file() and path.name == 'postgres.env':
                found.add(re.search(r'^POSTGRES_PASSWORD=(.*)$', path.read_text(), re.M)[1])
        return any(self.accepted('fcs_owner', value) for value in found)

    def calls_of(self, command):
        return [entry['argv'] for entry in self.state()['log'] if entry['argv'][0] == command]

    def runner_cfg(self):
        return json.loads(self.runner_json.read_text())

    def runner_refuses(self):
        """The real admission, run in a process of its own as the unit starts it."""
        process = subprocess.run([sys.executable, str(self.runner_program), '--config', str(self.runner_json)],
                                 env={key: value for key, value in self.env().items() if key != rotate.TOKEN_ENV},
                                 capture_output=True, text=True, timeout=30)
        return process.returncode == 1 and 'rotation_in_progress' in process.stdout

    def deploy_refuses(self, monkeypatch):
        monkeypatch.setattr(p, 'ROTATION_MARKER', self.marker)
        return p.rotation_running() is not None

    def full_rotation(self, which='owner'):
        self.ok('apply', which)
        self.ok('verify')
        return self.ok('finish')


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# spec: ANYM-784
def test_the_verifier_matches_the_published_scram_sha_256_vector():
    """RFC 7677's exchange: its server signature and client proof follow from the stored keys of this verifier."""
    salt = base64.b64decode('W22ZaJ0SNY7soEsUEjb6gQ==')
    verifier_text = rotate.scram_verifier('pencil', salt=salt, iterations=4096)
    match = re.fullmatch(r'SCRAM-SHA-256\$4096:([^$]+)\$([^:]+):(.+)', verifier_text)
    assert base64.b64decode(match[1]) == salt
    stored, server = base64.b64decode(match[2]), base64.b64decode(match[3])
    auth = (b'n=user,r=rOprNGfwEbeRWgbNEkqO,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,'
            b's=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096,c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0')
    assert base64.b64encode(hmac.new(server, auth, hashlib.sha256).digest()).decode() == '6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4='
    proof = base64.b64decode('dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=')
    signature = hmac.new(stored, auth, hashlib.sha256).digest()
    client_key = bytes(a ^ b for a, b in zip(proof, signature))
    assert hashlib.sha256(client_key).digest() == stored


# spec: ANYM-784
def test_the_plan_changes_nothing_and_prints_fingerprints_only(host):
    before = {name: path.read_bytes() for name, path in host.files.items()}
    line = host.ok('plan')
    assert line['status'] == 'plan' and line['ready'] is True
    assert line['fcs_owner'] == {'fingerprint': rotate.fingerprint(OLD['fcs_owner']), 'database': 'accepted'}
    assert {name: path.read_bytes() for name, path in host.files.items()} == before
    assert not host.journal_dir.exists() and not host.calls_of('stop') and not host.retired.exists()
    assert all('ALTER' not in entry['stdin'] for entry in host.state()['log'])
    assert not any(value in host.last_output for value in OLD.values())


# spec: ANYM-784
def test_an_owner_rotation_runs_the_whole_sequence_on_the_existing_containers(host):
    line = host.ok('apply', 'owner')
    assert line['status'] == 'verifying' and line['next'] == 'verify'
    assert json.loads(host.marker.read_text())['phase'] == 'verifying'
    assert stat.S_IMODE(host.marker.stat().st_mode) == 0o600 and stat.S_IMODE(host.journal_dir.stat().st_mode) == 0o700
    value = host.assert_owner_rotated()
    line = host.ok('verify')
    assert line['status'] == 'verified' and line['backup_id']
    line = host.ok('finish')
    assert line['status'] == 'finished' and line['new'] == {'fcs_owner': rotate.fingerprint(value)}
    assert not host.marker.exists() and not (host.journal_dir / 'staged').exists()
    assert json.loads(host.journal.read_text())['phase'] == 'finished'
    # Stop and start of the same containers only: no restart, recreate, removal or build.
    verbs = {entry['argv'][0] for entry in host.state()['log']}
    assert verbs <= {'ps', 'inspect', 'network', 'exec', 'run', 'stop', 'start', 'create', 'kill'}
    assert [argv[1] for argv in host.calls_of('stop')] == [WORKER, API]
    assert [argv[1] for argv in host.calls_of('start')] == [API, WORKER]
    assert host.state()['roles']['fcs_gateway'] == verifier(OLD['fcs_gateway'])  # one role set at a time
    assert host.role_value('gateway.json') == OLD['fcs_gateway']


# spec: ANYM-784
def test_the_service_roles_rotate_together_and_their_profiles_lose_no_key(host):
    host.files['gateway.json'].chmod(0o640)
    uid, gid = os.stat(host.files['gateway.json']).st_uid, os.stat(host.files['gateway.json']).st_gid
    host.full_rotation('roles')
    host.assert_roles_rotated()
    for name in ('gateway.json', 'worker.json'):
        document = json.loads(host.files[name].read_text())
        assert {key: document[key] for key in KEYS_KEPT} == {key: PROFILE[key] for key in KEYS_KEPT}
    assert host.role_value('gateway.json') != host.role_value('worker.json')
    info = os.stat(host.files['gateway.json'])
    assert stat.S_IMODE(info.st_mode) == 0o640 and (info.st_uid, info.st_gid) == (uid, gid)
    assert host.owner_values() == {OLD['fcs_owner']} and not host.retired.exists()  # the retired list is the owner's


# spec: ANYM-784
def test_every_other_line_of_the_env_and_dsn_files_stays(host):
    host.files['postgres.env'].write_text(f"POSTGRES_USER=fcs_owner\nPOSTGRES_DB=family_calendar\nPOSTGRES_PASSWORD={OLD['fcs_owner']}\nEXTRA=kept\n")
    host.files['owner.dsn'].write_text(f"postgresql://fcs_owner:{OLD['fcs_owner']}@calendar-db:5432/family_calendar?application_name=x\n")
    host.ok('apply', 'owner')
    text = host.files['postgres.env'].read_text()
    assert text.startswith('POSTGRES_USER=fcs_owner\nPOSTGRES_DB=family_calendar\nPOSTGRES_PASSWORD=') and text.endswith('\nEXTRA=kept\n')
    assert host.files['owner.dsn'].read_text().endswith('@calendar-db:5432/family_calendar?application_name=x\n')


BOUNDARIES = ('marker', 'staged', 'before-alter', 'after-alter', 'settled', 'altered', 'file:postgres.env',
              'file:runtime.json', 'file:owner.dsn', 'files', 'restarted', 'verifying')


# spec: ANYM-784
@pytest.mark.parametrize('boundary', BOUNDARIES)
def test_a_kill_at_any_boundary_of_apply_recovers_to_one_new_set(host, monkeypatch, boundary):
    code, _, _ = host.tool('apply', 'owner', kill_at=boundary)
    assert code == -signal.SIGKILL
    # The durable admission holds after the process died, and no credential is lost.
    assert host.marker.exists() and host.runner_refuses() and host.deploy_refuses(monkeypatch)
    assert host.operator_credential_works()
    line = host.ok('recover')
    if boundary in ('marker', 'staged'):  # no ALTER was sent: it aborts and nothing has changed
        assert line['status'] == 'aborted' and not host.marker.exists()
        assert host.owner_values() == {OLD['fcs_owner']} and host.accepted('fcs_owner', OLD['fcs_owner'])
        assert not host.retired.exists() and not (host.journal_dir / 'staged').exists()
        assert not host.calls_of('stop')
        return
    assert line['status'] == 'verifying'
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()
    assert not host.marker.exists() and not (host.journal_dir / 'staged').exists()
    assert host.retired.read_text().split() == [rotate.fingerprint(OLD['fcs_owner'])]


# spec: ANYM-784
@pytest.mark.parametrize('boundary', ['before-alter', 'file:worker.json', 'files'])
def test_a_kill_of_the_service_role_rotation_recovers_too(host, boundary):
    assert host.tool('apply', 'roles', kill_at=boundary)[0] == -signal.SIGKILL
    host.ok('recover')
    host.ok('verify')
    host.ok('finish')
    host.assert_roles_rotated()
    assert json.loads(host.files['gateway.json'].read_text())['allowed_origins'] == PROFILE['allowed_origins']


# spec: ANYM-784
@pytest.mark.parametrize('boundary', ['finish-journal', 'finish-copies', 'finished'])
def test_a_kill_during_finish_ends_in_finished(host, boundary):
    host.ok('apply', 'owner')
    host.ok('verify')
    code, _, _ = host.tool('finish', kill_at=boundary)
    assert code == -signal.SIGKILL
    value = host.assert_owner_rotated()
    line = host.ok('recover')
    assert line['status'] in ('finished', 'nothing_to_recover')
    assert not host.marker.exists() and not (host.journal_dir / 'staged').exists()
    assert host.assert_owner_rotated() == value and json.loads(host.journal.read_text())['phase'] == 'finished'


# spec: ANYM-784
def test_recover_after_finish_changes_nothing(host):
    host.full_rotation()
    value = host.assert_owner_rotated()
    stops = len(host.calls_of('stop'))
    assert host.ok('recover')['status'] == 'nothing_to_recover'
    assert host.assert_owner_rotated() == value and len(host.calls_of('stop')) == stops


# spec: ANYM-784
def test_a_recovery_that_is_killed_itself_can_run_again(host):
    host.tool('apply', 'owner', kill_at='file:runtime.json')
    assert host.tool('recover', kill_at='file:owner.dsn')[0] == -signal.SIGKILL
    host.ok('recover')
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()


# spec: ANYM-784
def test_a_write_killed_half_way_leaves_the_file_whole_and_recover_removes_the_staged_rest(host):
    before = host.files['runtime.json'].read_bytes()
    code, _, _ = host.tool('apply', 'owner', write_fails_after='file:runtime.json', write_failure='kill')
    assert code == -signal.SIGKILL
    assert host.files['runtime.json'].read_bytes() == before and json.loads(before)
    assert [path.name for path in host.service.glob('.*.tmp')]
    host.ok('recover')
    assert not list(host.service.glob('.*.tmp'))
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()


# spec: ANYM-784
def test_a_failed_write_keeps_the_file_and_short_writes_are_completed(host):
    code, line, _ = host.tool('apply', 'owner', write_fails_after='file:runtime.json')
    assert code == 1 and line['code'] == 'os_error' and line['errno'] == 'ENOSPC'
    assert json.loads(host.files['runtime.json'].read_text())['port'] == 13232
    assert host.marker.exists() and not list(host.service.glob('.*.tmp'))
    host.ok('recover', short_writes=True)  # every write takes three bytes: the loop still writes all of it
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()


# spec: ANYM-784
@pytest.mark.parametrize('mode', ['commit_then_error', 'error', 'hang'])
def test_an_uncertain_alter_is_settled_by_the_probe_before_any_file_changes(host, mode):
    host.change(alter=mode)
    before = {name: path.read_bytes() for name, path in host.files.items()}
    code, line, _ = host.tool('apply', 'owner')
    if mode == 'commit_then_error':  # it did commit: the probe says so and the run goes on
        assert code == 0 and line['status'] == 'verifying'
        host.assert_owner_rotated()
    else:  # it did not: no file changed, and the marker keeps everyone out until recover
        assert code == 1 and line['code'] == 'alter_not_applied'
        assert {name: path.read_bytes() for name, path in host.files.items()} == before
        assert host.accepted('fcs_owner', OLD['fcs_owner']) and host.marker.exists() and not host.retired.exists()
        host.change(alter=None)
        host.ok('recover')
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()


# spec: ANYM-784
def test_a_database_the_probe_cannot_reach_changes_no_file(host):
    code = host.tool('apply', 'owner', kill_at='after-alter')[0]
    assert code == -signal.SIGKILL
    before = {name: path.read_bytes() for name, path in host.files.items()}
    host.change(probe='down')
    code, line, _ = host.tool('recover')
    assert code == 1 and line['code'] in ('database_unsettled', 'alter_uncertain')
    assert {name: path.read_bytes() for name, path in host.files.items()} == before and host.marker.exists()
    host.change(probe=None)
    host.ok('recover')
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()


# spec: ANYM-784
def test_a_lost_staged_value_is_replaced_by_a_fresh_one_never_by_the_old_one(host):
    host.tool('apply', 'owner', kill_at='file:runtime.json')
    written = host.owner_values() - {OLD['fcs_owner']}
    shutil.rmtree(host.journal_dir / 'staged' / json.loads(host.journal.read_text())['id'] / 'old', ignore_errors=False)
    (host.journal_dir / 'staged' / json.loads(host.journal.read_text())['id'] / 'new.json').unlink()
    host.ok('recover')
    value = host.assert_owner_rotated()
    assert value not in written and not host.accepted('fcs_owner', written.pop())
    host.ok('verify')
    host.ok('finish')
    assert host.assert_owner_rotated() == value


# spec: ANYM-784
@pytest.mark.parametrize('mode, commits_first', [('late', False), ('orphan', False), ('late', True), ('orphan', True)])
def test_an_alter_that_starts_after_its_caller_cannot_undo_the_recovery(host, mode, commits_first):
    """Astra r1 and Sol r2 M1: the staged value is lost, so the recovery makes a fresh one. The old exec may start at
    any moment, also after everything looked settled: its fence finds another verifier and it changes nothing."""
    gate = host.tmp / 'gate'
    gate.write_text('')
    host.change(alter=mode, gate=str(gate))
    code = host.tool('apply', 'owner')[0]
    assert code == (1 if mode == 'late' else -signal.SIGKILL)
    host.change(alter=None)
    staged = host.journal_dir / 'staged' / json.loads(host.journal.read_text())['id'] / 'new.json'
    lost = json.loads(staged.read_text())['fcs_owner']
    staged.unlink()
    if commits_first:  # it won the race: the recovery replaces what it wrote
        gate.unlink()
        deadline = time.monotonic() + 30
        while not host.accepted('fcs_owner', lost):
            assert time.monotonic() < deadline
            time.sleep(0.02)
    host.ok('recover')
    value = host.assert_owner_rotated()
    assert value != lost and not host.accepted('fcs_owner', lost)
    if not commits_first:
        gate.unlink()
        time.sleep(0.5)
    assert host.assert_owner_rotated() == value and not host.accepted('fcs_owner', lost)
    host.ok('verify')
    host.ok('finish')


# spec: ANYM-784
def test_every_alter_is_fenced_on_the_verifier_it_replaces_with_deadlines_the_server_enforces(host):
    host.ok('apply', 'roles')
    entry = next(entry for entry in host.state()['log'] if 'ALTER ROLE' in entry['stdin'])
    for role in ('fcs_gateway', 'fcs_worker'):
        assert f"rolname = '{role}') <> '{verifier(OLD[role])}'" in entry['stdin']
    stdin = entry['stdin']
    assert stdin.index('pg_advisory_xact_lock') < stdin.index('rotation_fence') < stdin.index('ALTER ROLE')
    for setting in ('lock_timeout', 'statement_timeout', 'idle_in_transaction_session_timeout'):
        assert stdin.index(f'SET LOCAL {setting} = ') < stdin.index('pg_advisory_xact_lock')


# spec: ANYM-784
def test_a_fence_that_finds_another_verifier_leaves_the_files_alone(host):
    """A stored verifier that changed between the read and the ALTER: nothing commits and recover completes."""
    host.tool('apply', 'owner', kill_at='before-alter')
    before = {name: path.read_bytes() for name, path in host.files.items()}
    state = host.state()
    state['fence_race'] = True
    host.state_path.write_text(json.dumps(state))
    code, line, _ = host.tool('recover')
    assert code == 1 and line['code'] == 'database_unsettled'
    assert {name: path.read_bytes() for name, path in host.files.items()} == before and host.marker.exists()
    host.change(fence_race=False)
    host.ok('recover')
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()


# spec: ANYM-784
def test_a_helper_container_left_in_any_state_blocks_nothing(host):
    """Astra r1 M2: a client that died between create and start: `docker kill` cannot remove such a container."""
    host.ok('apply', 'owner')
    state = host.state()
    stranded, foreign = '7' * 64, '6' * 64
    state['containers'][stranded] = {'running': False, 'image': IMAGE, 'networks': ['deedkit-calendar_calendar-backend'],
                                     'labels': {rotate.HELPER_LABEL: DB}}
    state['containers'][foreign] = {'running': False, 'image': IMAGE, 'networks': [], 'labels': {rotate.HELPER_LABEL: 'another-database'}}
    host.state_path.write_text(json.dumps(state))
    host.ok('verify', '--no-backup')
    assert host.calls_of('rm') == [['rm', '--force', stranded]] and foreign in host.state()['containers']
    host.ok('verify')
    names = [argv[argv.index('--name') + 1] for argv in host.calls_of('run') if '--name' in argv]
    assert len(names) == len(set(names))
    checks = [argv for argv in host.calls_of('run') if argv[-2:] == ['db-roles', '--check']]
    assert len(checks) == 2 and all(f'{rotate.HELPER_LABEL}={DB}' in argv for argv in checks)
    host.ok('finish')


# spec: ANYM-784
def test_a_recovery_waits_for_a_backup_worker_left_by_a_dead_runner(host):
    """Astra r1 M3: its flocks are free, its `.unsettled` is not, and it holds the owner credentials."""
    host.tool('apply', 'owner', kill_at='altered')
    unsettled = host.messaging / '.runner.lock.unsettled'
    unsettled.write_text('{}')
    host.change(worker_running=True)
    before = {name: path.read_bytes() for name, path in host.files.items()}
    code, line, _ = host.tool('recover')
    assert code == 75 and line['code'] == 'backup_unsettled' and line['next'] == 'recover'
    assert {name: path.read_bytes() for name, path in host.files.items()} == before and not host.calls_of('stop')
    # The runner's own recovery runs first and settles it as soon as the worker is gone.
    host.change(worker_running=False)
    assert host.ok('recover')['status'] == 'verifying' and not unsettled.exists()
    host.ok('verify')
    host.ok('finish')
    host.assert_owner_rotated()


# spec: ANYM-784
def test_a_retained_upload_does_not_stop_a_recovery(host):
    """A journal without `.unsettled` holds an encrypted bundle and no credential."""
    host.tool('apply', 'owner', kill_at='altered')
    (host.backup / 'backups' / 'journal.json').write_text('{}')
    assert host.ok('recover')['status'] == 'verifying'
    host.assert_owner_rotated()


# spec: ANYM-784
def test_no_value_reaches_an_argument_a_log_or_the_output(host):
    host.tool('apply', 'owner', kill_at='files')
    host.ok('recover')
    host.ok('verify')
    host.ok('finish')
    new = host.assert_owner_rotated()
    canaries = [new, OLD['fcs_owner']]
    log = host.state()['log']
    for entry in log:
        assert not any(value in ' '.join(entry['argv']) for value in canaries), entry['argv'][:3]
    # The ALTER carries the verifier; only the probe's stdin (a tmpfs passfile in a throwaway container) has a value.
    altering = [entry for entry in log if 'ALTER ROLE' in entry['stdin']]
    assert altering and all('SCRAM-SHA-256$4096:' in entry['stdin'] for entry in altering)
    assert not any(value in entry['stdin'] for entry in altering for value in canaries)
    assert all(entry['argv'][0] == 'run' and '--entrypoint' in entry['argv'] for entry in log
               if any(value in entry['stdin'] for value in canaries))
    for text in (host.last_output, (host.journal_dir / 'rotation.log').read_text(), host.journal.read_text()):
        assert not any(value in text for value in canaries)
        assert 'postgresql://' not in text
    assert host.retired.read_text().split() == [rotate.fingerprint(OLD['fcs_owner'])]


# spec: ANYM-784
def test_signals_and_a_dropped_terminal_do_not_stop_the_critical_section(host):
    host.change(stop_tool_on='alter')
    process = host.start('apply', 'owner')
    process.stdout.close()  # the ssh session's pipe is gone: every later line would hit EPIPE
    assert process.wait(timeout=60) == 0
    assert json.loads(host.marker.read_text())['phase'] == 'verifying'
    host.assert_owner_rotated()
    assert (host.journal_dir / 'rotation.log').read_text().count('"status": "verifying"') == 1


# spec: ANYM-784
def test_two_applies_at_once_one_is_busy(host):
    gate = host.tmp / 'gate'
    gate.write_text('')
    host.change(pause_alter=str(gate))
    first = host.start('apply', 'owner')
    deadline = time.monotonic() + 30
    while not host.marker.exists() or not any('ALTER' in entry['stdin'] for entry in host.state()['log']):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    code, line, _ = host.tool('apply', 'owner')
    assert code == 75 and line == {'code': 'rotation_tool_running', 'status': 'busy'}
    gate.unlink()
    assert first.wait(timeout=60) == 0
    first.stdout.close()
    host.assert_owner_rotated()


# spec: ANYM-784
def test_a_new_apply_on_an_unsettled_marker_is_busy_and_names_recover(host):
    host.tool('apply', 'owner', kill_at='altered')
    code, line, _ = host.tool('apply', 'owner')
    assert code == 75 and line['code'] == 'rotation_unsettled' and line['next'] == 'recover'


def hold_lock(path):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return descriptor


# spec: ANYM-784
@pytest.mark.parametrize('name, code', [('runner', 'backup_running'), ('host', 'backup_running'),
                                        ('global', 'deploy_running'), ('plan_a', 'deploy_running')])
def test_a_held_lock_refuses_busy_before_anything_changes(host, name, code):
    path = {'runner': host.backup / 'backups' / '.runner.lock', 'host': host.messaging / '.runner.lock',
            'global': host.global_dir / 'deploy.lock', 'plan_a': host.plan_a / 'deploy.lock'}[name]
    descriptor = hold_lock(path)
    try:
        before = {name: path.read_bytes() for name, path in host.files.items()}
        found, line, _ = host.tool('apply', 'owner')
        assert found == 75 and line == {'code': code, 'status': 'busy'}
        assert {name: path.read_bytes() for name, path in host.files.items()} == before
        assert not host.marker.exists() and all('ALTER' not in entry['stdin'] for entry in host.state()['log'])
    finally:
        os.close(descriptor)
    host.ok('apply', 'owner')


# spec: ANYM-784
def test_recover_and_finish_refuse_busy_while_the_production_lock_is_held(host):
    host.tool('apply', 'owner', kill_at='altered')
    descriptor = hold_lock(host.global_dir / 'deploy.lock')
    try:
        code, line, _ = host.tool('recover')
        assert code == 75 and line['code'] == 'deploy_running' and host.marker.exists()
    finally:
        os.close(descriptor)
    host.ok('recover')
    host.ok('verify')
    descriptor = hold_lock(host.global_dir / 'deploy.lock')
    try:
        code, line, _ = host.tool('finish')
        assert code == 75 and line['code'] == 'deploy_running' and host.marker.exists()
    finally:
        os.close(descriptor)
    host.ok('finish')


# spec: ANYM-784
def test_a_rotation_and_a_competing_deploy_at_the_same_time(host, tmp_path, monkeypatch):
    """Each side refuses the other: the deploy reads the marker with its locks held, the tool reads their locks."""
    (tmp_path / 'deployer').mkdir()
    deployer = Deployer(tmp_path / 'deployer', monkeypatch)
    monkeypatch.setattr(p, 'ROTATION_MARKER', host.marker)
    host.tool('apply', 'owner', kill_at='before-alter')
    gate = host.tmp / 'gate'
    gate.write_text('')
    host.change(pause_alter=str(gate))
    recovering = host.start('recover')
    deadline = time.monotonic() + 30
    while not any('ALTER' in entry['stdin'] for entry in host.state()['log']) and recovering.poll() is None:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    for auto in (False, True):
        outcome = deployer.deploy(auto=auto)
        assert outcome['status'] == 'busy' and outcome['code'] == 'rotation_running'
    assert deployer.steps == [] and deployer.git == []
    gate.unlink()
    assert recovering.wait(timeout=60) == 0
    recovering.stdout.close()
    assert deployer.deploy()['status'] == 'busy'  # still until `finished`
    host.ok('verify')
    host.ok('finish')
    assert deployer.deploy()['status'] == 'deployed'
    host.assert_owner_rotated()


# spec: ANYM-784
def test_an_active_or_unknown_call_refuses_before_anything_changes_and_before_a_restart(host):
    voice = {'running': True, 'image': IMAGE, 'networks': [], 'calls': True,
             'labels': {'com.docker.compose.project': 'deedkit-voice', 'com.docker.compose.service': 'voice'}}
    state = host.state()
    state['containers']['f' * 64] = voice
    state['calls'] = [['00000000-0000-4000-8000-000000000001', 'active']]
    host.state_path.write_text(json.dumps(state))
    code, line, _ = host.tool('apply', 'owner')
    assert code == 75 and line['code'] == 'active_call' and not host.marker.exists()
    # A call that starts during the rotation stops it before the restart, forward only.
    host.change(calls=[])
    assert host.tool('apply', 'owner', kill_at='files')[0] == -signal.SIGKILL
    host.change(calls=[['00000000-0000-4000-8000-000000000001', 'active']])
    code, line, _ = host.tool('recover')
    assert code == 75 and line['code'] == 'active_call' and not host.calls_of('stop') and host.marker.exists()
    host.change(calls=[])
    host.ok('recover')
    assert len(host.calls_of('stop')) == 2
    # An unreadable call state is a call.
    host.ok('verify')
    host.change(calls='not-a-list')
    code, line, _ = host.tool('finish')
    assert code == 75 and line['code'] == 'call_state_unknown' and host.marker.exists()
    host.change(calls=[])
    host.ok('finish')


# spec: ANYM-784
@pytest.mark.parametrize('leftover', ['unsettled', 'journal'])
def test_an_unsettled_backup_refuses_a_rotation(host, leftover):
    (host.messaging / '.runner.lock.unsettled' if leftover == 'unsettled' else host.backup / 'backups' / 'journal.json').write_text('{}')
    code, line, _ = host.tool('apply', 'owner')
    assert code == 75 and line['code'] == 'backup_unsettled' and not host.marker.exists()


# spec: ANYM-784
@pytest.mark.parametrize('stale', ['runner', 'tool'])
def test_a_host_without_the_marker_readers_refuses_a_rotation(host, stale):
    (host.runner_program if stale == 'runner' else host.tool_file).write_text('# from before this card\n')
    code, line, _ = host.tool('apply', 'owner')
    assert code == 1 and line['code'] == 'admission_not_installed' and not host.marker.exists()


# spec: ANYM-784
def test_finish_needs_the_verified_marker_and_the_backup(host):
    host.ok('apply', 'owner')
    code, line, _ = host.tool('finish')
    assert code == 1 and line['code'] == 'not_verified' and host.marker.exists()
    assert host.ok('verify', '--no-backup')['backup_id'] is None
    code, line, _ = host.tool('finish')
    assert code == 1 and line['code'] == 'backup_missing' and host.marker.exists()
    host.change(backup='fail')
    code, line, _ = host.tool('verify')
    assert code == 1 and line['code'] == 'backup_failed'
    assert host.tool('finish')[1]['code'] == 'backup_missing'
    host.change(backup=None)
    assert host.ok('verify')['backup_id']
    host.ok('finish')


# spec: ANYM-784
def test_the_backup_handoff_takes_the_locks_the_tool_released(host, monkeypatch):
    """The runner's real locks and admission: the tool's own run passes in `verifying`, any other start is refused."""
    host.ok('apply', 'owner')
    assert host.runner_refuses() and host.deploy_refuses(monkeypatch)
    assert host.ok('verify')['backup_id']
    status = json.loads((host.backup / 'backups' / 'status.json').read_text())
    assert status['state'] == 'ok'
    assert host.runner_refuses()  # still until `finished`
    host.ok('finish')
    assert not host.runner_refuses() and not host.deploy_refuses(monkeypatch)


# spec: ANYM-784
@pytest.mark.parametrize('failure, expected', [
    ('health', 'health'), ('db_roles', 'db_roles'), ('old_accepted', 'old_refused:fcs_owner'),
    ('new_refused', 'new_accepted:fcs_owner'), ('recreated', 'identities'), ('stopped', 'running'), ('file', 'file:runtime.json')])
def test_verify_fails_for_every_check_and_finish_stays_refused(host, failure, expected):
    host.ok('apply', 'owner')
    state = host.state()
    if failure == 'health':
        state['health'] = False
    elif failure == 'db_roles':
        state['db_roles'] = False
    elif failure == 'old_accepted':
        state['roles']['fcs_owner'] = verifier(OLD['fcs_owner'])
    elif failure == 'new_refused':
        state['roles']['fcs_owner'] = verifier('something-else')
    elif failure == 'recreated':
        state['containers']['9' * 64] = state['containers'].pop(API)
    elif failure == 'stopped':
        state['containers'][WORKER]['running'] = False
    else:
        document = json.loads(host.files['runtime.json'].read_text())
        document['dsn'] = re.sub(r'fcs_owner:[^@]+@', 'fcs_owner:other@', document['dsn'])
        host.files['runtime.json'].write_text(json.dumps(document))
    host.state_path.write_text(json.dumps(state))
    code, line, _ = host.tool('verify')
    assert code == 1 and line['code'] == 'verify_failed' and expected in line['failed']
    assert host.tool('finish')[1]['code'] == 'not_verified' and host.marker.exists()


# spec: ANYM-784
def test_files_that_disagree_or_a_stale_value_refuse_before_anything_changes(host):
    original = host.files['owner.dsn'].read_text()
    host.files['owner.dsn'].write_text(original.replace(OLD['fcs_owner'], 'different'))
    code, line, _ = host.tool('apply', 'owner')
    assert code == 1 and line['code'] == 'profile_disagrees' and not host.marker.exists()
    host.files['owner.dsn'].write_text(original)
    state = host.state()
    state['roles']['fcs_owner'] = verifier('another')
    host.state_path.write_text(json.dumps(state))
    code, line, _ = host.tool('apply', 'owner')
    assert code == 1 and line['code'] == 'current_value_not_accepted' and not host.marker.exists()


# spec: ANYM-784
def test_the_retired_list_grows_and_never_shrinks(host):
    host.retired.write_text('aaaaaaaaaaaa\n')
    host.retired.chmod(0o644)
    host.full_rotation()
    first = host.assert_owner_rotated()
    assert host.retired.read_text().split() == ['aaaaaaaaaaaa', rotate.fingerprint(OLD['fcs_owner'])]
    assert stat.S_IMODE(host.retired.stat().st_mode) == 0o600
    host.full_rotation()
    assert host.retired.read_text().split() == ['aaaaaaaaaaaa', rotate.fingerprint(OLD['fcs_owner']), rotate.fingerprint(first)]


# spec: ANYM-784
@pytest.mark.parametrize('present, kept', [('aaaaaaaaaaaa\n012345', ['aaaaaaaaaaaa']), ('012345', []),
                                           ('aaaaaaaaaaaa\nbbbbbbbbbbbb', ['aaaaaaaaaaaa', 'bbbbbbbbbbbb'])])
def test_a_torn_line_in_the_retired_list_leaves_every_whole_entry_and_the_new_one(host, present, kept):
    """Astra r1 M6: an append after a torn line used to glue the two into one malformed line."""
    host.retired.write_text(present)
    host.ok('apply', 'owner')
    assert host.retired.read_text() == ''.join(entry + '\n' for entry in [*kept, rotate.fingerprint(OLD['fcs_owner'])])


# spec: ANYM-784
def test_a_kill_while_the_retired_list_is_written_leaves_it_whole_and_recover_completes_it(host):
    host.retired.write_text('aaaaaaaaaaaa\n')
    code = host.tool('apply', 'owner', write_fails_after='settled', write_failure='kill')[0]
    assert code == -signal.SIGKILL
    assert host.retired.read_text() == 'aaaaaaaaaaaa\n' and list(host.backup.glob('.retired-passwords.*.tmp'))
    host.ok('recover')
    assert host.retired.read_text().split() == ['aaaaaaaaaaaa', rotate.fingerprint(OLD['fcs_owner'])]
    assert not list(host.backup.glob('.retired-passwords.*.tmp'))


# spec: ANYM-784
def test_a_retired_list_that_cannot_be_written_refuses_before_the_alter(host):
    host.retired.symlink_to(host.tmp / 'elsewhere')
    code, line, _ = host.tool('apply', 'owner')
    assert code == 1 and line['code'] == 'retired_list_unusable' and not host.marker.exists()


# spec: ANYM-784
def test_the_undo_note_says_undo_is_another_rotation(host):
    line = host.ok('undo-note')
    assert 'another rotation' in line['note'] and 'never restored' in line['note']


class Scratch:
    """A restore's scratch database: its own project, an internal network, a profile outside production."""

    def __init__(self, host):
        self.host, self.profile = host, host.tmp / 'scratch' / 'service'
        self.profile.mkdir(parents=True)
        Host.write_profile(self.profile)
        state = host.state()
        state['containers'] = {DB: {'running': True, 'image': IMAGE, 'networks': ['scratch_backend'],
                                    'labels': {'com.docker.compose.project': 'scratch', 'com.docker.compose.service': 'calendar-db'}}}
        state['networks'] = {'scratch_backend': True}
        host.state_path.write_text(json.dumps(state))
        self.args = ('--mode', 'isolated', '--container', DB, '--profile-dir', str(self.profile))
        # A mode that read them would find these, and the audit below would name them.
        self.audit = host.tmp / 'opened.json'

    def run(self, command, *extra, **options):
        return self.host.tool(*command, *self.args, *extra, audit_opens=str(self.audit), **options)

    def ok(self, *command, **options):
        code, line, output = self.run(command, **options)
        assert code == 0, output
        return line

    def assert_touched_no_production_path(self):
        assert not self.host.journal_dir.exists() and not self.host.retired.exists()
        assert not (self.host.backup / 'backups' / '.runner.lock').exists()
        assert not (self.host.messaging / '.runner.lock').exists() and not (self.host.global_dir / 'deploy.lock').exists()
        production = (str(self.host.backup), str(self.host.service.parent), str(self.host.global_dir), str(self.host.plan_a),
                      str(self.host.production_json))
        assert not [path for path in json.loads(self.audit.read_text()) if path.startswith(production)]
        assert self.host.owner_values() == {OLD['fcs_owner']} and self.host.role_value('gateway.json') == OLD['fcs_gateway']
        verbs = {entry['argv'][0] for entry in self.host.state()['log']}
        assert not verbs & {'stop', 'start', 'restart', 'kill', 'create'}
        assert not any(entry['argv'][0] == 'exec' and entry['argv'][1] != '-i' for entry in self.host.state()['log'])


@pytest.fixture
def scratch(host):
    return Scratch(host)


# spec: ANYM-784
def test_an_isolated_rotation_rewrites_only_its_profile_and_touches_nothing_of_production(host, scratch):
    retired = host.tmp / 'scratch-retired'
    for which in ('owner', 'roles'):
        code, line, output = scratch.run(('apply', which), '--retired-list', str(retired))
        assert code == 0, output
        assert scratch.ok('verify')['status'] == 'verified'
        assert scratch.ok('finish')['status'] == 'finished'
        scratch.assert_touched_no_production_path()
    env = re.search(r'^POSTGRES_PASSWORD=(.*)$', (scratch.profile / 'postgres.env').read_text(), re.M)[1]
    assert env != OLD['fcs_owner'] and host.accepted('fcs_owner', env) and not host.accepted('fcs_owner', OLD['fcs_owner'])
    runtime = json.loads((scratch.profile / 'runtime.json').read_text())['dsn']
    assert f'fcs_owner:{env}@' in runtime
    gateway = json.loads((scratch.profile / 'gateway.json').read_text())
    assert {key: gateway[key] for key in KEYS_KEPT} == {key: PROFILE[key] for key in KEYS_KEPT}
    assert OLD['fcs_gateway'] not in gateway['dsn'] and not host.accepted('fcs_gateway', OLD['fcs_gateway'])
    assert retired.read_text().split() == [rotate.fingerprint(OLD['fcs_owner'])]
    assert not (host.tmp / 'scratch' / 'service.rotation' / 'staged').exists()


# spec: ANYM-784
def test_an_isolated_rotation_also_recovers_from_a_kill(host, scratch):
    code, _, _ = scratch.run(('apply', 'owner'), kill_at='file:runtime.json')
    assert code == -signal.SIGKILL
    assert scratch.ok('recover')['status'] == 'verifying'
    scratch.ok('verify')
    scratch.ok('finish')
    scratch.assert_touched_no_production_path()
    assert not host.accepted('fcs_owner', OLD['fcs_owner'])


# spec: ANYM-784
@pytest.mark.parametrize('change, code', [
    ('api', 'isolated_project_has_services'), ('worker', 'isolated_project_has_services'),
    ('network', 'isolated_network_not_internal'), ('no_network', 'isolated_network_not_internal'),
    ('neighbour', 'isolated_network_shared'), ('stopped_neighbour', 'isolated_network_shared'),
    ('production_project', 'isolated_target_is_production'),
    ('production_state', 'isolated_profile_is_production'), ('under_production', 'isolated_profile_is_production')])
def test_an_isolated_rotation_refuses_a_target_that_is_not_isolated(host, scratch, change, code):
    state = host.state()
    labels = state['containers'][DB]['labels']
    if change in ('api', 'worker'):
        state['containers'][API] = {'running': False, 'image': IMAGE, 'networks': [], 'labels': {
            **labels, 'com.docker.compose.service': 'calendar-' + change}}
    elif change == 'network':
        state['networks']['scratch_backend'] = False
    elif change == 'no_network':
        state['containers'][DB]['networks'] = []
    elif change in ('neighbour', 'stopped_neighbour'):  # Astra r1 M4: another project on the same internal network
        state['containers'][API] = {'running': change == 'neighbour', 'image': IMAGE, 'networks': ['scratch_backend'], 'labels': {
            'com.docker.compose.project': 'deedkit-calendar', 'com.docker.compose.service': 'calendar-api'}}
    elif change == 'production_project':
        labels['com.docker.compose.project'] = 'deedkit-calendar'
    host.state_path.write_text(json.dumps(state))
    args = list(scratch.args)
    if change == 'production_state':
        args[-1] = str(host.service)
    elif change == 'under_production':
        args[-1] = str(host.backup / 'nested')
        Path(args[-1]).mkdir()
    found, line, _ = host.tool('apply', 'owner', *args)
    assert found == 1 and line['code'] == code
    assert not (scratch.profile.parent / 'service.rotation' / 'rotation.unsettled').exists()
    assert all('ALTER' not in entry['stdin'] for entry in host.state()['log'])


# spec: ANYM-784
def test_an_isolated_rotation_accepts_its_own_helper_on_the_network(host, scratch):
    state = host.state()
    state['containers']['7' * 64] = {'running': False, 'image': IMAGE, 'networks': ['scratch_backend'], 'labels': {rotate.HELPER_LABEL: DB}}
    host.state_path.write_text(json.dumps(state))
    assert scratch.ok('apply', 'owner')['status'] == 'verifying'


# spec: ANYM-784
def test_an_isolated_retired_list_in_production_is_refused_before_anything_is_created(host, scratch):
    """Astra r1 M5, also through a symbolic link."""
    link = host.tmp / 'scratch-list'
    link.symlink_to(host.retired)
    for listed in (host.retired, link):
        code, line, _ = scratch.run(('apply', 'owner'), '--retired-list', str(listed))
        assert code == 1 and line['code'] == 'isolated_retired_list_is_production'
    assert not host.retired.exists() and not (host.tmp / 'scratch' / 'service.rotation').exists()


# spec: ANYM-784
def test_an_isolated_profile_reached_through_a_production_link_keeps_its_journal_out_of_production(host, scratch):
    """Astra r1 M5: the check read the resolved path and the journal used the spelling."""
    link = host.backup / 'scratch-link'
    link.symlink_to(scratch.profile)
    args = ('--mode', 'isolated', '--container', DB, '--profile-dir', str(link))
    code, line, output = host.tool('apply', 'owner', *args)
    assert code == 0, output
    assert not (host.backup / 'scratch-link.rotation').exists()
    assert (host.tmp / 'scratch' / 'service.rotation' / 'rotation.unsettled').exists()


# spec: ANYM-784
def test_a_refused_production_profile_creates_no_journal_directory(host, scratch):
    code, line, _ = host.tool('apply', 'owner', '--mode', 'isolated', '--container', DB, '--profile-dir', str(host.service))
    assert code == 1 and line['code'] == 'isolated_profile_is_production'
    assert not Path(f'{host.service}.rotation').exists()


# spec: ANYM-784
@pytest.mark.parametrize('entry', ['staged', 'journal.json', 'rotation.log'])
def test_a_link_in_the_journal_directory_is_refused_before_anything_is_written(host, scratch, entry):
    """Sol r2 M5: `staged` pointing into production would have carried the copies and the new value there."""
    journal = host.tmp / 'scratch' / 'service.rotation'
    journal.mkdir(mode=0o700)
    (journal / entry).symlink_to(host.service)
    before = sorted(path.name for path in host.service.iterdir())
    code, line, _ = scratch.run(('apply', 'owner'))
    assert code == 1 and line['code'] == 'journal_entry_is_a_link'
    assert sorted(path.name for path in host.service.iterdir()) == before
    assert not (journal / 'rotation.unsettled').exists()
    assert all('ALTER' not in entry['stdin'] for entry in host.state()['log'])


# spec: ANYM-784
def test_a_journal_directory_owned_by_someone_else_or_open_to_others_is_refused(host, scratch):
    journal = host.tmp / 'scratch' / 'service.rotation'
    journal.mkdir()
    journal.chmod(0o755)  # mkdir's mode passes through the process umask, which an imported module may have set
    code, line, _ = scratch.run(('apply', 'owner'))
    assert code == 1 and line['code'] == 'journal_directory_not_private'


# spec: ANYM-784
def test_each_mode_takes_only_its_own_arguments(host, scratch):
    code, line, _ = host.tool('apply', 'owner', '--container', DB)
    assert code == 1 and line['code'] == 'production_mode_takes_no_target'
    code, line, _ = host.tool('apply', 'owner', '--mode', 'isolated', '--container', DB)
    assert code == 1 and line['code'] == 'isolated_needs_container_and_profile_dir'


# spec: ANYM-784
def test_the_tool_refuses_anyone_but_root(host, monkeypatch):
    monkeypatch.setattr(rotate, 'running_as_root', lambda: False)
    assert rotate.main(['plan']) == 1


# spec: ANYM-784
def test_the_runbook_names_every_subcommand_and_the_failure_table():
    text = (ROOT / 'docs' / 'operations' / 'production.md').read_text()
    section = text.split('## Rotate the calendar database passwords', 1)[1].split('\n## ', 1)[0]
    for word in ('plan', 'apply owner', 'apply roles', 'verify', 'finish', 'recover', 'undo-note'):
        assert f'`{word}`' in section or f'calendar_rotate.py {word}' in section, word
    for phrase in ('an exposure', 'a restore', 'a schedule', 'undo is another rotation', 'never restored', 'rotation_running',
                   'rotation_in_progress', '--mode isolated', 'retired-passwords'):
        assert phrase in section, phrase
    for boundary in ('before the `ALTER`', 'after the `ALTER`', 'between files', 'short write', 'before `verify`', 'after `finish`'):
        assert boundary in section, boundary


def docker_ready():
    if not sys.platform.startswith('linux') or not shutil.which('docker'):
        return False
    return subprocess.run(['docker', 'info'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30).returncode == 0


# spec: ANYM-784
def test_in_real_postgresql_the_verifier_logs_in_and_the_replaced_value_is_refused(tmp_path):
    """The whole isolated sequence against the image the calendar runs: the verifier format, the socket ALTER, the
    throwaway probe and its scram-only login, a kill after the ALTER."""
    if not docker_ready():
        pytest.skip('needs Docker on Linux; CI runs it')
    compose = (ROOT / 'deploy' / 'compose.calendar.yaml').read_text()
    image = re.search(r'image: (postgres:\S+)', compose)[1]
    subprocess.run(['docker', 'pull', '-q', image], check=True, stdout=subprocess.DEVNULL, timeout=300)
    suffix = os.urandom(4).hex()
    network, name = f'rotate_net_{suffix}', f'rotate_db_{suffix}'
    subprocess.run(['docker', 'network', 'create', '--internal', network], check=True, stdout=subprocess.DEVNULL, timeout=60)
    try:
        subprocess.run(['docker', 'run', '-d', '--rm', '--name', name, '--network', network, '-e', 'POSTGRES_USER=fcs_owner',
                        '-e', 'POSTGRES_DB=family_calendar', '-e', 'POSTGRES_PASSWORD=' + OLD['fcs_owner'], image],
                       check=True, stdout=subprocess.DEVNULL, timeout=120)
        # Over TCP: the image's first, initializing server listens on its socket only.
        ready = 'until pg_isready -q -U fcs_owner -d family_calendar -h 127.0.0.1; do sleep 1; done'
        subprocess.run(['docker', 'exec', name, 'sh', '-c', ready], check=True, timeout=120)
        roles = ''.join(f"CREATE ROLE {role} LOGIN PASSWORD '{OLD[role]}';" for role in ('fcs_gateway', 'fcs_worker'))
        subprocess.run(['docker', 'exec', '-i', name, 'psql', '-U', 'fcs_owner', '-d', 'family_calendar', '-v', 'ON_ERROR_STOP=1'],
                       input=roles, text=True, check=True, stdout=subprocess.DEVNULL, timeout=60)
        profile = tmp_path / 'scratch' / 'service'
        profile.mkdir(parents=True)
        Host.write_profile(profile)
        driver = tmp_path / 'driver.py'
        driver.write_text(DRIVER)

        def tool(*argv, **options):
            spec = tmp_path / f'spec-{os.urandom(3).hex()}.json'
            spec.write_text(json.dumps({'scripts': str(ROOT / 'scripts'), 'argv': [*argv, '--mode', 'isolated', '--container', name,
                                                                                   '--profile-dir', str(profile)], 'constants': {
                'PRODUCTION_ROOTS': [str(tmp_path / 'production')]}, **options}))
            return subprocess.run([sys.executable, str(driver), str(spec)], capture_output=True, text=True, timeout=180)

        assert json.loads(tool('plan').stdout.splitlines()[-1])['ready'] is True
        assert tool('apply', 'owner', kill_at='before-alter').returncode == -signal.SIGKILL
        recovered = tool('recover')
        assert recovered.returncode == 0, recovered.stdout
        for command in ('verify', 'finish'):
            done = tool(command)
            assert done.returncode == 0, done.stdout
        rolled = tool('apply', 'roles')
        assert rolled.returncode == 0, rolled.stdout
        for command in ('verify', 'finish'):
            done = tool(command)
            assert done.returncode == 0, done.stdout
        env = re.search(r'^POSTGRES_PASSWORD=(.*)$', (profile / 'postgres.env').read_text(), re.M)[1]
        assert env != OLD['fcs_owner']
        target = rotate.Target(tmp_path / 'journal', profile)
        target.attach(name)
        assert target.probe('fcs_owner', env) == 'accepted' and target.probe('fcs_owner', OLD['fcs_owner']) == 'refused'
        assert target.probe('fcs_owner', 'a-random-value') == 'refused'
        # The fence in the real database: an ALTER that expects the stored verifier of the start changes nothing.
        late = rotate.alter_statements({'fcs_owner': verifier(OLD['fcs_owner'])}, {'fcs_owner': verifier('a-late-value')})
        answer = subprocess.run(['docker', 'exec', '-i', name, 'psql', '-X', '-q', '-v', 'ON_ERROR_STOP=1', '-U', 'fcs_owner',
                                 '-d', 'family_calendar', '-f', '-'], input=late, text=True, capture_output=True, timeout=60)
        assert answer.returncode != 0 and 'rotation_fence' in answer.stderr
        assert target.probe('fcs_owner', env) == 'accepted' and target.probe('fcs_owner', 'a-late-value') == 'refused'
        gateway = re.search(r'fcs_gateway:([^@]+)@', (profile / 'gateway.json').read_text())[1]
        assert target.probe('fcs_gateway', gateway) == 'accepted' and target.probe('fcs_gateway', OLD['fcs_gateway']) == 'refused'
    finally:
        subprocess.run(['docker', 'stop', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        subprocess.run(['docker', 'network', 'rm', network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
