"""Operator inventory of failed WhatsApp originals (ANYM-815): read-only, grouped, media key from the adapter."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess

import psycopg
import pytest
from psycopg.rows import dict_row

from messaging_service import collector_control, media_inventory
from test_archive import archive as archive, event

ACCOUNT = 'inventory-wa'
CHAT = '15551234567@c.us'


def failed(ar, s, mid, *, sent_days, queued_days=0, code: str | None = 'media_network_error', chat=CHAT,
           mime='image/jpeg', size: int | None = 4, state='failed', attempts=8):
    at = (datetime.now(timezone.utc)-timedelta(days=sent_days)).isoformat()
    media = {'provider_id': 'media-'+mid, 'filename': 'synthetic.bin', 'mime': mime}
    if size is not None: media['bytes'] = size
    message = ar.ingest(event(account=ACCOUNT, platform='whatsapp', chat=chat, provider_id=mid, sent_at=at,
        updated_at=at, media=[media]))['message']
    with s.pool.connection() as c:
        fid = c.execute('SELECT id FROM media_jobs WHERE message=%s', (message,)).fetchone()['id']
        c.execute('''UPDATE media_jobs SET state=%s,error_code=%s,attempts=%s,
            created_at=clock_timestamp()-make_interval(days=>%s) WHERE id=%s''', (state, code, attempts, queued_days, fid))
    return fid


@pytest.fixture()
def inventory_archive(archive):
    ar, s, _, _ = archive
    ar.register(ACCOUNT, 'whatsapp', 'Synthetic inventory source', discover=True)
    return ar, s


class FakeAdapter:
    """Answers media_keys lines like the native adapter's journal lookup; records every call."""
    def __init__(self, held, unknown=()):
        self.held, self.unknown, self.calls = held, set(unknown), []

    def __call__(self, argv, *, input, timeout, **_):
        assert timeout <= 60
        self.calls.append((argv, input))
        out = []
        for line in input.splitlines():
            request = json.loads(line)
            assert request['method'] == 'media_keys' and len(request['params']['messages']) <= 200
            out.append(json.dumps({'id': request['id'], 'result': {'keys': [
                'held' if m['message'] in self.held else 'unknown' if m['message'] in self.unknown else 'none'
                for m in request['params']['messages']]}}))
        return subprocess.CompletedProcess(argv, 0, '\n'.join(out)+'\n', '')


def probe_dsn():
    # The disposable database of the `database` fixture (tests/conftest.py), which these tests already require.
    return Path(os.environ['PROBE_TEST_DSN_FILE']).read_text().strip()


def groups(result):
    return {(g['code'], g['chat_kind'], g['age_when_queued'], g['mime_class']): g for g in result['groups']}


# spec: ANYM-815
def test_inventory_groups_failed_whatsapp_jobs_by_code_kind_age_mime_and_media_key(inventory_archive):
    ar, s = inventory_archive
    failed(ar, s, 'false_'+CHAT+'_OLD1', sent_days=400)
    failed(ar, s, 'false_'+CHAT+'_OLD2', sent_days=400, size=None)
    failed(ar, s, 'false_'+CHAT+'_YOUNG', sent_days=10, code='media_unavailable', mime='audio/ogg')
    failed(ar, s, 'false_'+CHAT+'_SPENT', sent_days=60, code=None, mime='application/pdf', state='queued')
    failed(ar, s, 'false_1203630000@g.us_GROUP', sent_days=50, chat='1203630000@g.us')
    failed(ar, s, 'false_'+CHAT+'_ALIVE', sent_days=400, state='queued', attempts=2)  # still retried: not listed
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='group' WHERE provider_chat='1203630000@g.us' AND account=%s", (ACCOUNT,))
    adapter = FakeAdapter({'false_'+CHAT+'_OLD1', 'false_1203630000@g.us_GROUP'}, unknown={'false_'+CHAT+'_SPENT'})
    with s.pool.connection() as c:
        result = media_inventory.inventory(c, ACCOUNT, adapter=('/synthetic/anymsg-whatsmeow', '/synthetic/config.json'),
            run=adapter)
    assert result['failed_jobs'] == 5 and result['media_key_basis'] == 'native_journal'
    assert result['age_basis'] == 'age_when_queued' and result['retention_days_estimate'] == 30
    g = groups(result)
    old = g[('media_network_error', 'automatic', 'over_30d', 'image')]
    assert (old['jobs'], old['bytes_known'], old['bytes'], old['now_age']) == (2, 1, 4, 'over_1y')
    assert (old['media_key_held'], old['media_key_none'], old['media_key_unknown']) == (1, 1, 0)
    young = g[('media_unavailable', 'automatic', 'under_30d', 'audio')]
    assert young['jobs'] == 1 and young['now_age'] == 'under_30d' and young['media_key_none'] == 1
    spent = g[('retry_exhausted', 'automatic', 'over_30d', 'document')]
    assert spent['jobs'] == 1 and spent['media_key_unknown'] == 1  # an unreadable journal row is never "none"
    group = g[('media_network_error', 'on_request', 'over_30d', 'image')]
    assert group['jobs'] == 1 and group['media_key_held'] == 1
    # One adapter run, one request line for these five, never a provider method; no identity is printed.
    assert len(adapter.calls) == 1 and adapter.calls[0][0][-1] == '--media-keys'
    assert len(adapter.calls[0][1].splitlines()) == 1
    assert CHAT not in json.dumps(result) and 'OLD1' not in json.dumps(result)


