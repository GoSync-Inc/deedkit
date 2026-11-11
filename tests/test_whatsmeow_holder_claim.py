"""ANYM-937: the WhatsApp session holder's Go claim caller runs ANYM-914's statements under its ANYM-936 login.

The text check compares whatsapp/whatsmeow/claim.go with session_claim.py, placeholders converted; the database
checks run the real `anymsg-whatsmeow --serve` binary and the Python caller against one database. No provider: the
holder makes no WhatsApp contact before a collector attaches with a fresh policy, and none attaches with one here.
"""
import json
import os
import re
import select
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from messaging_service import session_claim as sc
from messaging_service.archive import Archive
from test_session_holder_role import OTHER, RolePool, holders  # noqa: F401  (fixture)
from test_whatsmeow_contract import native_fixture_binary  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[1]
PAIRS = {'claimAcquireSQL': 'ACQUIRE_SQL', 'claimRefreshSQL': 'REFRESH_SQL', 'claimReleaseSQL': 'RELEASE_SQL',
         'claimWaitingSQL': 'WAITING_SQL', 'claimRowSQL': 'ROW_SQL'}
ACCOUNT = OTHER  # whatsapp-demo-1


def go_constants():
    text = ROOT.joinpath('whatsapp', 'whatsmeow', 'claim.go').read_text()
    return dict(re.findall(r'const (\w+) = `([^`]*)`', text))


def converted(statement):
    """psycopg placeholders as numbered PostgreSQL ones: a name keeps its first number, each %s takes the next."""
    names = {}

    def name(match):
        names.setdefault(match.group(1), len(names) + 1)
        return f'${names[match.group(1)]}'
    if '%(' in statement:
        return re.sub(r'%\((\w+)\)s', name, statement)
    count = iter(range(1, 100))
    return re.sub(r'%s', lambda _: f'${next(count)}', statement)


def normal(statement):
    return ' '.join(statement.split())


# spec: ANYM-937
def test_the_go_claim_statements_are_session_claims_constants_with_only_placeholders_converted():
    constants = go_constants()
    assert set(PAIRS) <= set(constants)
    for go_name, python_name in PAIRS.items():
        assert normal(constants[go_name]) == normal(converted(getattr(sc, python_name))), go_name
    # Never a .sql file: that path would be a hand-applied change to the release planner (SQL_INVENTORY).
    assert not list(ROOT.joinpath('whatsapp', 'whatsmeow').glob('*.sql'))


