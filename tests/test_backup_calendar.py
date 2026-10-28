"""Family calendar capture: snapshot, lossless catalog manifest, vault and profile state (ANYM-605)."""
import base64
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import uuid

from hypothesis import given, settings, strategies as st
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
import pytest

from messaging_service import backup_calendar, backup_worker
from messaging_service.backup_calendar import (CalendarCaptureRefused, calendar_manifest, capture_state,
    create_calendar_snapshot, fernet_hmac_valid, rows_digest, verify_calendar_bundle)

PROFILE = {'postgres.env': b'POSTGRES_PASSWORD=synthetic\n', 'runtime.json': b'{"db_port":13231}\n',
           'gateway.json': b'{"dsn":"synthetic","bind":"0.0.0.0","allowed_hosts":["calendar-api:13232"]}\n',
           'worker.json': b'{"dsn":"synthetic"}\n'}
SCHEMA = '''CREATE SCHEMA fcs;
CREATE TABLE fcs.migrations(version int PRIMARY KEY, checksum text NOT NULL);
INSERT INTO fcs.migrations SELECT v,'synthetic-'||v FROM generate_series(1,4) v;
CREATE TABLE fcs.connections(id text PRIMARY KEY, provider text NOT NULL, enabled boolean NOT NULL DEFAULT true);
CREATE TYPE fcs.mood AS ENUM ('calm','busy');
CREATE DOMAIN fcs.label AS text CHECK (VALUE <> '');
CREATE TABLE fcs.notes(body json, amount numeric, ratio float8, at timestamptz, span interval, raw bytea,
    mood fcs.mood, label fcs.label, tags text[]);
INSERT INTO fcs.notes VALUES ('{"b": 1,  "a": [1, 2]}',1.10,0.1,'2026-10-28 12:00:00+03','1 day 02:00','\\x00ff',
    'calm','home','{a,b}'),
  ('{"b": 1,  "a": [1, 2]}',1.10,0.1,'2026-10-28 12:00:00+03','1 day 02:00','\\x00ff','calm','home','{a,b}'),
  (NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL),
  ('[]',-0.000000001,1e-300,'infinity','-3 months','\\x','busy','x','{}');
CREATE TABLE public.extra(id int PRIMARY KEY, note text);
'''


def fernet_key(seed):
    return base64.urlsafe_b64encode(hashlib.sha256(seed).digest())


def fernet_token(key, body):
    # A Fernet-shaped token: only its HMAC is checked at capture, so the ciphertext is synthetic.
    signing = base64.urlsafe_b64decode(key)[:16]
    data = b'\x80' + (1_800_000_000).to_bytes(8, 'big') + bytes(16) + hashlib.sha256(body).digest()
    return base64.urlsafe_b64encode(data + hmac.new(signing, data, 'sha256').digest())


def write_state(root, tokens, *, key=b'calendar-key'):
    vault = root / 'vault'; profile = root / 'profile'
    vault.mkdir(mode=0o700, parents=True); profile.mkdir(mode=0o700)
    (vault / 'key').write_bytes(fernet_key(key))
    for name, value in tokens.items():
        (vault / (name + '.enc')).write_bytes(fernet_token(fernet_key(key), value))
    for name, value in PROFILE.items():
        (profile / name).write_bytes(value)
    return vault, profile


# spec: ANYM-605
@settings(max_examples=200)
@given(st.lists(st.text(st.characters(blacklist_categories=['Cs']), max_size=20), max_size=12), st.randoms(use_true_random=False))
def test_row_digest_is_an_order_free_multiset_hash(rows, random):
    shuffled = list(rows); random.shuffle(shuffled)
    assert rows_digest(shuffled) == rows_digest(rows)
    for row in set(rows) | {'x'}:
        # A duplicated row is a different multiset.
        assert rows_digest([*rows, row]) != rows_digest(rows)


