"""A maintainer key reads every agent's feedback metadata and the loop's released summary (ANYM-988)."""
from contextlib import contextmanager
from copy import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
import pytest

from messaging_service import __main__ as operator
from messaging_service import store as store_module
from messaging_service.backup import database_manifest
from messaging_service.policy import AUDIT_OPERATIONS, OPERATION_PERMISSIONS, PERMISSIONS, effective_permissions
from messaging_service.store import APIError
from test_event_delivery import call, identity

SOURCE = Path(store_module.__file__).parent
ROOT = Path(__file__).resolve().parents[1]
PERMISSION = 'feedback-maintainer-read'
MERGED = 'deedkit@' + 'a' * 40
NOW = datetime(2026, 11, 12, 9, 0, tzinfo=timezone.utc)
FORBIDDEN = {'description', 'refs', 'status_note', 'request_id', 'key_id', 'request_hash'}


def maintainer(store, name='maintainer'):
    permissions = ['read', PERMISSION]
    return store.issue_key(name, ['telegram-demo'], permissions=permissions, key_permissions=permissions)


def submit(store, token, n, **options):
    return call(store, token, 'submit_feedback', 'synthetic-maintainer-%d' % n, 'bug',
        options.pop('description', 'Synthetic report %d' % n), **options)['feedback_id']


def projection(feedback_id, **changes):
    item = {'feedback_id': feedback_id, 'classification': 'reported-bug', 'reason_code': 'new-idea',
        'primary_spec': None, 'new_card': 'synthetic-card', 'released_brief': 'A synthetic problem in plain words.',
        'released_at': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        'reader_pin': 'reader-2.1.316/release-1'}
    return {**item, **changes}


def record(store, *items):
    return store.record_feedback_projection({'projections': list(items)})


def pages(store, token, **options):
    """Every page of one export, following next_cursor while has_more: (items in order, last reply)."""
    reply = store.export_feedback_metadata(token, **options)
    items = list(reply['items'])
    while reply['has_more']:
        reply = store.export_feedback_metadata(token, cursor=reply['next_cursor'], **options)
        items += reply['items']
    return items, reply


def ids(items):
    return [item['feedback_id'] for item in items]


def projection_rows(store):
    with store.pool.connection() as c:
        return c.execute('SELECT * FROM feedback_projections ORDER BY feedback_id').fetchall()


def audit_rows(store, operation):
    with store.pool.connection() as c:
        return c.execute('SELECT * FROM audit_log WHERE operation=%s ORDER BY id', (operation,)).fetchall()


@pytest.fixture()
def clean(seeded):
    store = seeded[0]
    with store.pool.connection() as c:
        c.execute('TRUNCATE agent_feedback CASCADE')
    return store


# spec: ANYM-988
def test_the_maintainer_permission_is_known_gates_the_read_and_is_never_inherited(clean):
    store = clean
    assert PERMISSION in PERMISSIONS
    assert OPERATION_PERMISSIONS['export_feedback_metadata'] == PERMISSION
    assert {'export_feedback_metadata', 'record_feedback_projection'} <= AUDIT_OPERATIONS
    token = maintainer(store)
    assert store.export_feedback_metadata(token)['items'] == []
    for held in (['read', 'feedback-write'], ['read', 'subscription-write', 'subscription-ack', 'feedback-write']):
        other = store.issue_key('not-maintainer-%d' % len(held), ['telegram-demo'], permissions=held, key_permissions=held)
        with pytest.raises(APIError) as refused:
            store.export_feedback_metadata(other)
        assert refused.value.code == 'forbidden'
    # The reporting key itself is no maintainer; a NULL key ceiling never inherits a new permission.
    reporter = identity(store, 'reporter')
    submit(store, reporter, 1)
    with pytest.raises(APIError):
        store.export_feedback_metadata(reporter)
    assert effective_permissions(['read', PERMISSION], None) == ['read']
    assert effective_permissions(['read', PERMISSION], ['read', PERMISSION]) == sorted(['read', PERMISSION])
    assert [row['outcome'] for row in audit_rows(store, 'export_feedback_metadata')].count('denied') == 3
    # The widened CHECKs still refuse any other word on a write.
    with pytest.raises(psycopg.errors.CheckViolation), store.pool.connection() as c:
        c.execute("UPDATE principals SET permissions=ARRAY['read','feedback-maintainer-write']::text[] WHERE id='maintainer'")


