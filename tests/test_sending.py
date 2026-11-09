"""Outbound contracts use isolated PostgreSQL and synthetic providers only."""
import asyncio
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.collector_state import CollectorState
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from test_archive import event


@pytest.fixture
def sender(seeded):
    s, reader, _ = seeded
    archive = Archive(s)
    archive.register('send-fixture', 'telegram', 'Synthetic sender', discover=True)
    archive.ingest(event(account='send-fixture', chat='100', provider_id='1', body='Existing synthetic message'))
    key = s.issue_key('send-agent', ['send-fixture'], permissions=['read', 'send'])
    epoch = archive.acquire('send-fixture', 'synthetic-send-worker')
    state = CollectorState(archive, 'send-fixture', ('synthetic-send-worker', epoch))
    state.phase('connection', 'connected', 'provider_ready')
    archive.set_sending('send-fixture', True)
    yield s, archive, key, reader, state
    archive.release('send-fixture', 'synthetic-send-worker', epoch)


def submit(s, key, **changes):
    return s.send_message(key, account='send-fixture',
        chat_id=stable_id('chat', 'send-fixture', '100'), text='Exact synthetic text',
        request_id='synthetic-request-0001', **changes)


def test_read_only_key_cannot_send(seeded):
    s, key, _ = seeded
    with pytest.raises(APIError) as error:
        s.send_message(key, account='telegram-demo', chat_id='telegram-demo-chat',
            text='Synthetic', request_id='synthetic-request-0001')
    assert error.value.code == 'forbidden'


def test_idempotency_and_account_chat_scope(sender):
    s, _, key, reader, _ = sender
    first = submit(s, key)
    assert first['status'] == 'queued' and first['provider_message_id'] is None
    assert submit(s, key)['send_id'] == first['send_id']
    with pytest.raises(APIError, match='idempotency_conflict'):
        s.send_message(key, account='send-fixture', chat_id=first['chat_id'],
            text='Different synthetic text', request_id='synthetic-request-0001')
    with pytest.raises(APIError, match='forbidden'):
        s.get_send_status(reader, first['send_id'])
    other = s.issue_key('other-send-agent', ['send-fixture'], permissions=['read', 'send'])
    with pytest.raises(APIError, match='not_found'):
        s.get_send_status(other, first['send_id'])
    s.set_access('send-agent', chats=[])
    with pytest.raises(APIError, match='not_found'):
        submit(s, key)
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n'] == 1
        rows = c.execute("SELECT resource_ids FROM audit_log WHERE operation='send_message'").fetchall()
        assert 'Exact synthetic text' not in str(rows)


@pytest.mark.parametrize('text', ['', '  ', '\0', '😀' * 2001])
def test_invalid_text_never_queues(sender, text):
    s, _, key, _, _ = sender
    with pytest.raises(APIError, match='invalid_message_text'):
        s.send_message(key, account='send-fixture', chat_id=stable_id('chat','send-fixture','100'),
            text=text, request_id='synthetic-request-0001')


def test_operator_account_gate_and_reply_target(sender):
    s, archive, key, _, _ = sender
    archive.set_sending('send-fixture', False)
    with pytest.raises(APIError, match='sending_disabled'):
        submit(s, key)
    archive.set_sending('send-fixture', True)
    with pytest.raises(APIError, match='not_found'):
        submit(s, key, reply_to='demo-tg-1')
    result = submit(s, key, reply_to=stable_id('msg', 'send-fixture', '100', '1'))
    assert result['status'] == 'queued'


def preview_of(s, key, **changes):
    return s.preview_message(key, account='send-fixture', chat_id=stable_id('chat', 'send-fixture', '100'),
        text='Exact synthetic text', **changes)


def queued_count(s):
    with s.pool.connection() as c:
        return c.execute('SELECT count(*) AS n FROM outgoing_messages').fetchone()['n']


