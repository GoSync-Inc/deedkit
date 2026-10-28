"""Restore roles and redacted pg_restore detail for the isolated R2 restore helper (synthetic data only)."""
import hashlib
import io
from pathlib import Path
import re
import subprocess

from hypothesis import given, strategies as st
import pytest

from messaging_service import backup_restore_worker as worker

SQL = Path(worker.__file__).parent
NAMED = re.compile(r"\bto\s+((?:[a-z_][a-z0-9_]*\s*,\s*)*[a-z_][a-z0-9_]*)", re.I)


def named_roles(text):
    text = re.sub(r'--[^\n]*', '', text)
    roles = {m.lower() for m in re.findall(r"rolname\s*=\s*'([a-z_][a-z0-9_]*)'", text, re.I)}
    for statement in text.split(';'):
        if re.search(r'\b(grant|create\s+policy|alter\s+policy)\b', statement, re.I):
            for group in NAMED.findall(statement):
                roles |= {r.strip().lower() for r in group.split(',')}
    return roles - {'public'}


# spec: ANYM-285
def test_every_role_a_migration_or_role_file_names_is_a_restore_role():
    files = sorted((SQL / 'migrations').glob('*.sql')) + sorted(SQL.glob('*_role.sql'))
    roles = set().union(*(named_roles(path.read_text()) for path in files))
    assert {'messaging_gateway', 'messaging_subscriptions', 'messaging_event_delivery'} <= roles
    assert roles <= set(worker.RESTORE_ROLES), sorted(roles - set(worker.RESTORE_ROLES))


# spec: ANYM-285
def test_restore_roles_sql_creates_only_fixed_nologin_roles():
    text = worker.restore_roles_sql()
    assert re.findall(r'CREATE ROLE (\w+) NOLOGIN;', text) == list(worker.RESTORE_ROLES)
    assert 'LOGIN;' not in text.replace(' NOLOGIN;', '') and 'PASSWORD' not in text


# spec: ANYM-285
def test_roles_are_created_before_pg_restore_and_its_failure_records_redacted_detail(tmp_path, monkeypatch):
    restored = tmp_path / 'bundle'; restored.mkdir()
    raw = b'{}'; (restored / 'manifest.json').write_bytes(raw)
    monkeypatch.setattr(worker, 'decrypt_snapshot_stream', lambda *a, **kw: restored)
    monkeypatch.setattr(worker.backup, 'verify_bundle', lambda *a: {'schema_version': 50})
    calls = []
    def run(args, **kw):
        calls.append(args[0])
        if args[0] == 'pg_restore':
            kw['stderr'].write(b'pg_restore: error: could not execute query: ERROR:  role "messaging_event_delivery" does not exist\n'
                               b'Command was: CREATE POLICY synthetic_beat ON t TO messaging_event_delivery USING (x = \'secret-value\');\n')
            raise subprocess.CalledProcessError(1, args)
    monkeypatch.setattr(worker.subprocess, 'run', run)
    config = {'v': 1, 'identity_file': '/run/restore/identity', 'work_directory': '/work', 'database_host': 'restore-postgres',
              'database_password': 'a' * 64, 'snapshot_sha256': hashlib.sha256(raw).hexdigest(), 'max_plaintext_bytes': 10}
    with pytest.raises(worker.RestoreStageError, match='restore_database_failed') as caught:
        worker.run(config, io.BytesIO(b''))
    assert calls == ['psql', 'pg_restore']
    assert caught.value.detail == 'role does not exist'


SENTINEL = 'private_row_sentinel'
# A row value as PostgreSQL interpolates it, with quotes, newlines and forged pg_restore lines.
values = st.builds(lambda a, b: a + SENTINEL + b,
                   st.text(alphabet='"\'\n:a_ ', max_size=8),
                   st.sampled_from(['', '"', '\npg_restore: error: could not execute query: ERROR:  role "' + SENTINEL + '" does not exist',
                                    '" does not exist', '\n', '"\nCommand was: x']))
FIRST = st.sampled_from([
    'pg_restore: error: could not execute query: ERROR:  invalid input syntax for type integer: "{}"',
    'pg_restore: error: COPY failed for table "chats": ERROR:  invalid input syntax for type uuid: "{}"',
    'pg_restore: error: could not execute query: ERROR:  duplicate key value {}',
    'pg_restore: error: could not execute query: ERROR:  role "{}" does not exist',  # a regrole value in INSERT data
    'pg_restore: error: {}'])