# spec: ANYM-988
def test_a_key_with_only_the_maintainer_permission_reads_feedback_and_nothing_else(clean):
    store = clean
    fid = submit(store, identity(store), 1)
    only = store.issue_key('bridge', ['telegram-demo'], permissions=[PERMISSION], key_permissions=[PERMISSION])
    assert ids(store.export_feedback_metadata(only)['items']) == [fid]
    assert store.capabilities(only)['permissions'] == [PERMISSION]
    # No `read`: no archive read, no own-feedback list, no submission.
    for refused in (lambda: store.list_chats(only), lambda: call(store, only, 'list_feedback'),
                    lambda: call(store, only, 'submit_feedback', 'synthetic-bridge-1', 'bug', 'Synthetic')):
        with pytest.raises(APIError) as failure:
            refused()
        assert failure.value.code == 'forbidden'


# spec: ANYM-988
def test_migration_094_is_an_expand_file_and_the_table_keeps_the_readers_closed_sets(clean):
    store = clean
    text = SOURCE.joinpath('migrations/094_feedback_projections.sql').read_text()
    assert text.startswith("-- rollout: manual")
    fid = submit(store, identity(store), 1)
    bad = {'classification': 'bogus', 'reason_code': 'bogus', 'primary_spec': 'card', 'new_card': 'Not A Slug',
        'released_brief': 'x' * 601, 'reader_pin': ''}
    base = {'classification': 'feature', 'reason_code': 'new-idea', 'reader_pin': 'pin'}
    for column, value in bad.items():
        with pytest.raises(psycopg.errors.CheckViolation), store.pool.connection() as c:
            values = {**base, column: value}
            c.execute('INSERT INTO feedback_projections(feedback_id,classification,reason_code,primary_spec,new_card,'
                'released_brief,released_at,reader_pin) VALUES(%s,%s,%s,%s,%s,%s,now(),%s)', (fid, values['classification'],
                values['reason_code'], values.get('primary_spec'), values.get('new_card'), values.get('released_brief'),
                values['reader_pin']))
    record(store, projection(fid))
    assert len(projection_rows(store)) == 1
    # A deleted feedback row takes its projection with it; nothing blocks the delete.
    with store.pool.connection() as c:
        c.execute('DELETE FROM agent_feedback WHERE id=%s', (fid,))
    assert projection_rows(store) == []


# spec: ANYM-988
def test_the_api_role_reads_the_table_and_never_writes_it(clean):
    store = clean
    role = 'test_projection_gateway_' + uuid4().hex[:8]
    with store.pool.connection() as c:
        c.execute(SOURCE.joinpath('gateway_role.sql').read_text().replace('messaging_gateway', role))

    class RestrictedPool:
        @contextmanager
        def connection(self):
            with store.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    gateway = copy(store)
    gateway.pool = RestrictedPool()
    try:
        token = maintainer(store)
        fid = submit(store, identity(store), 1)
        record(store, projection(fid))
        read = gateway.export_feedback_metadata(token)
        assert ids(read['items']) == [fid] and read['items'][0]['projection']['classification'] == 'reported-bug'
        for statement in ("INSERT INTO feedback_projections(feedback_id,classification,reason_code,released_at,reader_pin) "
                          "VALUES('%s','feature','new-idea',now(),'pin')" % uuid4(),
                          "UPDATE feedback_projections SET reader_pin='x'", 'DELETE FROM feedback_projections'):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with gateway.pool.connection() as c:
                    c.execute(statement)
    finally:
        with store.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


