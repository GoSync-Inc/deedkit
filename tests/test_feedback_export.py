"""Owner export of agent feedback through the operator container (ANYM-265)."""
import json
from pathlib import Path
import sys
import time

import psycopg
import pytest

from messaging_service import __main__ as operator
from messaging_service import feedback as feedback_module
from messaging_service import store as store_module
from messaging_service.feedback import export_feedback
from messaging_service.policy import AUDIT_OPERATIONS, OPERATION_PERMISSIONS
from messaging_service.store import APIError
from test_event_delivery import call, identity

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(store_module.__file__).parent
SUBMISSION = {'id', 'request_id', 'category', 'description', 'operation', 'created_at', 'principal', 'key_id'}
DISPOSITION = {'id', 'status', 'status_note', 'fixed_in', 'spec_ref', 'duplicate_of', 'status_changed_at'}
MERGED = 'deedkit@' + 'a' * 40


def submit(store, token, n, **options):
    return call(store, token, 'submit_feedback', 'synthetic-export-%d' % n, 'bug', 'Synthetic report %d' % n,
        **options)['feedback_id']


def exported(result):
    return [item['id'] for item in result.get('submissions', result['dispositions'])]


def everything(store, **options):
    """Every page of one export: (records by ID, export_head)."""
    result = store.export_feedback(**options)
    records, head = {}, result['export_head']
    while True:
        for item in result.get('submissions', []):
            assert item['id'] not in records, 'a page repeated a record'
            records[item['id']] = item
        if not result['has_more']:
            return records, head
        result = store.export_feedback(page=result['next_page'], limit=options.get('limit', 50))
        assert result['export_head'] == head


def insert(connection, owner, label):
    """A submission written directly by the owner connection, committed by the caller."""
    return str(connection.execute('''INSERT INTO agent_feedback(principal,key_id,request_id,request_hash,category,
        description,refs) VALUES(%s,%s,%s,repeat('a',64),'bug','Synthetic','{}') RETURNING id''',
        (owner['principal'], owner['key_id'], 'synthetic-' + label)).fetchone()[0])


def owner_of(store, feedback_id):
    with store.pool.connection() as c:
        return c.execute('SELECT principal,key_id FROM agent_feedback WHERE id=%s', (feedback_id,)).fetchone()


# spec: ANYM-265
def test_export_pages_every_principals_records_by_keyset(seeded):
    store, *_ = seeded
    first, second = identity(store), identity(store, 'feedback-other')
    ids = [submit(store, first, n) for n in range(3)] + [submit(store, second, n) for n in range(2)]
    records, head = everything(store, limit=2)
    assert sorted(records) == sorted(ids)
    assert {records[i]['principal'] for i in ids} == {'event-reader', 'feedback-other'}
    page = store.export_feedback(limit=2)
    assert page['has_more'] and page['next_page'] and len(page['submissions']) == 2
    last = store.export_feedback(page=store.export_feedback(page=page['next_page'], limit=2)['next_page'], limit=2)
    assert not last['has_more'] and last['next_page'] is None and len(last['submissions']) == 1
    # Records created after export_head never join that export's later pages.
    later = submit(store, first, 9)
    result = store.export_feedback(page=page['next_page'], limit=50)
    assert later not in exported(result) and result['export_head'] == page['export_head']
    for bad in ('x', 'eyJ2IjoyfQ', 'a' * 600):
        with pytest.raises(ValueError, match='invalid_page'):
            store.export_feedback(page=bad)


# spec: ANYM-265
def test_export_record_fields_untrusted_marker_and_no_refs(seeded):
    store, *_ = seeded
    token = identity(store)
    fid = submit(store, token, 1, refs={'chat_ids': ['telegram-demo-chat']})
    result = store.export_feedback()
    assert result['untrusted_content'] is True and result['mode'] == 'created'
    [submission] = result['submissions']
    [disposition] = result['dispositions']
    assert set(submission) == SUBMISSION and set(disposition) == DISPOSITION
    assert submission['id'] == disposition['id'] == fid and disposition['status'] == 'received'
    assert 'refs' not in json.dumps(result) and 'telegram-demo-chat' not in json.dumps(result)