# spec: ANYM-605
@settings(max_examples=200)
@given(st.binary(max_size=64), st.data())
def test_fernet_hmac_accepts_only_the_committed_token_under_its_key(body, data):
    key = fernet_key(b'one'); token = fernet_token(key, body)
    assert fernet_hmac_valid(key, token)
    assert not fernet_hmac_valid(fernet_key(b'two'), token)
    raw = bytearray(base64.urlsafe_b64decode(token))
    index = data.draw(st.integers(0, len(raw) - 1)); raw[index] ^= data.draw(st.integers(1, 255))
    assert not fernet_hmac_valid(key, base64.urlsafe_b64encode(bytes(raw)))
    assert not fernet_hmac_valid(key, b'not base64 !')


# spec: ANYM-605
def test_state_copy_skips_staging_and_reads_the_committed_token_through_a_racing_replace(tmp_path, monkeypatch):
    vault, profile = write_state(tmp_path / 'src', {'home': b'old'})
    (vault / '.tmp-paused').write_bytes(b'half-written refresh')
    committed = (vault / 'home.enc').read_bytes()
    real_open = os.open

    def racing_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if path == 'home.enc':
            # The writer's atomic replace lands after the copy opened its descriptor.
            staged = vault / '.tmp-race'; staged.write_bytes(fernet_token(fernet_key(b'calendar-key'), b'new'))
            os.replace(staged, vault / 'home.enc')
        return fd
    monkeypatch.setattr(backup_calendar.os, 'open', racing_open)
    state = capture_state(vault, profile, tmp_path / 'out', {'home': True})
    monkeypatch.undo()
    assert (tmp_path / 'out/vault/home.enc').read_bytes() == committed
    assert state['vault_staging_skipped'] == 1 and state['vault_warnings'] == []
    assert set(state['state_files']) == {'vault/key', 'vault/home.enc', *('profile/' + n for n in PROFILE)}
    assert state['state_files']['vault/home.enc']['sha256'] == hashlib.sha256(committed).hexdigest()
    assert not any(p.name.startswith('.tmp-') for p in (tmp_path / 'out/vault').iterdir())


# spec: ANYM-605
def test_state_gate_refuses_a_missing_enabled_token_and_warns_for_disabled_and_stray(tmp_path):
    vault, profile = write_state(tmp_path / 'src', {'home': b'a'})
    with pytest.raises(CalendarCaptureRefused) as refused:
        capture_state(vault, profile, tmp_path / 'one', {'home': True, 'work': True})
    assert (refused.value.code, refused.value.subject) == ('calendar_vault_incomplete', 'work')
    assert not (tmp_path / 'one').exists()
    (vault / 'notes.txt').write_bytes(b'stray')
    state = capture_state(vault, profile, tmp_path / 'two', {'home': True, 'work': False})
    assert state['vault_warnings'] == [{'kind': 'disabled_connection_without_token', 'name': 'work'},
                                       {'kind': 'unknown_entry', 'name': 'notes.txt'}]
    assert not (tmp_path / 'two/vault/notes.txt').exists()


# spec: ANYM-605
def test_state_gate_refuses_a_token_under_another_key_and_a_symlinked_entry(tmp_path):
    vault, profile = write_state(tmp_path / 'src', {'home': b'a'})
    (vault / 'work.enc').write_bytes(fernet_token(fernet_key(b'other-key'), b'b'))
    with pytest.raises(CalendarCaptureRefused) as refused:
        capture_state(vault, profile, tmp_path / 'one', {'home': True, 'work': True})
    assert (refused.value.code, refused.value.subject) == ('calendar_vault_key_mismatch', 'work.enc')
    (vault / 'work.enc').unlink(); (tmp_path / 'outside').write_bytes((vault / 'home.enc').read_bytes())
    (vault / 'work.enc').symlink_to(tmp_path / 'outside')
    with pytest.raises(CalendarCaptureRefused) as refused:
        capture_state(vault, profile, tmp_path / 'two', {'home': True})
    assert (refused.value.code, refused.value.subject) == ('calendar_state_unsafe', 'work.enc')
    (vault / 'work.enc').unlink(); (profile / 'worker.json').unlink()
    (profile / 'worker.json').symlink_to(tmp_path / 'outside')
    with pytest.raises(CalendarCaptureRefused, match='calendar_state_unsafe'):
        capture_state(vault, profile, tmp_path / 'three', {'home': True})