# spec: ANYM-285
@given(template=FIRST, value=values)
def test_restore_detail_is_a_fixed_template_that_never_keeps_a_row_value(template, value):
    from messaging_service import backup_r2_verifier as v
    raw = ('pg_restore: while PROCESSING TOC:\n' + template.format(value) + '\nDETAIL:  Key (id)=(' + value + ')\n').encode()
    detail = worker.restore_detail(raw)
    assert SENTINEL not in detail and v.DETAIL_SHAPE.fullmatch(detail)
    assert detail in ('unclassified pg_restore error', 'COPY failed', 'role does not exist')


# spec: ANYM-285
def test_restore_detail_keeps_only_the_object_name_of_a_known_template():
    error = 'pg_restore: error: could not execute query: ERROR:  '
    assert worker.restore_detail((error + 'role "messaging_gateway" does not exist\nCommand was: CREATE POLICY x\n').encode()) \
        == 'role does not exist'
    assert worker.restore_detail((error + 'relation "public.chats" already exists\n').encode()) == 'relation already exists'
    assert worker.restore_detail((error + 'extension "vector" is not available\n').encode()) == 'extension is not available'
    assert worker.restore_detail((error + 'out of memory\nDETAIL:  Failed on request of size 8192.\n').encode()) == 'out of memory'
    assert worker.restore_detail((error + 'role "Private Name" does not exist\n').encode()) == 'role does not exist'
    assert worker.restore_detail((error + 'invalid input syntax for type integer: "1"\n').encode()) == 'unclassified pg_restore error'
    assert worker.restore_detail(b'pg_restore: warning: synthetic\n') == ''


# spec: ANYM-285
def test_verifier_keeps_a_shaped_helper_detail_beside_the_unchanged_failure_fact(tmp_path, monkeypatch):
    from messaging_service import backup_r2_receiver as r2
    from messaging_service import backup_r2_verifier as v
    engine = v.Docker()
    monkeypatch.setattr(engine, 'setup', lambda *args: None)
    monkeypatch.setattr(engine, 'inspect_owned', lambda *args: {})
    monkeypatch.setattr(v, 'validate_helper_layout', lambda *args: None)
    class Process:
        stdin = io.BytesIO()
        def wait(self, timeout): return 1
        def poll(self): return 1
    monkeypatch.setattr(v.subprocess, 'Popen', lambda *a, **kw: Process())
    monkeypatch.setattr(r2, 'verify_object', lambda *a: None)
    fact = {'v': 1, 'status': 'failed', 'error': 'restore_database_failed'}
    for detail, expected in (({'v': 1, 'detail': 'role does not exist'}, 'role does not exist'),
                             ({'v': 1, 'detail': 'role "' + SENTINEL + '" does not exist'}, None),
                             ({'v': 1, 'detail': 'invalid input syntax for type integer: "' + SENTINEL + '"'}, None),
                             ({'v': 1, 'detail': 'out of memory', 'extra': 1}, None), (None, None)):
        files = {'failure.json': r2.encoded(fact)}
        if detail is not None: files['failure-detail.json'] = r2.encoded(detail)
        def private(path, limit, files=files):
            if path.name not in files: raise FileNotFoundError(path.name)
            return files[path.name]
        monkeypatch.setattr(v, 'private_file', private)
        with pytest.raises(v.HelperFailure, match='restore_database_failed') as caught:
            engine.restore({}, {}, None, {}, 'a' * 32, tmp_path, tmp_path)
        assert caught.value.detail == expected


# spec: ANYM-606
def test_every_role_a_calendar_schema_or_role_file_names_is_a_calendar_restore_role():
    calendar = Path(__file__).resolve().parents[1] / 'services/calendar/family_calendar/service'
    files = sorted(calendar.glob('*.sql'))
    assert {'roles.sql', 'schema.sql'} <= {path.name for path in files}
    roles = set().union(*(named_roles(path.read_text()) for path in files))
    assert {'fcs_gateway', 'fcs_worker'} <= roles
    assert roles <= set(worker.CALENDAR_RESTORE_ROLES), sorted(roles - set(worker.CALENDAR_RESTORE_ROLES))
    text = worker.restore_roles_sql(worker.CALENDAR_RESTORE_ROLES)
    assert re.findall(r'CREATE ROLE (\w+) NOLOGIN;', text) == list(worker.CALENDAR_RESTORE_ROLES)