# spec: ANYM-265
def test_export_runs_in_a_read_only_transaction(seeded):
    store, *_ = seeded
    submit(store, identity(store), 1)
    with store.pool.connection() as c:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            with c.transaction():
                export_feedback(c)
                c.execute("UPDATE agent_feedback SET status='accepted',spec_ref='ANYM-265'")
    assert [row['status'] for row in store.export_feedback()['dispositions']] == ['received']


# spec: ANYM-265
def test_export_argument_bounds(seeded):
    store, *_ = seeded
    for options, code in (({'since': '2026-10-22T00:00:00'}, 'invalid_since'),
                          ({'since': 'yesterday'}, 'invalid_since'),
                          ({'since': '2026-10-22T00:00:00Z', 'overlap': 1}, 'invalid_overlap'),
                          ({'since': '2026-10-22T00:00:00Z', 'overlap': 1441}, 'invalid_overlap'),
                          ({'overlap': 5}, 'overlap_needs_since_or_changed_since'),
                          ({'since': '2026-10-22T00:00:00Z', 'page': 'x'}, 'exclusive'),
                          ({'since': '2026-10-22T00:00:00Z', 'changed_since': '2026-10-22T00:00:00Z'}, 'exclusive')):
        with pytest.raises(ValueError, match=code):
            store.export_feedback(**options)
    for limit in (0, 101):
        with pytest.raises(APIError, match='invalid_limit'):
            store.export_feedback(limit=limit)


# spec: ANYM-265
def test_reverse_commit_order_and_empty_export_lose_nothing(seeded, database):
    store, *_ = seeded
    empty = store.export_feedback()
    assert empty['submissions'] == [] and not empty['has_more']
    after_empty = submit(store, identity(store), 1)
    found, head = everything(store, since=empty['export_head'])
    assert after_empty in found
    owner = owner_of(store, after_empty)
    dsn = database.pool.conninfo
    with psycopg.connect(dsn) as early, psycopg.connect(dsn) as late:
        early_id = insert(early, owner, 'early')   # the earlier created_at, committed last
        late_id = insert(late, owner, 'late')
        late.commit()
        first, head = everything(store, since=head)
        assert late_id in first and early_id not in first
        early.commit()
    second, _ = everything(store, since=head)
    assert early_id in second
    # Re-exported submissions are byte-identical.
    assert json.dumps(second[late_id]) == json.dumps(first[late_id])


# spec: ANYM-265
def test_row_committed_behind_the_cursor_returns_in_the_next_overlapping_export(seeded, database):
    store, *_ = seeded
    token = identity(store)
    first = submit(store, token, 0)
    owner = owner_of(store, first)
    start = store.export_feedback()['export_head']
    with psycopg.connect(database.pool.conninfo) as behind:
        hidden = insert(behind, owner, 'behind')   # timestamped between `first` and the rows below
        visible = [submit(store, token, n) for n in range(1, 4)]
        page = store.export_feedback(since=start, limit=2)   # the overlap reaches back to `first`
        assert exported(page) == [first, visible[0]]
        behind.commit()
        rest = store.export_feedback(page=page['next_page'], limit=2)
    assert exported(rest) == visible[1:] and not rest['has_more']
    again, _ = everything(store, since=page['export_head'])
    assert hidden in again


# spec: ANYM-265
def test_submission_over_the_transaction_timeout_is_not_stored_and_its_failure_is_audited(seeded, monkeypatch):
    store, *_ = seeded
    token = identity(store)
    validate = feedback_module._validate_refs

    def slow(*arguments):
        time.sleep(0.6)
        return validate(*arguments)
    monkeypatch.setattr(feedback_module, 'SUBMIT_TRANSACTION_TIMEOUT', '300ms')
    monkeypatch.setattr(feedback_module, '_validate_refs', slow)
    with pytest.raises(APIError) as failure:
        store.submit_feedback(token, 'synthetic-timeout-1', 'bug', 'Synthetic slow report')
    assert failure.value.code == 'query_timeout' and failure.value.audited
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM agent_feedback').fetchone()['n'] == 0
        rows = c.execute("SELECT * FROM audit_log WHERE operation='submit_feedback'").fetchall()
    assert [(row['principal'], row['outcome'], row['error_code'], row['resource_ids']) for row in rows] == [
        ('event-reader', 'denied', 'query_timeout', [])]
    # The pool recovered: the next submission within the bound is stored.
    monkeypatch.setattr(feedback_module, '_validate_refs', validate)
    assert submit(store, token, 2)