# spec: ANYM-927
def test_a_silent_preview_says_so_and_its_digest_binds_silent_both_ways(sender):
    s, _, key, _, _ = sender
    silent, loud = preview_of(s, key, silent=True), preview_of(s, key)
    assert silent['silent'] is True and silent['render_policy']['silent'] is True
    assert 'silent' not in loud and 'silent' not in loud['render_policy']
    assert preview_of(s, key, silent=False) == loud
    assert silent['preview_digest'] != loud['preview_digest']
    with pytest.raises(APIError, match='preview_required'):
        submit(s, key, silent=True)
    with pytest.raises(APIError, match='preview_mismatch'):
        submit(s, key, silent=True, preview_digest=loud['preview_digest'])
    with pytest.raises(APIError, match='preview_mismatch'):
        submit(s, key, preview_digest=silent['preview_digest'])
    assert queued_count(s) == 0
    for bad in ('true', 1, None):
        with pytest.raises(APIError, match='invalid_parameters'):
            submit(s, key, silent=bad, preview_digest=silent['preview_digest'])
    receipt = submit(s, key, silent=True, preview_digest=silent['preview_digest'])
    assert receipt['status'] == 'queued'
    # An exact retry returns the same send; the same request_id sent loud is a conflict.
    assert submit(s, key, silent=True, preview_digest=silent['preview_digest'])['send_id'] == receipt['send_id']
    with pytest.raises(APIError, match='idempotency_conflict'):
        submit(s, key, preview_digest=silent['preview_digest'])
    with s.pool.connection() as c:
        row = c.execute('SELECT render_policy FROM outgoing_messages').fetchone()
    assert row['render_policy'] == {'link_preview': False, 'allow_lossy': False, 'silent': True}


# spec: ANYM-927
def test_silent_on_whatsapp_is_the_field_error_silent_unsupported_and_queues_nothing(sender):
    s, archive, _, _, _ = sender
    archive.register('wa-silent', 'whatsapp', 'Synthetic WhatsApp', discover=True)
    archive.ingest(event(account='wa-silent', platform='whatsapp', chat='fixture@c.us', provider_id='1',
        body='Existing synthetic message'))
    archive.set_sending('wa-silent', True)
    key = s.issue_key('wa-silent-agent', ['wa-silent'], permissions=['read', 'send'])
    chat = stable_id('chat', 'wa-silent', 'fixture@c.us')
    calls = [lambda: s.preview_message(key, account='wa-silent', chat_id=chat, text='Synthetic', silent=True),
             lambda: s.send_message(key, account='wa-silent', chat_id=chat, text='Synthetic',
                 request_id='wa-silent-request-1', silent=True, preview_digest='a' * 64)]
    for call in calls:
        with pytest.raises(APIError, match='silent_unsupported') as refused:
            call()
        assert refused.value.field_errors == [{'field': 'silent', 'reason': 'enum',
            'expected': 'false: WhatsApp has no silent send', 'allowed': [False]}]
    assert queued_count(s) == 0
    assert s.preview_message(key, account='wa-silent', chat_id=chat, text='Synthetic', silent=False)['account'] == 'wa-silent'


def test_worker_sends_once_and_rechecks_revocation(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    calls = []
    async def provider(job):
        calls.append(job)
        return {'provider_message_id': '42'}
    worker = OutgoingWorker(state, provider)
    result = submit(s, key)
    assert asyncio.run(worker.once()) is True
    assert asyncio.run(worker.once()) is False
    receipt = s.get_send_status(key, result['send_id'])
    assert receipt['status'] == 'sent' and receipt['provider_message_id'] == '42'
    assert receipt['recipient_delivery_confirmed'] is False
    assert receipt['agent_send']['principal']=='send-agent'
    assert receipt['agent_send']['request_id']=='synthetic-request-0001'
    assert receipt['agent_send']['owner_approval_verified_by_server'] is False
    assert submit(s, key)['send_id'] == result['send_id']
    assert len(calls) == 1
    queued = s.send_message(key, account='send-fixture', chat_id=result['chat_id'],
        text='Second synthetic', request_id='synthetic-request-0002')
    s.set_access('send-agent', permissions=['read'])
    asyncio.run(worker.once())
    assert len(calls) == 1
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s',
            (queued['send_id'],)).fetchone()['status'] == 'cancelled'


