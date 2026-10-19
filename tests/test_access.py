"""Cross-transport authorization, transaction safety, and append-only audit."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
import json
from pathlib import Path
from threading import Event
import time
from uuid import UUID

import psycopg
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.policy import lock_principal
from messaging_service.store import APIError, digest


def hidden_chat(s):
    with s.pool.connection() as c:
        c.execute("INSERT INTO chats VALUES('hidden-chat','telegram-demo','HIDDEN_CHAT_SENTINEL')")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            VALUES('hidden-message','telegram-demo','hidden-chat','hidden-1','hidden-person',
                   'HIDDEN_BODY_SENTINEL','2026-10-02T13:00:00Z')""")
        c.execute("""INSERT INTO attachments(id,message,digest,filename,mime,bytes)
            SELECT 'hidden-file','hidden-message',digest,'HIDDEN_FILE_SENTINEL',mime,bytes
            FROM attachments WHERE id='demo-file-1'""")
        c.execute("""INSERT INTO media_jobs(id,account,message,descriptor)
            VALUES('hidden-pending','telegram-demo','hidden-message','{"filename":"HIDDEN_PENDING_SENTINEL"}')""")
        c.execute("""INSERT INTO source_status(account,status,last_ingest_at,coverage,detail_code)
            VALUES('telegram-demo','healthy',now(),'sample','HIDDEN_STATUS_SENTINEL')""")
        s.append_event(c,'telegram-demo','hidden-message')


def test_chat_scope_covers_every_read_file_and_event_route(seeded):
    s,a,b=seeded;hidden_chat(s)
    # A previously issued link must also lose access after the policy changes.
    old=s.get_attachment(b,'hidden-file')['download_url'].rsplit('/',1)[-1]
    s.set_access('agent-b',chats=['telegram-demo-chat'])
    outputs=[s.capabilities(b),s.list_chats(b),s.search(b),
             s.get_messages(b,'telegram-demo-chat'),s.get_context(b,'demo-tg-2'),
             s.get_updates(b,'main',limit=2),s.get_attachment(b,'demo-file-1')]
    assert 'HIDDEN_' not in json.dumps(outputs)
    assert 'hidden-chat' not in json.dumps(outputs)
    assert outputs[0]['sources'][0]['last_ingest_at'] is None
    assert outputs[0]['coverage']=='chat_scope_only'
    assert not outputs[5]['has_more'] and len(outputs[5]['items'])==2
    with pytest.raises(APIError,match='not_found'):s.search(b,chat='hidden-chat')  # ANYM-253
    assert s.search(b,sender='hidden-person')['items']==[]
    assert s.get_messages(b,'hidden-chat')['items']==[]
    for method,args in [('get_context',('hidden-message',)),('search',()),
                        ('get_attachment',('hidden-file',)),('get_attachment',('hidden-pending',)),
                        ('list_chats',())]:
        kw={'after':'hidden-message'} if method=='search' else {'after':'hidden-chat'} if method=='list_chats' else {}
        with pytest.raises(APIError,match='not_found'):getattr(s,method)(b,*args,**kw)
    with pytest.raises(APIError,match='not_found'):s.download(old)
    s.set_access('agent-b',chats=[])
    assert s.capabilities(b)['accounts']==[]
    assert s.list_chats(b)['items']==s.search(b)['items']==s.get_updates(b,'main')['items']==[]
    s.set_access('agent-b',chats=None)
    assert len(s.search(b)['items'])==3


def test_chat_listing_pagination_is_scoped(seeded):
    s,a,b=seeded;hidden_chat(s)
    page=s.list_chats(b,limit=1)
    assert page['next_cursor']==page['items'][0]['id']
    next_page=s.list_chats(b,after=page['next_cursor'],limit=1)
    assert next_page['next_cursor'] is None
    assert {page['items'][0]['id'],next_page['items'][0]['id']}=={'telegram-demo-chat','hidden-chat'}