# spec: ANYM-265
@pytest.mark.parametrize('stage', ['audit', 'commit'])
def test_submission_timing_out_in_its_audit_or_commit_is_not_stored_and_is_audited(seeded, monkeypatch, stage):
    store, *_ = seeded
    token = identity(store)
    write = store_module.audit
    calls = []

    def slow(*arguments):
        calls.append(arguments[2])
        # The first call is the success audit inside the submission transaction: expire the timeout
        # before it (audit stage) or after it, so the next statement is the commit (commit stage).
        if len(calls) == 1 and stage == 'audit':
            time.sleep(0.6)
        write(*arguments)
        if len(calls) == 1 and stage == 'commit':
            time.sleep(0.6)
    monkeypatch.setattr(feedback_module, 'SUBMIT_TRANSACTION_TIMEOUT', '300ms')
    monkeypatch.setattr(store_module, 'audit', slow)
    with pytest.raises(APIError) as failure:
        store.submit_feedback(token, 'synthetic-timeout-' + stage, 'bug', 'Synthetic slow report')
    assert failure.value.code == 'query_timeout' and failure.value.audited
    with store.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM agent_feedback').fetchone()['n'] == 0
        rows = c.execute("SELECT * FROM audit_log WHERE operation='submit_feedback'").fetchall()
    assert [(row['principal'], row['outcome'], row['error_code'], row['resource_ids']) for row in rows] == [
        ('event-reader', 'denied', 'query_timeout', [])]
    monkeypatch.setattr(store_module, 'audit', write)
    assert submit(store, token, 2)


# spec: ANYM-265
def test_changed_since_finds_a_status_change_on_an_old_item(seeded):
    store, *_ = seeded
    token = identity(store)
    old, other = submit(store, token, 1), submit(store, token, 2)
    _, head = everything(store)
    changed = store.export_feedback(changed_since=head)
    assert changed['mode'] == 'changed' and changed['dispositions'] == [] and 'submissions' not in changed
    store.set_feedback_status([old], 'fixed', spec_ref='ANYM-265', fixed_in=MERGED, note='Merged')
    changed = store.export_feedback(changed_since=head)
    [item] = changed['dispositions']
    assert item['id'] == old and item['status'] == 'fixed' and item['fixed_in'] == MERGED
    assert item['spec_ref'] == 'ANYM-265' and item['status_note'] == 'Merged' and set(item) == DISPOSITION
    store.set_feedback_status([other], 'declined')
    page = store.export_feedback(changed_since=head, limit=1)
    assert exported(page) == [old] and page['has_more']
    assert exported(store.export_feedback(page=page['next_page'], limit=1)) == [other]


# spec: ANYM-265
def test_no_agent_key_reaches_the_export():
    assert 'export_feedback' not in OPERATION_PERMISSIONS and 'export_feedback' not in AUDIT_OPERATIONS
    assert 'export_feedback' not in SOURCE.joinpath('app.py').read_text()
    catalog = json.loads((ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())
    contracts = json.loads((ROOT / 'tests/platform/fixtures/messaging-contracts.json').read_text())
    assert not any('export' in tool['name'] and 'feedback' in tool['name'] for tool in catalog)
    assert not any('export' in tool['name'] and 'feedback' in tool['name'] for tool in contracts['tools'])


class RecordingStore:
    calls = []

    def __init__(self, *arguments):
        pass

    def export_feedback(self, **options):
        self.calls.append(('export_feedback', options))
        return {'untrusted_content': True}

    def close(self):
        self.calls.append(('close',))


# spec: ANYM-265
def test_cli_export_feedback_forwards_its_arguments(monkeypatch, tmp_path, capsys):
    RecordingStore.calls = []
    monkeypatch.setattr(operator, 'Store', RecordingStore)
    dsn = tmp_path / 'synthetic.dsn'
    dsn.write_text('postgresql://synthetic.invalid/messaging_probe_test')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn))
    monkeypatch.setattr(sys, 'argv', ['anymsg', 'export-feedback', '--since', '2026-10-22T00:00:00Z', '--overlap', '5',
                                      '--limit', '20'])
    operator.main()
    assert RecordingStore.calls == [('export_feedback', {'since': '2026-10-22T00:00:00Z', 'changed_since': None,
        'page': None, 'overlap': 5, 'limit': 20}), ('close',)]
    assert json.loads(capsys.readouterr().out) == {'untrusted_content': True}