# spec: ANYM-605
@pytest.mark.parametrize('shape', ['no_vault', 'key_only'])
def test_empty_calendar_state_builds(tmp_path, shape):
    vault, profile = write_state(tmp_path / 'src', {})
    if shape == 'no_vault': shutil.rmtree(vault)
    state = capture_state(vault, profile, tmp_path / 'out', {})
    expected = {'profile/' + n for n in PROFILE} | (set() if shape == 'no_vault' else {'vault/key'})
    assert set(state['state_files']) == expected and state['vault_warnings'] == []


@pytest.fixture
def calendar_db(database):
    """A disposable synthetic calendar database (schema 1-4) beside the disposable messaging test database."""
    base = database.pool.conninfo
    assert conninfo_to_dict(base)['dbname'] == 'messaging_probe_test'
    names = []

    def create(schema=SCHEMA):
        name = 'fcs_' + uuid.uuid4().hex[:12] + '_restore_test'
        with psycopg.connect(base, autocommit=True) as c:
            c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
        names.append(name)
        dsn = make_conninfo(base, dbname=name)
        if schema:
            with psycopg.connect(dsn, autocommit=True) as c: c.execute(schema)
        return dsn
    yield create
    with psycopg.connect(base, autocommit=True) as c:
        for name in names: c.execute(sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(name)))


def manifest_of(dsn, **options):
    extra = ' '.join('-c %s=%s' % (k, v.replace(' ', '\\ ')) for k, v in options.items())
    with psycopg.connect(dsn, row_factory=dict_row, options=extra) as c:
        return calendar_manifest(c)


def pg_tools():
    container = os.environ.get('ANYMSG_TEST_POSTGRES_CONTAINER')
    if not container:
        if os.environ.get('CI'): pytest.fail('CI must provide the isolated PostgreSQL service for the calendar dump')
        pytest.skip('isolated PostgreSQL container unavailable locally')
    return container


def docker_tool(container, dsn, tool):
    values = conninfo_to_dict(dsn)
    inner = make_conninfo(host='127.0.0.1', port=5432, user=values['user'], dbname=values['dbname'])
    return ['docker', 'exec', '-i', '-e', 'PGPASSWORD=' + values['password'], container, tool, '--dbname=' + inner]


# spec: ANYM-605
def test_manifest_is_independent_of_session_settings_and_covers_every_table(calendar_db):
    dsn = calendar_db()
    tables, warnings = manifest_of(dsn)
    assert tables == manifest_of(dsn, TimeZone='Asia/Tokyo', DateStyle='SQL, DMY', IntervalStyle='sql_standard',
                                 extra_float_digits='0', search_path='fcs', bytea_output='escape')[0]
    assert set(tables) == {'fcs.migrations', 'fcs.connections', 'fcs.notes', 'public.extra'}
    assert tables['fcs.notes']['rows'] == 4 and tables['fcs.migrations']['rows'] == 4
    # Enum, domain, arrays, json and built-in base types are the tested set.
    assert warnings == []
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute('CREATE TYPE fcs.pair AS (a int, b text); CREATE TABLE fcs.later(p fcs.pair); '
                  "INSERT INTO fcs.later VALUES (ROW(1,'x'))")
    after, warnings = manifest_of(dsn)
    assert after['fcs.later']['rows'] == 1 and {k: after[k] for k in tables} == tables
    assert warnings == [{'table': 'fcs.later', 'column': 'p', 'type': 'fcs.pair'}]
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("UPDATE fcs.notes SET ratio=0.30000000000000004 WHERE ratio=0.1 AND ctid=(SELECT min(ctid) FROM fcs.notes WHERE ratio=0.1)")
    changed, _ = manifest_of(dsn)
    assert changed['fcs.notes']['rows'] == 4 and changed['fcs.notes']['sha256'] != tables['fcs.notes']['sha256']


