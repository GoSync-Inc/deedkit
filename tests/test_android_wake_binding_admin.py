"""DK-53 operator path for one Android wake binding (ANYM-997): exact read, default-paused enrollment. Disposable DB only."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import sys
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
import pytest

import messaging_service.__main__ as cli

URL = 'https://example.com/canary-wake-path'
SECRET = 'whsec_' + base64.b64encode(b'x' * 32).decode()
WORDS = 'canary owner words'


def operate(store, tmp_path, monkeypatch, capsys, *argv):
    class BorrowedStore:
        def __getattr__(self, name):
            return getattr(store, name)

        def close(self):
            pass

    dsn = tmp_path / 'test.dsn'
    dsn.write_text(store.pool.conninfo)
    dsn.chmod(0o600)
    monkeypatch.setattr(cli, 'Store', lambda *_: BorrowedStore())
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(sys, 'argv', ['anymsg', *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exit:
        code = exit.code
    out = capsys.readouterr().out
    return code, (json.loads(out) if out.strip() else None), out


def agent(store, principal, permissions=('read', 'delivery-write')):
    store.issue_key(principal, ['telegram-demo'], permissions=list(permissions), key_permissions=list(permissions))
    with store.pool.connection() as c:
        key = c.execute('SELECT id FROM api_keys WHERE principal=%s ORDER BY expires_at DESC LIMIT 1', (principal,)).fetchone()['id']
        endpoint = uuid4()
        c.execute('''INSERT INTO delivery_endpoints(id,principal,key_id,request_id,request_hash,url,host,signing_secret,state)
            VALUES(%s,%s,%s,'synthetic-request',%s,%s,'example.com',%s,'enabled')''',
            (endpoint, principal, key, 'a' * 64, URL, SECRET))
    return str(key), str(endpoint)


@pytest.fixture
def world(seeded):
    store = seeded[0]
    key, endpoint = agent(store, 'android-owner')
    other_key, other_endpoint = agent(store, 'other-agent')
    return store, key, endpoint, other_key, other_endpoint


def bind(store, key, endpoint, **changes):
    values = dict(principal='android-owner', key_id=key, device_id='phone', owner_id='android-owner', source_id='native',
                  auth_epoch=1, endpoint=endpoint, generation=7, state='active', routine_verified=True,
                  installed_verified=True, journal_id='epoch-one', after_seq=12) | changes
    with store.pool.connection() as c:
        c.execute('INSERT INTO android_wake_bindings(' + ','.join(values) + ',valid_until) VALUES(' +
                  ','.join(['%s'] * len(values)) + ",clock_timestamp()+interval '1 day')", list(values.values()))


def snapshot(store):
    with store.pool.connection() as c:
        return [c.execute('SELECT * FROM ' + table + ' ORDER BY 1,2,3').fetchall()
                for table in ('android_wake_bindings', 'android_wake_intents', 'delivery_endpoints', 'audit_log')]


def request(world, **changes):
    store, key, endpoint, *_ = world
    return dict(principal='android-owner', key_id=key, device_id='phone', owner_id='android-owner', source_id='native',
                auth_epoch=1, endpoint_id=endpoint,
                valid_until=(datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0).isoformat()) | changes


def write(tmp_path, document, name='binding.json'):
    path = tmp_path / name
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    return path


# spec: ANYM-997
def test_inspect_reads_one_exact_binding_with_its_endpoint_metadata_and_no_secret_or_write(world, tmp_path, monkeypatch, capsys):
    store, key, endpoint, other_key, other_endpoint = world
    bind(store, key, endpoint)
    with store.pool.connection() as c:
        c.execute('''INSERT INTO android_wake_intents(principal,key_id,wake_id,device_id,endpoint,binding_generation,identity_hash,
            identity,journal_id,source_seq,state,expires_at) VALUES('android-owner',%s,%s,'phone',%s,7,%s,%s,'epoch-one',12,
            'unknown',clock_timestamp()+interval '1 hour')''', (key, uuid4(), endpoint, 'b' * 64, json.dumps({'words': WORDS})))
    before = snapshot(store)
    # A runtime holder of the binding row cannot block the read: inspection takes no lock.
    with psycopg.connect(store.pool.conninfo, row_factory=dict_row) as holder:
        holder.execute('SELECT 1 FROM android_wake_bindings FOR UPDATE')
        code, result, raw = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-info',
                                    '--principal', 'android-owner', '--key-id', key, '--device', 'phone')
    assert code == 0 and result['found'] is True
    binding = result['binding']
    assert {k: binding[k] for k in ('principal', 'key_id', 'device_id', 'owner_id', 'source_id', 'auth_epoch', 'endpoint_id',
        'generation', 'state', 'routine_verified', 'installed_verified', 'journal_id', 'after_seq', 'live')} == dict(
        principal='android-owner', key_id=key, device_id='phone', owner_id='android-owner', source_id='native', auth_epoch=1,
        endpoint_id=endpoint, generation=7, state='active', routine_verified=True, installed_verified=True,
        journal_id='epoch-one', after_seq=12, live=True)
    assert result['endpoint'] == {**result['endpoint'], 'endpoint_id': endpoint, 'same_key': True, 'state': 'enabled'}
    assert result['key'] == {**result['key'], 'found': True, 'revoked': False, 'principal_enabled': True, 'wake_rights': True}
    assert result['outbox'] == {'unknown': 1}
    # Stored flags are reported, never treated as fresh proof: the dispatcher's own at-most-once requirement still shows.
    assert result['activation_blockers'] == ['endpoint_not_at_most_once']
    assert result['stored_flags_are_not_delivery_proof'] is True
    for leaked in (URL, 'canary-wake-path', SECRET, WORDS, 'other-agent', other_key, other_endpoint, 'signing'):
        assert leaked not in raw
    assert snapshot(store) == before


# spec: ANYM-997
def test_inspect_is_exact_and_reports_not_found_without_another_principal_or_device(world, tmp_path, monkeypatch, capsys):
    store, key, endpoint, other_key, _ = world
    bind(store, key, endpoint)
    before = snapshot(store)
    for principal, credential, device in (('android-owner', key, 'tablet'), ('other-agent', key, 'phone'),
                                          ('android-owner', other_key, 'phone')):
        code, result, raw = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-info',
                                    '--principal', principal, '--key-id', credential, '--device', device)
        assert code == 0 and result == {'found': False, 'principal': principal, 'key_id': credential, 'device_id': device}
        assert 'epoch-one' not in raw and endpoint not in raw
    # No broad default listing: every coordinate is required.
    for partial in (('--principal', 'android-owner', '--key-id', key), ('--key-id', key, '--device', 'phone')):
        assert operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-info', *partial)[0] == 2
    code, result, _ = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-info', '--principal',
                              'android-owner', '--key-id', 'not-a-uuid', '--device', 'phone')
    assert code == 1 and result == {'error': 'invalid_key_id'}
    assert snapshot(store) == before


# spec: ANYM-997
def test_enrollment_plans_then_creates_one_paused_unverified_binding_and_replays_the_same_request(
        world, tmp_path, monkeypatch, capsys):
    store, key, endpoint, *_ = world
    path = write(tmp_path, request(world))
    before = snapshot(store)
    code, plan, _ = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-enroll', '--file', str(path), '--check')
    assert code == 0 and plan['check'] == 'ok' and plan['would_create']['state'] == 'paused'
    assert snapshot(store) == before
    code, created, raw = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-enroll', '--file', str(path))
    assert code == 0 and created['created'] is True
    assert {k: created['binding'][k] for k in ('state', 'routine_verified', 'installed_verified', 'after_seq', 'journal_id',
                                               'generation', 'endpoint_id')} == dict(
        state='paused', routine_verified=False, installed_verified=False, after_seq=0, journal_id=None, generation=1,
        endpoint_id=endpoint)
    assert URL not in raw and SECRET not in raw
    after = snapshot(store)
    assert after[1:] == before[1:] and len(after[0]) == 1
    code, again, _ = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-enroll', '--file', str(path))
    assert code == 0 and again == {**created, 'created': False}
    assert snapshot(store) == after
    code, info, _ = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-info', '--principal', 'android-owner',
                            '--key-id', key, '--device', 'phone')
    assert {'state_paused', 'routine_unverified', 'installed_unverified'} <= set(info['activation_blockers'])


def _disable_endpoint(store, world):
    with store.pool.connection() as c:
        c.execute("UPDATE delivery_endpoints SET state='operator_disabled' WHERE id=%s", (world[2],))


def _revoke(store, world):
    with store.pool.connection() as c:
        c.execute('UPDATE api_keys SET revoked=true WHERE id=%s', (world[1],))


def _narrow(store, world):
    with store.pool.connection() as c:
        c.execute("UPDATE api_keys SET permissions=ARRAY['read'] WHERE id=%s", (world[1],))


def _disable_principal(store, world):
    with store.pool.connection() as c:
        c.execute("UPDATE principals SET enabled=false WHERE id='android-owner'")


# spec: ANYM-997
@pytest.mark.parametrize('case,change,setup,error', [
    ('wrong_principal', {'principal': 'other-agent'}, None, 'android_wake_key_not_found'),
    ('other_key', 'other_key', None, 'android_wake_key_not_found'),
    ('revoked_key', {}, _revoke, 'android_wake_key_revoked'),
    ('missing_rights', {}, _narrow, 'android_wake_rights_missing'),
    ('disabled_principal', {}, _disable_principal, 'android_wake_principal_disabled'),
    ('other_endpoint', 'other_endpoint', None, 'android_wake_endpoint_not_same_key'),
    ('unknown_endpoint', 'unknown_endpoint', None, 'android_wake_endpoint_not_same_key'),
    ('disabled_endpoint', {}, _disable_endpoint, 'android_wake_endpoint_not_enabled'),
    ('expired_lifetime', 'past', None, 'invalid_binding_lifetime'),
    ('beyond_key', 'beyond_key', None, 'invalid_binding_lifetime'),
    ('extra_field', {'state': 'active'}, None, 'invalid_binding_request'),
    ('verified_flag', {'routine_verified': True}, None, 'invalid_binding_request'),
    ('boolean_epoch', {'auth_epoch': True}, None, 'invalid_binding_request'),
    ('zero_epoch', {'auth_epoch': 0}, None, 'invalid_binding_request'),
    ('naive_time', 'naive', None, 'invalid_binding_request'),
    ('bad_device', {'device_id': 'phone one'}, None, 'invalid_binding_request'),
    ('missing_field', 'missing', None, 'invalid_binding_request'),
    ('not_json', 'not_json', None, 'invalid_binding_request'),
], ids=['wrong_principal', 'other_key', 'revoked_key', 'missing_rights', 'disabled_principal', 'other_endpoint', 'unknown_endpoint', 'disabled_endpoint', 'expired_lifetime', 'beyond_key', 'extra_field', 'verified_flag', 'boolean_epoch', 'zero_epoch', 'naive_time', 'bad_device', 'missing_field', 'not_json'])
def test_enrollment_refuses_wrong_identity_endpoint_lifetime_or_malformed_input_without_a_write(
        world, tmp_path, monkeypatch, capsys, case, change, setup, error):
    store, key, endpoint, other_key, other_endpoint = world
    now = datetime.now(timezone.utc)
    document = request(world)
    if change == 'other_key': document['key_id'] = other_key
    elif change == 'other_endpoint': document['endpoint_id'] = other_endpoint
    elif change == 'unknown_endpoint': document['endpoint_id'] = str(uuid4())
    elif change == 'past': document['valid_until'] = (now - timedelta(minutes=1)).isoformat()
    elif change == 'beyond_key': document['valid_until'] = (now + timedelta(days=30)).isoformat()
    elif change == 'naive': document['valid_until'] = (now + timedelta(days=1)).replace(tzinfo=None).isoformat()
    elif change == 'missing': document.pop('source_id')
    elif change == 'not_json': document = '{"principal": '
    else: document |= change
    if setup: setup(store, world)
    path = write(tmp_path, document)
    before = snapshot(store)
    for extra in (('--check',), ()):
        code, result, raw = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-enroll', '--file', str(path), *extra)
        assert (code, result) == (1, {'error': error})
        assert URL not in raw and SECRET not in raw
    assert snapshot(store) == before


# spec: ANYM-997
@pytest.mark.parametrize('state,change,error', [
    ('active', {}, 'android_wake_binding_active'),
    ('revoked', {}, 'android_wake_binding_revoked'),
    ('paused', {'source_id': 'other-source'}, 'android_wake_binding_conflict'),
    ('paused', {'auth_epoch': 2}, 'android_wake_binding_conflict'),
], ids=['active', 'revoked', 'paused_other_source', 'paused_other_epoch'])
def test_enrollment_never_overwrites_reactivates_or_rebinds_an_existing_binding(world, tmp_path, monkeypatch, capsys,
                                                                              state, change, error):
    store, key, endpoint, *_ = world
    bind(store, key, endpoint, state=state, routine_verified=state == 'active', installed_verified=state == 'active')
    path = write(tmp_path, request(world, **change))
    before = snapshot(store)
    for extra in (('--check',), ()):
        code, result, raw = operate(store, tmp_path, monkeypatch, capsys, 'android-wake-binding-enroll', '--file', str(path), *extra)
        assert code == 1 and result['error'] == error
        assert result['binding']['generation'] == 7 and result['binding']['after_seq'] == 12
        assert URL not in raw and SECRET not in raw
    assert snapshot(store) == before


# spec: ANYM-997
def test_concurrent_enrollments_create_one_paused_row_and_a_different_request_loses(world, tmp_path):
    from messaging_service import android_wake_admin as admin
    store = world[0]
    same = write(tmp_path, request(world), 'same.json')
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: admin.enroll(store, same), range(6)))
    assert sorted(r['created'] for r in results) == [False] * 5 + [True]
    assert len({json.dumps(r['binding'], sort_keys=True) for r in results}) == 1
    different = [write(tmp_path, request(world, device_id='tablet', source_id=f'source-{n}'), f'd{n}.json') for n in range(4)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda path: admin.enroll(store, path), different))
    assert sorted(str(r.get('created', r.get('error'))) for r in results) == ['True'] + ['android_wake_binding_conflict'] * 3
    with store.pool.connection() as c:
        rows = c.execute('SELECT state,routine_verified,installed_verified,after_seq FROM android_wake_bindings').fetchall()
    assert rows == [{'state': 'paused', 'routine_verified': False, 'installed_verified': False, 'after_seq': 0}] * 2