def test_agent_send_provenance_is_scoped_and_bounded_in_compact_reads(sender):
    from messaging_service.outgoing import OutgoingWorker
    from messaging_service.presentation import render
    s,archive,key,_,state=sender
    action=submit(s,key)
    async def provider(job):return {'provider_message_id':'42'}
    asyncio.run(OutgoingWorker(state,provider).once())
    mid=archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing',
        body='Exact synthetic text'))['message']
    reader=s.issue_key('scoped-reader',['send-fixture'],chats=[stable_id('chat','send-fixture','100')])
    result=s.get_context(reader,mid,before_count=0,after_count=0)
    compact=render('get_context',result,'compact')['message']
    assert compact['agent_send']=={'send_id':action['send_id'],'principal':'send-agent',
        'basis':'confirmed_outbox_receipt','body_matches_sent_text':True,'owner_approval_verified_by_server':False}
    assert 'key_id' not in str(compact['agent_send']) and 'request_id' not in compact['agent_send']
    assert next(r for r in render('search',s.search(reader),'compact')['items'] if r['id']==mid)['agent_send']['send_id']==action['send_id']
    updates=render('get_updates',s.get_updates(reader,'send-provenance'),'compact')['items']
    assert next(r for r in updates if r['message']==mid)['agent_send']['send_id']==action['send_id']
    archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing',
        body='edited synthetic text',updated_at='2026-10-01T12:01:00Z'))
    assert not s.get_context(reader,mid)['message']['agent_send']['body_matches_sent_text']
    s.set_access('scoped-reader',chats=[])
    with pytest.raises(APIError,match='not_found'):s.get_context(reader,mid)


def test_duplicate_provider_receipts_do_not_attribute_an_agent_by_guessing(sender):
    from messaging_service.outgoing import OutgoingWorker
    s,archive,key,_,state=sender
    submit(s,key)
    s.send_message(key,account='send-fixture',chat_id=stable_id('chat','send-fixture','100'),
        text='Second fixture',request_id='second-synthetic-request')
    async def provider(job):return {'provider_message_id':'42'}
    worker=OutgoingWorker(state,provider)
    asyncio.run(worker.once());asyncio.run(worker.once())
    mid=archive.ingest(event(account='send-fixture',provider_id='42',direction='outgoing',body='Exact synthetic text'))['message']
    assert s.get_context(key,mid)['message']['agent_send']=={'basis':'ambiguous_outbox_receipts',
        'candidate_count':2,'owner_approval_verified_by_server':False}