# spec: ANYM-988
def test_a_refused_projection_file_writes_nothing(clean):
    store = clean
    first, second = submit(store, identity(store), 1), submit(store, identity(store, 'other'), 2)
    good = projection(first)
    unknown = str(uuid4())
    cases = {
        'invalid_projection_file': [[], {'rows': []}, {'projections': [good], 'extra': 1}],
        'projections_1_to_100': [{'projections': []}, {'projections': [projection(first)] * 101}],
        'invalid_feedback_id': [{'projections': [good, projection('not-a-uuid')]}],
        'invalid_projection': [{'projections': [good, {**projection(second), 'description': 'private words'}]},
                               {'projections': [good, {k: v for k, v in projection(second).items() if k != 'reader_pin'}]}],
        'invalid_classification': [{'projections': [good, projection(second, classification='bogus')]}],
        'invalid_reason_code': [{'projections': [good, projection(second, reason_code='bogus')]}],
        'invalid_primary_spec': [{'projections': [good, projection(second, primary_spec='the card')]}],
        'invalid_new_card': [{'projections': [good, projection(second, new_card='Not A Slug')]}],
        'invalid_released_brief': [{'projections': [good, projection(second, released_brief='line\nbreak')]},
                                   {'projections': [good, projection(second, released_brief='x' * 601)]},
                                   {'projections': [good, projection(second, released_brief='')]}],
        'invalid_released_at': [{'projections': [good, projection(second, released_at='yesterday')]},
                                {'projections': [good, projection(second, released_at='2026-11-12T09:00:00')]},
                                {'projections': [good, projection(second,
                                    released_at=(datetime.now(timezone.utc) + timedelta(days=2)).isoformat())]}],
        'invalid_reader_pin': [{'projections': [good, projection(second, reader_pin='p' * 129)]}],
        'duplicate_feedback_id': [{'projections': [good, projection(first)]}],
        'unknown_feedback': [{'projections': [good, projection(unknown)]}],
    }
    for code, documents in cases.items():
        for document in documents:
            with pytest.raises(ValueError) as refused:
                store.record_feedback_projection(document)
            assert str(refused.value).startswith(code), (code, str(refused.value))
            assert unknown not in str(refused.value) and 'private words' not in str(refused.value)
    assert projection_rows(store) == [] and audit_rows(store, 'record_feedback_projection') == []


# spec: ANYM-988
def test_recording_is_idempotent_newer_wins_and_audits_no_brief(clean):
    store = clean
    first, second = submit(store, identity(store), 1), submit(store, identity(store, 'other'), 2)
    older = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    one, two = projection(first, released_at=older), projection(second, released_at=older, released_brief=None, new_card=None,
        classification='question', reason_code='question-answered')
    assert record(store, one, two) == {'recorded': 2, 'unchanged': 0}
    rows = projection_rows(store)
    assert [row['feedback_id'] for row in rows] == sorted([first, second]) or len(rows) == 2
    assert record(store, one, two) == {'recorded': 0, 'unchanged': 2}
    assert len(audit_rows(store, 'record_feedback_projection')) == 2
    # A newer release replaces; an older or equal one never does, whatever its content.
    changed = projection(first, released_at=newer, released_brief='A newer wording of the same problem.')
    assert record(store, changed) == {'recorded': 1, 'unchanged': 0}
    assert record(store, projection(first, released_at=older, released_brief='stale wording')) == {'recorded': 0, 'unchanged': 1}
    assert record(store, projection(first, released_at=newer, released_brief='same time, other words')) == \
        {'recorded': 0, 'unchanged': 1}
    stored = {str(row['feedback_id']): row for row in projection_rows(store)}
    assert stored[first]['released_brief'] == 'A newer wording of the same problem.'
    assert stored[second]['released_brief'] is None
    # The audit rows hold the feedback ID only: no brief, card slug or classification.
    audits = audit_rows(store, 'record_feedback_projection')
    assert len(audits) == 3 and all(row['actor'] == 'operator' and len(row['resource_ids']) == 1 for row in audits)
    text = json.dumps(audits, default=str)
    assert 'wording' not in text and 'synthetic-card' not in text and 'reported-bug' not in text