@pytest.mark.parametrize('permissions', [[],['read'],['download'],['cursor-write']])
def test_operation_permissions_are_enforced_by_rest_and_mcp(seeded,permissions,caplog):
    s,a,b=seeded;s.set_access('agent-b',permissions=permissions)
    with TestClient(create_app(s,['testserver'])) as client:
        auth={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream',
              'MCP-Protocol-Version':'2025-11-25'}
        calls=[('search',{},'read'),('get_attachment',{'attachment_id':'demo-file-1'},'download'),
               ('ack_updates',{'consumer':'main','receipt':'SECRET_INVALID_RECEIPT','expected_version':0},'cursor-write')]
        for method,params,permission in calls:
            rest=client.post('/v1/'+method,headers=auth,json=params)
            rpc=client.post('/mcp',headers=auth,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':method,'arguments':params}}).json()['result']
            if permission not in permissions:
                assert rest.status_code==403 and rest.json()['error']=='forbidden'
                assert rpc['isError'] and 'forbidden' in json.dumps(rpc)
            elif method=='ack_updates':
                assert rest.status_code==409 and 'invalid_receipt' in json.dumps(rpc)
            else:assert rest.status_code==200 and not rpc.get('isError')
        caps=client.post('/v1/capabilities',headers=auth,json={}).json()
        assert caps['permissions']==sorted(permissions)
        assert client.post('/v1/set_access',headers=auth,json={}).status_code==404
    assert b not in caplog.text and 'SECRET_INVALID_RECEIPT' not in caplog.text
    assert 'Traceback' not in caplog.text


def test_scope_revisions_invalidate_receipts_and_grants_but_rotation_preserves_state(seeded):
    s,a,b=seeded
    s.set_access('agent-b',chats=['telegram-demo-chat'])
    caps=s.capabilities(b);page=s.get_updates(b,'main')
    grant=s.get_attachment(b,'demo-file-1')['download_url'].rsplit('/',1)[-1]
    rotated=s.issue_key('agent-b',['telegram-demo'])
    assert s.capabilities(rotated)['scope_version']==caps['scope_version']
    assert s.capabilities(rotated)['chat_scope']==caps['chat_scope']
    s.ack_updates(rotated,'main',page['receipt'],0)
    assert s.get_updates(rotated,'main')['items']==[]
    s.set_access('agent-b',enabled=False)
    with pytest.raises(APIError,match='unauthorized'):s.search(b)
    with pytest.raises(APIError,match='not_found'):s.download(grant)
    disabled_rotation=s.issue_key('agent-b',['telegram-demo'])
    with pytest.raises(APIError,match='unauthorized'):s.capabilities(disabled_rotation)
    s.set_access('agent-b',enabled=True)
    with pytest.raises(APIError,match='invalid_receipt'):s.ack_updates(rotated,'main',page['receipt'],0)
    with pytest.raises(APIError,match='not_found'):s.download(grant)
    assert len(s.get_updates(rotated,'main')['items'])==2
    kid=s.capabilities(b)['credential_id'];assert str(UUID(kid))==kid
    assert s.revoke(credential_id=kid)==1
    with pytest.raises(APIError,match='unauthorized'):s.search(b)
    assert s.search(rotated)['items']
    keys=s.list_keys('agent-b')
    assert len(keys)==3 and all(set(k)=={'id','principal','expires_at','revoked','permissions','permission_version'} for k in keys)
    assert next(k for k in keys if k['id']==kid)['revoked']


def test_audit_denials_commit_without_attempted_writes_and_never_store_request_content(seeded):
    s,a,b=seeded
    with pytest.raises(APIError,match='invalid_query'):
        with s.authorized(b,'search') as (c,p):
            c.execute("INSERT INTO consumers(principal,name,scope_version) VALUES('agent-b','must-rollback',1)")
            p['_audit_targets']=['SHOULD_NOT_BE_LOGGED']
            raise APIError('invalid_query')
    with s.pool.connection() as c:
        assert not c.execute("SELECT 1 FROM consumers WHERE name='must-rollback'").fetchone()
    s.search(b,'PRIVATE_QUERY_SENTINEL')
    for method,args in [('get_context',('PRIVATE_INVALID_ID_SENTINEL',)),
                        ('download',('PRIVATE_INVALID_GRANT_SENTINEL',))]:
        with pytest.raises(APIError):getattr(s,method)(*(args if method=='download' else (b,*args)))
    rows=s.audit_tail(limit=100);encoded=json.dumps(rows)
    for secret in [b,digest(b),'PRIVATE_QUERY_SENTINEL','PRIVATE_INVALID_ID_SENTINEL',
                   'PRIVATE_INVALID_GRANT_SENTINEL','SHOULD_NOT_BE_LOGGED']:
        assert secret not in encoded
    denial=next(r for r in rows if r['error_code']=='invalid_query')
    assert denial['principal']=='agent-b' and denial['outcome']=='denied' and denial['resource_ids']==[]
    assert all(r['actor']=='operator' for r in rows if r['operation']=='issue_key')


def test_audit_failure_rolls_back_ack_and_surfaces_failure(seeded):
    s,a,b=seeded;page=s.get_updates(b,'main')
    with s.pool.connection() as c:
        c.execute("""CREATE FUNCTION reject_test_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.operation='ack_updates' THEN RAISE EXCEPTION 'audit_unavailable'; END IF;
            RETURN NEW; END $$""")
        c.execute('CREATE TRIGGER reject_test_audit BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION reject_test_audit()')
    try:
        with pytest.raises(psycopg.Error,match='audit_unavailable'):s.ack_updates(b,'main',page['receipt'],0)
        assert s.get_updates(b,'main')['ack_version']==0
    finally:
        with s.pool.connection() as c:
            c.execute('DROP TRIGGER reject_test_audit ON audit_log');c.execute('DROP FUNCTION reject_test_audit()')
    assert s.ack_updates(b,'main',page['receipt'],0)['ack_version']==1


def wait_for_acl_waiter(s):
    deadline=time.monotonic()+2
    while time.monotonic()<deadline:
        with s.pool.connection() as c:
            if c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted").fetchone():return
        time.sleep(.01)
    pytest.fail('operator did not wait on the principal lock')


def test_inflight_read_serializes_scope_change_and_next_read_rechecks(seeded):
    s,a,b=seeded;started=Event()
    def change():started.set();return s.set_access('agent-b',chats=[])
    with ThreadPoolExecutor(1) as threads:
        with s.authorized(b,'search') as (c,p):
            changing=threads.submit(change);assert started.wait(1)
            wait_for_acl_waiter(s);assert not changing.done()
            assert len(s._search(c,p)['items'])==2
        assert changing.result(timeout=3)['chat_ids']==[]
    assert s.search(b)['items']==[]


def test_waiting_authentication_rechecks_revoked_key(seeded):
    s,a,b=seeded
    with ThreadPoolExecutor(1) as threads:
        with s.pool.connection() as c:
            lock_principal(c,'agent-b')
            pending=threads.submit(s.search,b)
            wait_for_acl_waiter(s)
            c.execute('UPDATE api_keys SET revoked=true WHERE digest=%s',(digest(b),))
        with pytest.raises(APIError,match='unauthorized'):pending.result(timeout=3)


def test_consumer_limit_is_atomic_and_scope_replay_does_not_use_new_slot(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("INSERT INTO consumers(principal,name,scope_version) SELECT 'agent-b','consumer-'||n,1 FROM generate_series(1,63) n")
    def create(name):
        try:s.get_updates(b,name);return 'ok'
        except APIError as e:return e.code
    with ThreadPoolExecutor(2) as threads:results=list(threads.map(create,['last-a','last-b']))
    assert sorted(results)==['consumer_limit','ok']
    s.set_access('agent-b',chats=['telegram-demo-chat'])
    assert s.get_updates(b,'consumer-1')['items']
    with pytest.raises(APIError,match='consumer_limit'):s.get_updates(b,'extra')


def test_gateway_database_role_can_append_audit_but_not_edit_it_or_acl(seeded):
    import messaging_service.store as module
    s,a,b=seeded;role='test_messaging_gateway'
    ddl=Path(module.__file__).with_name('gateway_role.sql').read_text().replace('messaging_gateway',role)
    with s.pool.connection() as c:c.execute(ddl)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute('SET LOCAL ROLE '+role)
                yield c
    gateway=copy(s);gateway.pool=RestrictedPool()
    try:
        assert gateway.search(b)['items']
        assert gateway.get_sync_status(b)['sources']
        page=gateway.get_updates(b,'main')
        assert gateway.ack_updates(b,'main',page['receipt'],0)['acked']
        attachment=gateway.get_attachment(b,'demo-file-1')
        assert gateway.download(attachment['download_url'].rsplit('/',1)[-1])[1].is_file()
        denied_sql=["SELECT * FROM audit_log", "DELETE FROM audit_log", "UPDATE audit_log SET outcome='ok'",
            "INSERT INTO audit_log(operation,outcome,actor) VALUES('issue_key','ok','operator')",
            "INSERT INTO audit_log(operation,outcome,db_role) VALUES('search','ok','probe_owner')",
            "INSERT INTO audit_log(operation,outcome,occurred_at) VALUES('search','ok',now())",
            "UPDATE principals SET enabled=true", "DELETE FROM api_keys", "ALTER TABLE messages ADD COLUMN forbidden text",
            "UPDATE collector_health SET operations='{}'", "UPDATE processing_profiles SET enabled=true",
            "SELECT owner FROM collector_leases"]
        for sql in denied_sql:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with gateway.pool.connection() as c:c.execute(sql)
        row=s.audit_tail(principal='agent-b',limit=1)[0]
        assert row['db_role']==role and row['actor']=='gateway'
    finally:
        with s.pool.connection() as c:c.execute('DROP OWNED BY '+role);c.execute('DROP ROLE '+role)


def test_edge_audit_is_bounded_and_redacts_bad_headers_and_rest_arguments(seeded,caplog):
    s,a,b=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        auth={'Authorization':'Bearer '+b}
        for _ in range(8):
            assert client.post('/v1/search',headers=auth,json={'wrong':'PRIVATE_BODY_SENTINEL'}).status_code==400
        assert client.post('/v1/search',headers={'Authorization':'Bearer PRIVATE_AUTH_SENTINEL'},json={}).status_code==401
    with s.pool.connection() as c:
        rows=c.execute("SELECT * FROM audit_log WHERE operation='http_boundary'").fetchall()
    assert len(rows)==5 and all(r['error_code']=='invalid_parameters' for r in rows)
    assert all(r['principal'] is None and r['resource_ids']==[] for r in rows)
    assert 'PRIVATE_BODY_SENTINEL' not in str(rows)+caplog.text
    assert 'PRIVATE_AUTH_SENTINEL' not in str(rows)+caplog.text


def test_sdk_invalid_protocol_and_tool_diagnostics_never_log_input(seeded,caplog):
    s,a,b=seeded;marker='PRIVATE_MCP_INPUT_SENTINEL'
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream',
                 'MCP-Protocol-Version':'2025-11-25'}
        payloads=[{'jsonrpc':'2.0','id':1,'method':marker,'params':{}},
            {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':marker,'arguments':{}}},
            {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'search','arguments':{'query':{'private':marker}}}},
            {'jsonrpc':'2.0','method':'notifications/progress','params':{'progressToken':marker,'progress':marker}},
            {'jsonrpc':'invalid','id':marker,'method':'tools/call','params':marker}]
        for payload in payloads:
            assert client.post('/mcp',headers=headers,json=payload).status_code in (200,202,400,422)
        assert client.post('/mcp',headers=headers,content='{invalid "'+marker+'"').status_code==400
    assert marker not in caplog.text and b not in caplog.text


def test_library_log_redaction_covers_lazy_loggers_and_exception_traces(caplog):
    import logging
    from messaging_service.security_logging import redact_library_logs
    redact_library_logs();factory=logging.getLogRecordFactory()
    redact_library_logs();assert logging.getLogRecordFactory() is factory
    for namespace in ['mcp.new_transport','uvicorn.error','psycopg.pool','httpx']:
        try:raise RuntimeError('PRIVATE_EXCEPTION_SENTINEL')
        except RuntimeError:logging.getLogger(namespace).exception('PRIVATE_LOG_SENTINEL %s','PRIVATE_ARGUMENT_SENTINEL')
        record=caplog.records[-1]
        assert record.levelno==logging.ERROR and record.exc_info is None and record.stack_info is None
        assert 'RuntimeError' in record.getMessage()
    assert 'PRIVATE_' not in caplog.text