def test_ambiguous_provider_failure_never_retries(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    calls = []
    async def provider(job):
        calls.append(job)
        raise TimeoutError('synthetic secret diagnostic must not be returned')
    worker = OutgoingWorker(state, provider)
    queued = submit(s, key)
    asyncio.run(worker.once())
    assert s.get_send_status(key, queued['send_id'])['status'] == 'unknown'
    asyncio.run(worker.once())
    assert len(calls) == 1
    assert submit(s, key)['status'] == 'unknown'


def test_crash_after_dispatch_is_unknown_not_replayed(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    queued = submit(s, key)
    with s.pool.connection() as c:
        c.execute("UPDATE outgoing_messages SET status='dispatching',lease_epoch=0 WHERE id=%s",
                  (queued['send_id'],))
    calls = []
    async def provider(job): calls.append(job)
    asyncio.run(OutgoingWorker(state, provider).once())
    assert not calls
    assert s.get_send_status(key, queued['send_id'])['status'] == 'unknown'


def test_busy_provider_gate_leaves_send_queued(sender):
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    queued = submit(s, key)
    calls = []
    async def scenario():
        gate = asyncio.Lock()
        async def provider(job):
            calls.append(job)
            return {'provider_message_id':'43'}
        await gate.acquire()
        task = asyncio.create_task(OutgoingWorker(state, provider, gate).once())
        await asyncio.sleep(.05)
        assert not calls and s.get_send_status(key,queued['send_id'])['status']=='queued'
        gate.release()
        assert await task is True
    asyncio.run(scenario())
    assert len(calls)==1 and s.get_send_status(key,queued['send_id'])['status']=='sent'


@pytest.mark.parametrize('change,expected', [('expiry','expired'),('revoke','cancelled'),('disable','cancelled')])
def test_pending_send_rechecks_expiry_key_and_account(sender,change,expected):
    from messaging_service.outgoing import OutgoingWorker
    s, archive, key, _, state = sender
    queued = submit(s,key)
    if change=='expiry':
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET expires_at=now()-interval '1 second'")
        assert s.get_send_status(key,queued['send_id'])['status']=='expired'
    elif change=='revoke':s.revoke(key)
    else:archive.set_sending('send-fixture',False)
    calls = []
    async def provider(job):calls.append(job)
    assert asyncio.run(OutgoingWorker(state,provider).once()) is True
    assert not calls
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM outgoing_messages WHERE id=%s',
            (queued['send_id'],)).fetchone()['status']==expected


def test_send_works_with_restricted_gateway_and_collector_roles(sender):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    import psycopg
    import messaging_service.store as module
    from messaging_service.outgoing import OutgoingWorker
    s, _, key, _, state = sender
    roles={'gateway':'send_test_gateway','collector':'send_test_collector'}
    class RestrictedPool:
        def __init__(self,role):self.role=role
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute('SET LOCAL ROLE '+self.role)
                yield c
    for kind,role in roles.items():
        ddl=Path(module.__file__).with_name(kind+'_role.sql').read_text().replace('messaging_'+kind,role)
        with s.pool.connection() as c:c.execute(ddl)
    gateway=copy(s);gateway.pool=RestrictedPool(roles['gateway'])
    collector=copy(s);collector.pool=RestrictedPool(roles['collector'])
    restricted_state=CollectorState(Archive(collector),state.account,state.lease)
    calls=[]
    async def provider(job):
        calls.append(job)
        return {'provider_message_id':'44'}
    try:
        queued=submit(gateway,key)
        assert asyncio.run(OutgoingWorker(restricted_state,provider).once())
        assert gateway.get_send_status(key,queued['send_id'])['status']=='sent'
        assert len(calls)==1
        with s.pool.connection() as c:
            c.execute("UPDATE outgoing_messages SET status='dispatching',lease_epoch=0 WHERE id=%s",(queued['send_id'],))
        assert gateway.get_send_status(key,queued['send_id'])['status']=='unknown'
        for pool,sql in [(gateway.pool,"UPDATE outgoing_messages SET status='sent'"),
                         (collector.pool,'SELECT digest FROM api_keys'),
                         (collector.pool,'UPDATE account_policies SET send_enabled=true')]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with pool.connection() as c:c.execute(sql)
    finally:
        for role in roles.values():
            with s.pool.connection() as c:
                c.execute('DROP OWNED BY '+role);c.execute('DROP ROLE '+role)


def test_mcp_and_rest_send_contract(sender):
    s, _, key, _, _ = sender
    with TestClient(create_app(s, ['testserver'])) as client:
        headers = {'Authorization': 'Bearer ' + key}
        args = {'account':'send-fixture','chat_id':stable_id('chat','send-fixture','100'),
                'text':'Synthetic HTTP message','request_id':'synthetic-http-request'}
        response = client.post('/v1/send_message', headers=headers, json=args)
        assert response.status_code == 200
        receipt = response.json()
        assert receipt['status'] == 'queued'
        assert client.post('/v1/get_send_status', headers=headers,
            json={'send_id':receipt['send_id']}).json()['send_id'] == receipt['send_id']
        assert client.post('/v1/send_message', headers=headers,
            json={**args,'approved':True}).status_code == 400
        tools = client.post('/mcp', headers={**headers,'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}).json()['result']['tools']
        tool = next(t for t in tools if t['name'] == 'send_message')
        assert tool['annotations']['readOnlyHint'] is False
        assert tool['annotations']['openWorldHint'] is True
        assert 'confirm' in tool['description'].lower()


# spec: ANYM-927
def test_mcp_send_message_and_preview_take_silent_with_no_schema_default(sender):
    s, _, key, _, _ = sender
    headers = {'Authorization': 'Bearer ' + key}
    with TestClient(create_app(s, ['testserver'])) as client:
        tools = client.post('/mcp', headers={**headers,'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}).json()['result']['tools']
        args = {'account':'send-fixture','chat_id':stable_id('chat','send-fixture','100'),'text':'Synthetic silent'}
        preview = client.post('/v1/preview_message', headers=headers, json={**args,'silent':True}).json()
        sent = client.post('/v1/send_message', headers=headers, json={**args,'silent':True,
            'request_id':'synthetic-silent-http','preview_digest':preview['preview_digest']})
    for name in ('preview_message', 'send_message'):
        schema = next(t for t in tools if t['name'] == name)['inputSchema']['properties']['silent']
        assert 'default' not in schema and 'notification sound' in schema['description']
    assert preview['silent'] is True and sent.status_code == 200 and sent.json()['status'] == 'queued'


# spec: ANYM-927
def test_a_silent_telegram_send_reaches_telethon_as_silent():
    from types import SimpleNamespace
    from messaging_service.telegram_collector import TelethonProvider
    calls = []
    class Client:
        async def send_message(self, chat, text, **options):
            calls.append(options)
            return SimpleNamespace(id=42, chat_id=100)
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    text = 'Synthetic quiet'
    def job(policy):
        artifact = {'format':'plain','source_text':text,'rendered_text':text,'provider_text':text,'entities':[],
            'provider_payload':{'text':text,'entities':[],'link_preview':False},'warnings':[],'links':[],
            'utf16_units':len(text),'renderer_version':'anymsg-renderer-1'}
        return {'provider_chat':'100','body':text,'source_text':text,'source_format':'plain','render_artifact':artifact,
            'render_policy':policy,'preview_digest':'f'*64,'expires_at':datetime.now(timezone.utc).timestamp()+300}
    asyncio.run(provider.send_text(job({'link_preview':False,'allow_lossy':False,'silent':True})))
    asyncio.run(provider.send_text(job({'link_preview':False,'allow_lossy':False})))
    assert calls[0]['silent'] is True and 'silent' not in calls[1]
    assert {k: v for k, v in calls[0].items() if k != 'silent'} == calls[1]
    # A stored policy may carry silent only as true; anything else is refused before the provider.
    from messaging_service.outgoing import SendRejected, dispatch_payload
    for policy in ({'link_preview':False,'allow_lossy':False,'silent':False},
                   {'link_preview':False,'allow_lossy':False,'silent':'yes'}):
        with pytest.raises(SendRejected):
            asyncio.run(provider.send_text(job(policy)))
    assert len(calls) == 2
    with pytest.raises(SendRejected):
        dispatch_payload(job({'link_preview':False,'allow_lossy':False,'silent':True}), 'whatsapp')


def test_telegram_send_plain_text_through_existing_client():
    from types import SimpleNamespace
    from messaging_service.telegram_collector import TelethonProvider
    calls = []
    class Client:
        async def send_message(self, chat, text, **options):
            calls.append((chat, text, options))
            return SimpleNamespace(id=42, chat_id=100)
    provider = TelethonProvider.__new__(TelethonProvider)
    provider.client = Client()
    result = asyncio.run(provider.send_text({'provider_chat':'100','body':'Synthetic **literal**',
        'reply_provider_id':'1','expires_at':datetime.now(timezone.utc).timestamp()+300}))
    assert result['provider_message_id'] == '42'
    assert calls == [(100, 'Synthetic **literal**', {
        'parse_mode':None,'link_preview':False,'reply_to':1,'clear_draft':False})]