class Holder:
    """One `anymsg-whatsmeow --serve` process with its private directories and stderr events."""

    def __init__(self, binary, root, dsn, *, locks=None, boot='boot-one'):
        self.root = root
        root.mkdir(mode=0o700, exist_ok=True)
        for name in ('session', 'events', 'pages'):
            (root / name).mkdir(mode=0o700, exist_ok=True)
        self.locks = locks or root / 'session-locks'
        self.locks.mkdir(mode=0o700, exist_ok=True)
        # A unix socket path has a 104-byte limit on macOS; the pytest directory can be longer.
        self.sockets = Path(tempfile.mkdtemp(prefix='hs'))
        (root / 'boot_id').write_text(boot + '\n')
        dsn_file = root / 'holder.dsn'
        dsn_file.write_text(dsn + '\n')
        dsn_file.chmod(0o600)
        config = root / 'config.json'
        config.write_text(json.dumps({'account': ACCOUNT, 'expected_wid': '12345@c.us',
            'session_directory': str(root / 'session'), 'events_directory': str(root / 'events'),
            'pages_directory': str(root / 'pages'), 'session_lock_directory': str(self.locks),
            'holder_dsn_file': str(dsn_file), 'boot_id_path': str(root / 'boot_id')}))
        config.chmod(0o600)
        self.process = subprocess.Popen([str(binary), '--serve', str(self.sockets), '--config', str(config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, bufsize=0)
        assert self.process.stderr is not None
        self.err = self.process.stderr
        self.events = []

    def wait_for(self, event, seconds=30, **fields):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for seen in self.events:
                if seen.get('event') == event and all(seen.get(k) == v for k, v in fields.items()):
                    return seen
            ready, _, _ = select.select([self.err], [], [], 0.2)
            if ready:
                line = self.err.readline()
                if line:
                    self.events.append(json.loads(line))
                elif self.process.poll() is not None:
                    break
        raise AssertionError(f'{event} not seen: {self.events}')

    def stop(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        code = self.process.wait(timeout=25)  # inside the holder service's stop_grace_period
        for line in self.err.read().splitlines():
            self.events.append(json.loads(line))
        for name in ('control.sock', 'send.sock', 'token'):
            self.sockets.joinpath(name).unlink(missing_ok=True)
        self.sockets.rmdir()
        return code

    def wait_for_sockets(self, seconds=30):
        """The holder logs holder.claimed before it writes its token and opens the control socket (serve.go), so a
        collector that attaches on that event can arrive first: wait for both, as a real collector retries."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.sockets.joinpath('token').exists() and self.sockets.joinpath('control.sock').exists():
                return
            assert self.process.poll() is None, f'the holder exited: {self.process.returncode}'
            time.sleep(0.05)
        raise AssertionError('the holder opened no control socket')

    def attach(self, owner, epoch):
        self.wait_for_sockets()
        token = self.sockets.joinpath('token').read_text().strip()
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(10)
            s.connect(str(self.sockets / 'control.sock'))
            stream = s.makefile('rwb')
            hello = json.loads(stream.readline())['hello']
            stream.write(json.dumps({'hello': {'protocol': 1, 'supported': [1, 1]}, 'token': token,
                'lane': 'control', 'lease': {'owner': owner, 'epoch': epoch}}).encode() + b'\n')
            stream.flush()
            return hello, json.loads(stream.readline())


def holder_dsn(store, role, password):
    with store.pool.connection() as c:
        params = conninfo_to_dict(c.info.dsn)
    # Only the connection keys: pgx sends any other key (psycopg's libpq-only ones) as a server runtime parameter.
    kept = {k: params[k] for k in ('host', 'port', 'dbname') if params.get(k)}
    return make_conninfo(**kept, user=role, password=password)


@pytest.fixture()
def holder_login(holders):  # noqa: F811
    store, make = holders
    password = 'synthetic-' + uuid4().hex
    role = make(ACCOUNT, password=password)
    with store.pool.connection() as c:
        c.execute('DELETE FROM session_claims')
        c.execute('DELETE FROM collector_leases')
    dsn = holder_dsn(store, role, password)
    import psycopg
    try:
        psycopg.connect(dsn).close()
    except psycopg.OperationalError as exc:  # a cluster whose pg_hba refuses password logins for new roles
        pytest.skip(f'password login unavailable on this test cluster: {type(exc).__name__}')
    return store, role, dsn


def claim_row(store):
    with store.pool.connection() as c:
        return c.execute(sc.ROW_SQL, (ACCOUNT,)).fetchone()


# spec: ANYM-937
def test_the_go_holder_and_the_python_caller_share_one_claim_under_the_holder_login(holder_login, native_fixture_binary,  # noqa: F811
                                                                                     tmp_path):
    store, role, dsn = holder_login
    holder = Holder(native_fixture_binary, tmp_path / 'one', dsn)
    try:
        claimed = holder.wait_for('holder.claimed')
        row = claim_row(store)
        assert row['holder_kind'] == 'holder' and row['generation'] == claimed['claim_generation'] == 1
        assert row['host_id'] == (holder.locks / 'host_id').read_text().strip() and row['boot_id'] == 'boot-one'
        info = os.stat(holder.locks / f'{ACCOUNT}.lock')
        assert row['lock_ref'] == f'{info.st_dev}:{info.st_ino}'  # the Python lock domain, byte for byte
        # A child-mode collector (and whatsapp_pair) takes the canonical lock first: refused while the holder runs.
        with pytest.raises(RuntimeError, match='session_already_owned'):
            with sc.host_lock(ACCOUNT, holder.locks, boot_id_path=tmp_path / 'one' / 'boot_id'):
                pass
        # Another host's Python opener is refused by the Go holder's row.
        other = tmp_path / 'other-locks'
        other.mkdir(mode=0o700)
        (tmp_path / 'boot-two').write_text('boot-two\n')
        with sc.host_lock(ACCOUNT, other, boot_id_path=tmp_path / 'boot-two') as lock:
            with pytest.raises(sc.ClaimRefused, match='session_claimed_elsewhere'):
                sc.acquire(store.pool, lock, 'collector')
        # The lease check runs under the holder login's row security: a live lease attaches, a forged one does not.
        archive = Archive(store)
        epoch = archive.acquire(ACCOUNT, 'collector-one')
        hello, reply = holder.attach('collector-one', epoch)
        assert hello['account'] == ACCOUNT and hello['protocol'] == 1 and 'send_text' in hello['methods']
        assert reply['attached']['account'] == ACCOUNT and reply['attached']['connected'] is False
        assert holder.attach('forged', epoch)[1] == {'error': 'collector_lease_invalid'}
        archive.release(ACCOUNT, 'collector-one', epoch)
    finally:
        assert holder.stop() == 0
    # The holder released its claim within the grace, after its (absent) provider connection ended.
    row = claim_row(store)
    assert row['released_by'] == 'holder' and row['released_at'] is not None
    # Python takes it next, then a Go holder after Python's release: one generation sequence.
    with sc.host_lock(ACCOUNT, holder.locks, boot_id_path=tmp_path / 'one' / 'boot_id') as lock:
        claim = sc.acquire(RolePool(store, role), lock, 'holder')
        assert claim.generation == 2
        assert sc.release(RolePool(store, role), claim)
    again = Holder(native_fixture_binary, tmp_path / 'one', dsn, locks=holder.locks)
    try:
        assert again.wait_for('holder.claimed')['claim_generation'] == 3
    finally:
        assert again.stop() == 0


# spec: ANYM-937
def test_an_attach_that_arrives_before_the_holder_has_opened_its_sockets_waits_for_them(
        holder_login, native_fixture_binary, tmp_path):  # noqa: F811
    store, _, dsn = holder_login
    holder = Holder(native_fixture_binary, tmp_path / 'one', dsn)
    try:
        archive = Archive(store)
        epoch = archive.acquire(ACCOUNT, 'collector-one')
        # Straight after the process starts, before holder.claimed: the token and the socket do not exist yet.
        hello, reply = holder.attach('collector-one', epoch)
        assert hello['account'] == ACCOUNT and reply['attached']['account'] == ACCOUNT
        archive.release(ACCOUNT, 'collector-one', epoch)
    finally:
        assert holder.stop() == 0


# spec: ANYM-937
def test_a_refused_or_failed_acquire_keeps_the_holder_up_without_a_claim(holder_login, native_fixture_binary,  # noqa: F811
                                                                         tmp_path):
    store, role, dsn = holder_login
    # A restored backup from before the claim, migrated to the claim schema: no row and restored_disabled.
    with store.pool.connection() as c:
        c.execute("UPDATE source_status SET status='restored_disabled' WHERE account=%s", (ACCOUNT,))
    holder = Holder(native_fixture_binary, tmp_path / 'restored', dsn)
    try:
        holder.wait_for('session_claim.waiting', code='session_claim_restore_hold')
        time.sleep(1)
        assert holder.process.poll() is None  # waiting in-process, never a Compose exit loop
        assert claim_row(store) is None
        assert not (holder.sockets / 'control.sock').exists()  # nothing served before the claim
        assert not (tmp_path / 'restored' / 'session' / 'auth.sqlite').exists()  # the session store stays closed
    finally:
        assert holder.stop() == 0
    # Another host's live claim: refused with one waiting mark.
    with store.pool.connection() as c:
        c.execute("UPDATE source_status SET status='not_started' WHERE account=%s", (ACCOUNT,))
        c.execute('''INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,
            claimed_at,seen_at) VALUES(%s,%s,'boot-x','9:9',%s,'collector',4,clock_timestamp(),clock_timestamp())''',
            (ACCOUNT, 'a' * 32, 'b' * 32))
    holder = Holder(native_fixture_binary, tmp_path / 'elsewhere', dsn)
    try:
        holder.wait_for('session_claim.waiting', code='session_claimed_elsewhere')
        row = claim_row(store)
        assert row['generation'] == 4 and row['waiting_host_id'] == (holder.locks / 'host_id').read_text().strip()
        assert holder.process.poll() is None
    finally:
        assert holder.stop() == 0
    # An unreachable database: the same wait.
    params = conninfo_to_dict(dsn)
    params.update(host='127.0.0.1', port='1')
    holder = Holder(native_fixture_binary, tmp_path / 'down', make_conninfo(**params))
    try:
        holder.wait_for('session_claim.waiting', code='session_claim_unavailable')
        assert holder.process.poll() is None
    finally:
        assert holder.stop() == 0


# spec: ANYM-937
def test_the_go_holder_waits_on_a_restored_pre_claim_backup_migrated_to_the_claim_schema(database, native_fixture_binary,  # noqa: F811
                                                                                         tmp_path):
    """A real pg_dump/pg_restore of a schema-77 (pre-claim) database, quarantined and migrated: the Go holder, under
    its own login on the restored database, refuses with session_claim_restore_hold and writes no claim."""
    from messaging_service import backup
    from test_session_holder_role import drop, group, login
    from test_subscription_retention import restore_database, snapshot_restored
    group(database)
    with restore_database(database, tmp_path / 'source', through=77) as source:
        source.seed()
        with source.pool.connection() as c:  # the account's status row, as every collected account has
            c.execute('INSERT INTO source_status(account) VALUES(%s) ON CONFLICT DO NOTHING', (ACCOUNT,))
        with snapshot_restored(source, tmp_path) as restored:
            backup.quarantine_restore(restored)
            restored.migrate()
            group(restored)
            password = 'synthetic-' + uuid4().hex
            role = login(restored, ACCOUNT, password=password)
            try:
                holder = Holder(native_fixture_binary, tmp_path / 'restored-holder', holder_dsn(restored, role, password))
                try:
                    holder.wait_for('session_claim.waiting', code='session_claim_restore_hold')
                    assert holder.process.poll() is None
                    with restored.pool.connection() as c:
                        assert c.execute('SELECT count(*) AS n FROM session_claims').fetchone()['n'] == 0
                finally:
                    assert holder.stop() == 0
            finally:
                drop(restored, role)