# spec: ANYM-988
def test_the_read_holds_metadata_and_the_projection_of_every_principal_and_nothing_private(clean):
    store = clean
    one, two = identity(store, 'agent-one'), identity(store, 'agent-two')
    canary = 'canary-description-' + uuid4().hex
    note = 'canary-note-' + uuid4().hex
    first = submit(store, one, 1, description=canary, refs={'chat_ids': ['telegram-demo-chat']}, operation='list_chats')
    second = submit(store, two, 2)
    store.set_feedback_status([first], 'fixed', spec_ref='ANYM-988', fixed_in=MERGED, note=note)
    record(store, projection(second, primary_spec='ANYM-988'))
    reply = store.export_feedback_metadata(maintainer(store))
    assert reply['untrusted_content'] is True and reply['has_more'] is False and reply['next_cursor']
    by_id = {item['feedback_id']: item for item in reply['items']}
    assert set(by_id) == {first, second}
    assert by_id[first]['principal'] == 'agent-one' and by_id[second]['principal'] == 'agent-two'
    assert {key: by_id[first][key] for key in ('category', 'operation', 'status', 'fixed_in', 'spec_ref', 'duplicate_of')} == {
        'category': 'bug', 'operation': 'list_chats', 'status': 'fixed', 'fixed_in': MERGED, 'spec_ref': 'ANYM-988',
        'duplicate_of': None}
    assert by_id[first]['status_changed_at'] and by_id[first]['created_at'] and by_id[first]['projection'] is None
    assert by_id[second]['status'] == 'received' and by_id[second]['projection'] == {'classification': 'reported-bug',
        'reason_code': 'new-idea', 'primary_spec': 'ANYM-988', 'new_card': 'synthetic-card',
        'released_brief': 'A synthetic problem in plain words.', 'released_at': by_id[second]['projection']['released_at'],
        'reader_pin': 'reader-2.1.316/release-1'}
    text = json.dumps(reply)
    for forbidden in (canary, note, 'telegram-demo-chat', 'synthetic-maintainer-1'):
        assert forbidden not in text
    for item in reply['items']:
        assert not FORBIDDEN & set(item)
    assert len(audit_rows(store, 'export_feedback_metadata')) == 1


# spec: ANYM-988
def test_a_status_change_or_a_recorded_projection_moves_an_item_to_the_end_of_the_order(clean):
    store = clean
    token = maintainer(store)
    reporter = identity(store)
    first, second, third = (submit(store, reporter, n) for n in (1, 2, 3))
    assert ids(pages(store, token)[0]) == [first, second, third]
    store.set_feedback_status([first], 'accepted', spec_ref='ANYM-988')
    assert ids(pages(store, token)[0]) == [second, third, first]
    record(store, projection(second))
    assert ids(pages(store, token)[0]) == [third, first, second]
    store.set_feedback_status([third], 'declined', note='Synthetic note')
    assert ids(pages(store, token)[0]) == [first, second, third]


# spec: ANYM-988
def test_paging_returns_every_item_once_and_the_final_cursor_overlaps_the_last_minutes(clean):
    store = clean
    token = maintainer(store)
    reporter = identity(store)
    submitted = [submit(store, reporter, n) for n in range(1, 6)]
    items, last = pages(store, token, limit=1)
    assert ids(items) == submitted and len(ids(items)) == len(set(ids(items)))
    assert last['has_more'] is False and last['next_cursor']
    # A row committed behind the export head is returned by the next poll (the overlap) and is not lost.
    late = submit(store, reporter, 6)
    with store.pool.connection() as c:
        c.execute("UPDATE agent_feedback SET created_at=created_at-interval '1 minute' WHERE id=%s", (late,))
    again = store.export_feedback_metadata(token, cursor=last['next_cursor'], limit=100)
    assert late in ids(again['items']) and set(ids(again['items'])) <= set(submitted) | {late}
    assert set(submitted[-2:]) <= set(ids(again['items']))
    # An empty database still hands back a cursor that a later poll accepts.
    with store.pool.connection() as c:
        c.execute('TRUNCATE agent_feedback CASCADE')
    empty = store.export_feedback_metadata(token)
    assert empty['items'] == [] and empty['has_more'] is False
    fresh = submit(store, reporter, 7)
    assert ids(store.export_feedback_metadata(token, cursor=empty['next_cursor'])['items']) == [fresh]