# spec: ANYM-815
def test_media_keys_are_asked_in_batches_of_at_most_200(inventory_archive):
    ar, s = inventory_archive
    refs = [('c%d@c.us' % n, 'false_c%d@c.us_M' % n) for n in range(401)]
    adapter = FakeAdapter({refs[0][1], refs[400][1]})
    keys = media_inventory.adapter_keys('/synthetic/anymsg-whatsmeow', '/synthetic/config.json', refs, run=adapter)
    assert len(keys) == 401 and keys.count('held') == 2 and keys[0] == keys[400] == 'held'
    assert [len(json.loads(line)['params']['messages']) for line in adapter.calls[0][1].splitlines()] == [200, 200, 1]


# spec: ANYM-815
@pytest.mark.parametrize('reply', ['', 'not json\n', '{"id":1,"error":"invalid_request"}\n',
    '{"id":1,"result":{"keys":["held","held"]}}\n', '{"id":1,"result":null}\n', '{"id":1,"result":[]}\n',
    '{"id":1,"result":{"keys":[true]}}\n', '{"id":1,"result":{"held":[true]}}\n', 'exit'])
def test_media_key_is_unknown_when_the_adapter_cannot_answer(inventory_archive, reply):
    ar, s = inventory_archive
    failed(ar, s, 'false_'+CHAT+'_OLD1', sent_days=400)
    def run(argv, **_):
        if reply == 'exit': raise subprocess.TimeoutExpired(argv, 60)
        return subprocess.CompletedProcess(argv, 0, reply, '')
    with s.pool.connection() as c:
        result = media_inventory.inventory(c, ACCOUNT, adapter=('/synthetic/x', '/synthetic/c'), run=run)
    assert result['media_key_basis'] == 'unavailable'
    assert result['groups'][0]['media_key_unknown'] == 1 and result['groups'][0]['media_key_held'] == 0


# spec: ANYM-815
def test_inventory_runs_inside_a_read_only_transaction_and_changes_nothing(inventory_archive, tmp_path, monkeypatch, capsys):
    ar, s = inventory_archive
    fid = failed(ar, s, 'false_'+CHAT+'_OLD1', sent_days=400)
    dsn = probe_dsn()
    with s.pool.connection() as c:
        before = c.execute('SELECT state,error_code,attempts,next_due,requested_until FROM media_jobs WHERE id=%s',
            (fid,)).fetchone()
    opened = []
    real = psycopg.connect
    def connect(*args, **kwargs):
        connection = real(*args, **kwargs); opened.append(connection); return connection
    monkeypatch.setattr(media_inventory.psycopg, 'connect', connect)
    dsn_file = tmp_path/'dsn'; dsn_file.write_text(dsn); dsn_file.chmod(0o600)
    config = tmp_path/'collector.json'
    config.write_text(json.dumps({'provider': 'whatsmeow', 'account': ACCOUNT,
        'native_executable': '/synthetic/anymsg-whatsmeow'}))
    config.chmod(0o600)
    adapter = FakeAdapter(set())
    monkeypatch.setattr(media_inventory.subprocess, 'run', adapter)
    collector_control.run_media_inventory(dsn_file, ACCOUNT, config)
    printed = json.loads(capsys.readouterr().out)
    assert printed['failed_jobs'] == 1 and printed['media_key_basis'] == 'native_journal'
    assert adapter.calls[0][0] == ['/synthetic/anymsg-whatsmeow', '--config', str(config), '--media-keys']
    assert len(opened) == 1 and opened[0].read_only is True
    # The same reads succeed in a transaction PostgreSQL itself keeps read-only: none of them writes.
    with real(dsn, row_factory=dict_row) as c:
        c.read_only = True  # every transaction of this session begins READ ONLY
        assert media_inventory.inventory(c, ACCOUNT)['failed_jobs'] == 1
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            c.execute("UPDATE media_jobs SET attempts=attempts WHERE id=%s", (fid,))
        c.rollback()
    with s.pool.connection() as c:
        assert c.execute('SELECT state,error_code,attempts,next_due,requested_until FROM media_jobs WHERE id=%s',
            (fid,)).fetchone() == before


# spec: ANYM-815
def test_inventory_refuses_an_unknown_or_non_whatsapp_account(inventory_archive, tmp_path, capsys):
    ar, s = inventory_archive
    dsn_file = tmp_path/'dsn'; dsn_file.write_text(probe_dsn()); dsn_file.chmod(0o600)
    for account in ('archive-test', 'missing-account', 'bad account'):
        with pytest.raises(SystemExit):
            collector_control.run_media_inventory(dsn_file, account, None)
        assert capsys.readouterr().out.strip() == 'invalid_account'


# spec: ANYM-815
def test_inventory_refuses_the_collector_config_of_another_account(inventory_archive, tmp_path, monkeypatch, capsys):
    ar, s = inventory_archive
    failed(ar, s, 'false_'+CHAT+'_OLD1', sent_days=400)
    dsn_file = tmp_path/'dsn'; dsn_file.write_text(probe_dsn()); dsn_file.chmod(0o600)
    config = tmp_path/'collector.json'
    config.write_text(json.dumps({'provider': 'whatsmeow', 'account': 'other-wa',
        'native_executable': '/synthetic/anymsg-whatsmeow'}))
    config.chmod(0o600)
    adapter = FakeAdapter({'false_'+CHAT+'_OLD1'})
    monkeypatch.setattr(media_inventory.subprocess, 'run', adapter)
    with pytest.raises(SystemExit):
        collector_control.run_media_inventory(dsn_file, ACCOUNT, config)
    assert capsys.readouterr().out.strip() == 'collector_config_mismatch' and adapter.calls == []