# spec: ANYM-605
def test_capture_is_one_snapshot_and_its_dump_restores_to_an_equal_manifest(calendar_db, tmp_path):
    container = pg_tools()
    dsn = calendar_db()
    vault, profile = write_state(tmp_path / 'src', {'home': b'a'})
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("INSERT INTO fcs.connections VALUES ('home','google',true),('old','icloud',false)")

    def commit_after_export():
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute("INSERT INTO public.extra VALUES (99,'after the snapshot')")
            c.execute("INSERT INTO fcs.connections VALUES ('late','google',true)")
    summary = create_calendar_snapshot(dsn, tmp_path / 'snapshot', docker_tool(container, dsn, 'pg_dump'),
        vault_directory=vault, profile_directory=profile, exported_callback=commit_after_export)
    assert summary['status'] == 'snapshot_created'
    manifest = verify_calendar_bundle(tmp_path / 'snapshot')
    assert manifest['profile'] == 'calendar' and manifest['schema_version'] == 4
    assert manifest['restore_mode'] == 'verify_only_no_provider_calls'
    assert manifest['tables']['public.extra']['rows'] == 0
    # 'late' committed after the export: neither its row nor a missing token reaches the capture.
    assert manifest['tables']['fcs.connections']['rows'] == 2
    assert manifest['vault_warnings'] == [{'kind': 'disabled_connection_without_token', 'name': 'old'}]
    restored = calendar_db(schema=None)
    subprocess.run([*docker_tool(container, restored, 'pg_restore'), '--exit-on-error', '--no-owner', '--no-acl'],
        input=(tmp_path / 'snapshot/database.dump').read_bytes(), check=True, capture_output=True)
    assert manifest_of(restored) == (manifest['tables'], manifest['type_warnings'])
    with psycopg.connect(restored) as c:
        assert c.execute('SELECT count(*) FROM public.extra').fetchone()[0] == 0


# spec: ANYM-605
def test_worker_calendar_profile_builds_an_encrypted_bundle_and_records_a_refusal(calendar_db, tmp_path, monkeypatch):
    container = pg_tools()
    if not shutil.which('age') or not shutil.which('age-keygen'):
        if os.environ.get('CI'): pytest.fail('CI must provide age')
        pytest.skip('age unavailable locally')
    dsn = calendar_db()
    vault, profile = write_state(tmp_path / 'src', {})
    (tmp_path / 'dsn').write_text(dsn)
    subprocess.run(['age-keygen', '-o', str(tmp_path / 'identity')], check=True, capture_output=True)
    (tmp_path / 'recipient').write_text(subprocess.run(['age-keygen', '-y', str(tmp_path / 'identity')],
        check=True, capture_output=True, text=True).stdout)
    real = backup_worker.dump_credentials
    monkeypatch.setattr(backup_worker, 'dump_credentials',
        lambda value, directory: (docker_tool(container, value, 'pg_dump'), real(value, directory)[1]))
    work = tmp_path / 'work'; work.mkdir(mode=0o700)
    config = {'profile': 'calendar', 'id': '20261028T120000Z-' + uuid.uuid4().hex[:16], 'dsn_file': str(tmp_path / 'dsn'),
              'vault_directory': str(vault), 'profile_directory': str(profile),
              'recipient_file': str(tmp_path / 'recipient'), 'work_directory': str(work)}
    with pytest.raises(ValueError, match='invalid_backup_worker_config'):
        backup_worker.run({**config, 'mode': 'stream_v2'})
    result = backup_worker.run(config)
    assert result['status'] == 'encrypted' and result['profile'] == 'calendar'
    assert json.loads((work / 'result.json').read_text())['sha256'] == result['sha256']
    from messaging_service.backup_crypto import decrypt_snapshot
    decrypt_snapshot(work / 'bundle.tar.age', tmp_path / 'identity', tmp_path / 'restored')
    assert verify_calendar_bundle(tmp_path / 'restored')['schema_version'] == 4
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("INSERT INTO fcs.connections VALUES ('home','google',true)")
    second = tmp_path / 'second'; second.mkdir(mode=0o700)
    with pytest.raises(CalendarCaptureRefused):
        backup_worker.run({**config, 'work_directory': str(second)})
    assert json.loads((second / 'refused.json').read_text()) == {
        'id': config['id'], 'status': 'refused', 'code': 'calendar_vault_incomplete', 'subject': 'home'}
    assert not (second / 'bundle.tar.age').exists() and not (second / 'snapshot').exists()