# spec: ANYM-988
def test_a_bad_cursor_or_limit_is_refused_and_a_removed_permission_ends_the_next_page(clean):
    store = clean
    token = maintainer(store)
    reporter = identity(store)
    for n in (1, 2):
        submit(store, reporter, n)
    first = store.export_feedback_metadata(token, limit=1)
    for cursor in ('garbage', '', first['next_cursor'][:-4], 'e30', 'x' * 600):
        with pytest.raises(APIError) as refused:
            store.export_feedback_metadata(token, cursor=cursor)
        assert refused.value.code == 'invalid_cursor', cursor
    for limit in (0, 101, True, '5'):
        with pytest.raises(APIError) as refused:
            store.export_feedback_metadata(token, limit=limit)
        assert refused.value.code == 'invalid_limit'
    store.set_key_permissions(store.capabilities(token)['credential_id'], ['read'])
    with pytest.raises(APIError) as refused:
        store.export_feedback_metadata(token, cursor=first['next_cursor'], limit=1)
    assert refused.value.code == 'forbidden'


# spec: ANYM-988
def test_the_backup_manifest_covers_the_projection_table(clean):
    store = clean
    fid = submit(store, identity(store), 1)
    with store.pool.connection() as c:
        before = database_manifest(c)['feedback_projections']
    record(store, projection(fid))
    with store.pool.connection() as c:
        assert database_manifest(c)['feedback_projections'] != before


# spec: ANYM-988
def test_rest_and_mcp_carry_the_read_and_capabilities_name_it_only_to_a_holder(clean):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    store = clean
    token, other = maintainer(store), identity(store)
    fid = submit(store, other, 1)
    assert 'export_feedback_metadata' in store.capabilities(token, detail='full')['available']
    assert 'export_feedback_metadata' not in store.capabilities(other, detail='full')['available']
    headers = {'Authorization': 'Bearer ' + token}
    with TestClient(create_app(store, ['testserver'])) as client:
        rest = client.post('/v1/export_feedback_metadata', headers=headers, json={'limit': 5})
        assert rest.status_code == 200 and ids(rest.json()['items']) == [fid] and rest.json()['untrusted_content'] is True
        again = client.post('/v1/export_feedback_metadata', headers=headers, json={'cursor': rest.json()['next_cursor']})
        assert again.status_code == 200 and ids(again.json()['items']) == [fid]
        mcp = rpc(client, token, 'export_feedback_metadata', {'limit': 5}).json()['result']
        assert not mcp.get('isError') and ids(mcp['structuredContent']['items']) == [fid]
        assert client.post('/v1/export_feedback_metadata', headers=headers, json={'limit': 0}).status_code in (400, 422)
        assert client.post('/v1/export_feedback_metadata', headers=headers, json={'status': 'fixed'}).status_code in (400, 422)
        denied = client.post('/v1/export_feedback_metadata', headers={'Authorization': 'Bearer ' + other}, json={})
        assert denied.status_code == 403 and 'items' not in denied.json()
        assert rpc(client, other, 'export_feedback_metadata', {}).json()['result']['isError'] is True


class RecordingStore:
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    def record_feedback_projection(self, document):
        self.calls.append(('record_feedback_projection', document))
        return {'recorded': 1, 'unchanged': 0}

    def close(self):
        self.calls.append(('close',))


# spec: ANYM-988
def test_the_operator_command_reads_its_file_and_prints_counts_only(monkeypatch, tmp_path, capsys):
    RecordingStore.calls = []
    monkeypatch.setattr(operator, 'Store', RecordingStore)
    dsn = tmp_path / 'synthetic.dsn'
    dsn.write_text('postgresql://synthetic.invalid/messaging_probe_test')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    document = {'projections': [projection(str(uuid4()))]}
    path = tmp_path / 'projections.json'
    path.write_text(json.dumps(document))
    monkeypatch.setattr(sys, 'argv', ['anymsg', 'record-feedback-projection', '--file', str(path)])
    operator.main()
    assert RecordingStore.calls == [('record_feedback_projection', document), ('close',)]
    assert json.loads(capsys.readouterr().out) == {'recorded': 1, 'unchanged': 0}
    # No file, or a file that is not JSON, never reaches the store.
    RecordingStore.calls = []
    monkeypatch.setattr(sys, 'argv', ['anymsg', 'record-feedback-projection'])
    with pytest.raises(SystemExit):
        operator.main()
    path.write_text('not json')
    monkeypatch.setattr(sys, 'argv', ['anymsg', 'record-feedback-projection', '--file', str(path)])
    with pytest.raises(ValueError, match='invalid_projection_file'):
        operator.main()
    assert [call_[0] for call_ in RecordingStore.calls if call_[0] != 'close'] == []
