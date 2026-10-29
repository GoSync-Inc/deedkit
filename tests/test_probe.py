from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import threading

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.store import APIError, digest


def test_scoped_accounts_and_search(seeded):
    s,a,b=seeded
    assert len(s.capabilities(a)['accounts'])==2
    assert len(s.capabilities(b)['accounts'])==1
    assert len(s.search(a)['items'])==3
    assert len(s.search(b)['items'])==2
    assert s.search(a,'PRIVATE_SCOPE_SENTINEL')['items']==[]
    assert s.search(b,account='whatsapp-demo-1')['items']==[]
    assert len(s.list_chats(b)['items'])==1


def test_filters_pagination_and_unicode(seeded):
    s,a,b=seeded
    assert s.search(a,'Маяк')['items'][0]['id']=='demo-tg-1'
    assert s.search(a,'Atlas')['items'][0]['id']=='demo-wa-1'
    assert s.search(a,sender='demo-person-2')['items'][0]['id']=='demo-tg-2'
    assert [m['id'] for m in s.search(a,from_time='2026-10-02T12:00:00Z',to_time='2026-10-03T12:00:00Z')['items']]==['demo-tg-2']
    page=s.search(a,limit=1)
    ids=[page['items'][0]['id']]
    while page['next_cursor']:
        page=s.search(a,after=page['next_cursor'],limit=1)
        ids.extend(m['id'] for m in page['items'])
    assert ids==['demo-tg-1','demo-tg-2','demo-wa-1']


@pytest.mark.parametrize('method,args',[
    ('get_context',{'message_id':'demo-private-1'}),
    ('get_context',{'message_id':'demo-wa-1'}),
    ('search',{'after':'demo-private-1'}),
    ('get_attachment',{'attachment_id':'missing'}),
])
def test_direct_reference_acl(seeded,method,args):
    s,a,b=seeded
    with pytest.raises(APIError,match='not_found'): getattr(s,method)(b,**args)


@pytest.mark.parametrize('params',[
    {'limit':0},{'limit':101},{'limit':True},{'query':'a'*201},
    {'from_time':'2026-10-01'},{'from_time':'bad'},
    {'from_time':'2026-10-04T00:00:00Z','to_time':'2026-10-01T00:00:00Z'},
])
def test_query_validation(seeded,params):
    s,a,b=seeded
    with pytest.raises(APIError): s.search(a,**params)


def test_download_hash_scope_and_revocation(seeded):
    s,a,b=seeded
    item=s.get_attachment(b,'demo-file-1')
    grant=item['download_url'].rsplit('/',1)[-1]
    # download() also returns the admitted binding now; assertions are unchanged.
    metadata,path,_=s.download(grant)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==item['sha256']
    with pytest.raises(APIError): s.download(item['sha256'])
    s.revoke(b)
    with pytest.raises(APIError,match='not_found'): s.download(grant)
    with pytest.raises(APIError,match='unauthorized'): s.search(b)
    assert s.search(a)['items']


def test_grant_expiry_and_missing_original(seeded):
    s,a,b=seeded
    item=s.get_attachment(a,'demo-file-1'); grant=item['download_url'].rsplit('/',1)[-1]
    with s.pool.connection() as c:
        c.execute("UPDATE download_grants SET expires_at=now()-interval '1 second'")
    with pytest.raises(APIError): s.download(grant)
    item=s.get_attachment(a,'demo-file-1'); grant=item['download_url'].rsplit('/',1)[-1]
    path=s.files/item['digest']; original=path.read_bytes()
    try:
        path.unlink()
        with pytest.raises(APIError,match='attachment_unavailable'): s.download(grant)
    finally: path.write_bytes(original)


def test_started_transfer_outlives_grant_ttl_but_not_revocation(seeded):
    s,a,b=seeded
    def admitted(token):
        grant=s.get_attachment(token,'demo-file-1')['download_url'].rsplit('/',1)[-1]
        return grant,s.download(grant)[2]
    grant,binding=admitted(a)
    other,other_binding=admitted(b)
    with s.pool.connection() as c:
        c.execute("UPDATE download_grants SET expires_at=now()-interval '1 second'")
    s.get_attachment(a,'demo-file-1')  # Its cleanup deletes the expired grant rows.
    with s.pool.connection() as c:
        assert c.execute('SELECT 1 FROM download_grants WHERE digest=%s',(digest(grant),)).fetchone() is None
    with pytest.raises(APIError,match='not_found'): s.download(grant)  # No new transfer.
    row,path,again=s.download(grant,active=binding)
    assert row['id']=='demo-file-1' and path.is_file() and again==binding
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET active=false WHERE id='demo-file-1'")
    with pytest.raises(APIError,match='not_found'): s.download(grant,active=binding)
    with s.pool.connection() as c:
        c.execute("UPDATE attachments SET active=true WHERE id='demo-file-1'")
    assert s.download(grant,active=binding)[1].is_file()
    # The file stays visible to the narrower scope, but the admitted scope version is stale.
    s.set_access('agent-a',chats=['telegram-demo-chat'])
    assert s.get_attachment(a,'demo-file-1')['status']=='ready'
    with pytest.raises(APIError,match='not_found'): s.download(grant,active=binding)
    assert s.download(other,active=other_binding)[1].is_file()
    s.revoke(b)
    with pytest.raises(APIError,match='not_found'): s.download(other,active=other_binding)


def test_head_revalidates_a_started_transfer_after_grant_expiry(seeded, monkeypatch):
    from messaging_service.app import ActiveDownloads
    s,a,b=seeded
    auth={'Authorization':'Bearer '+b}
    def expire():
        with s.pool.connection() as c:
            c.execute("UPDATE download_grants SET expires_at=now()-interval '1 second'")
    with TestClient(create_app(s,['testserver'])) as client:
        def link():
            return client.post('/v1/get_attachment',headers=auth,json={'attachment_id':'demo-file-1'}).json()['download_url']
        started,unused=link(),link()
        assert client.get(started).status_code==200
        expire()
        # A relay may still be delivering the finished transfer's buffered bytes.
        assert client.head(started).status_code==200
        assert client.get(started).status_code==404
        assert client.head(unused).status_code==404
        monkeypatch.setattr(ActiveDownloads,'LINGER',0)
        finished=link()
        assert client.get(finished).status_code==200
        expire()
        assert client.head(finished).status_code==404
        s.revoke(b)
        assert client.head(started).status_code==404


def test_active_download_bindings_cover_running_and_recent_transfers(monkeypatch):
    from messaging_service import app as module
    clock=[1000.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    transfers=module.ActiveDownloads()
    binding=('key-digest',1,'file')
    done=transfers.start('g'*43,binding)
    clock[0]+=86400
    assert transfers.binding('g'*43)==binding  # Running: no time limit.
    done()
    clock[0]+=module.ActiveDownloads.LINGER-1
    assert transfers.binding('g'*43)==binding
    clock[0]+=2
    assert transfers.binding('g'*43) is None and transfers.binding('h'*43) is None
    monkeypatch.setattr(module.ActiveDownloads,'LIMIT',3)
    transfers.start('r'*43,binding)
    for n in range(5):
        transfers.start(str(n)*43,binding)()
    assert len(transfers.entries)==3
    assert transfers.binding('r'*43)==binding and transfers.binding('4'*43)==binding
    assert transfers.binding('0'*43) is None


def test_independent_consumers_replay_and_get_does_not_ack(seeded):
    s,a,b=seeded
    page=s.get_updates(a,'main',limit=1)
    again=s.get_updates(a,'main',limit=1)
    assert page['items']==again['items']
    assert len(s.get_updates(b,'main')['items'])==2
    assert s.ack_updates(a,'main',page['receipt'],page['ack_version'])['ack_version']==1
    assert s.ack_updates(a,'main',page['receipt'],page['ack_version'])['replayed']
    assert len(s.get_updates(a,'main')['items'])==2
    assert len(s.get_updates(a,'other-task')['items'])==3
    assert len(s.get_updates(b,'main')['items'])==2
    with pytest.raises(APIError,match='stale_ack'):
        s.ack_updates(a,'main',again['receipt'],again['ack_version'])


def test_receipt_cannot_be_forged_or_used_by_other_agent(seeded):
    s,a,b=seeded
    page=s.get_updates(a,'main')
    s.get_updates(b,'main')
    for token,consumer,receipt,version in [(b,'main',page['receipt'],0),
        (a,'other',page['receipt'],0),(a,'main','forged',0),(a,'main',page['receipt'],50)]:
        with pytest.raises(APIError): s.ack_updates(token,consumer,receipt,version)


def test_scope_change_invalidates_receipts_and_downloads(seeded):
    s,a,b=seeded
    page=s.get_updates(a,'main')
    grant=s.get_attachment(a,'demo-file-1')['download_url'].rsplit('/',1)[-1]
    new=s.issue_key('agent-a',['whatsapp-demo-1'])
    with pytest.raises(APIError): s.ack_updates(new,'main',page['receipt'],0)
    with pytest.raises(APIError): s.download(grant)
    assert len(s.get_updates(new,'main')['items'])==1
    assert len(s.search(a)['items'])==1  # scopes belong to principal, not stale key claims


def test_rotation_preserves_checkpoint(seeded):
    s,a,b=seeded
    page=s.get_updates(a,'main')
    s.ack_updates(a,'main',page['receipt'],0)
    new=s.issue_key('agent-a',['telegram-demo','whatsapp-demo-1'])
    s.revoke(a)
    assert s.get_updates(new,'main')['items']==[]


def test_concurrent_ack_only_one_page_advances(seeded):
    s,a,b=seeded
    pages=[s.get_updates(a,'main',limit=1) for _ in range(2)]
    def ack(page):
        try: return s.ack_updates(a,'main',page['receipt'],0)['acked']
        except APIError as error: return error.code
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(ack,pages))
    assert results.count(True)==1 and results.count('stale_ack')==1


def test_commit_order_and_rollback_do_not_skip_events(seeded):
    s,a,b=seeded
    page=s.get_updates(a,'main'); s.ack_updates(a,'main',page['receipt'],0)
    started=threading.Event()
    def second():
        with s.pool.connection() as c:
            started.set()
            return s.append_event(c,'telegram-demo','demo-tg-2','message.edited')
    with ThreadPoolExecutor(1) as pool:
        with s.pool.connection() as first:
            first_id=s.append_event(first,'telegram-demo','demo-tg-1','message.created')
            later=pool.submit(second); assert started.wait(2)
            during=s.get_updates(a,'main')
            assert during['items']==[]
            assert not later.done()
        second_id=later.result(timeout=6)
    assert second_id==first_id+1
    assert [e['kind'] for e in s.get_updates(a,'main')['items']]==['message.created','message.edited']
    with pytest.raises(RuntimeError):
        with s.pool.connection() as c:
            s.append_event(c,'telegram-demo','demo-tg-1','message.deleted')
            raise RuntimeError('rollback')
    assert len(s.get_updates(a,'main')['items'])==2


def test_restart_persistence_and_seed_idempotence(seeded):
    s,a,b=seeded
    page=s.get_updates(a,'main'); s.ack_updates(a,'main',page['receipt'],0)
    s.seed(); s.migrate()
    from messaging_service.store import Store
    restarted=Store(s.pool.conninfo,s.files,s.public_url)
    try:
        assert restarted.get_updates(a,'main')['items']==[]
        assert len(restarted.search(a)['items'])==3
    finally: restarted.close()


def test_rest_boundary_and_files(seeded):
    s,a,b=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        assert client.get('/healthz').json()=={'status':'alive','version':s.capabilities(b)['version']}
        assert client.post('/v1/search',json={}).status_code==401
        auth={'Authorization':'Bearer '+b}
        response=client.post('/v1/search',headers=auth,json={})
        assert response.status_code==200 and len(response.json()['items'])==2
        assert response.headers['cache-control']=='no-store'
        assert client.post('/v1/search',headers={**auth,'Origin':'https://evil.invalid'},json={}).status_code==403
        assert client.post('/v1/search',headers={**auth,'Host':'evil.invalid'},json={}).status_code==403
        assert client.post('/v1/search',headers=auth,json={'token':a}).status_code==400
        assert client.post('/v1/search',headers=auth,content=b'x'*17000).status_code==413
        assert client.post('/v1/send',headers=auth,json={}).status_code==404
        attachment=client.post('/v1/get_attachment',headers=auth,json={'attachment_id':'demo-file-1'}).json()
        result=client.get(attachment['download_url'])
        assert result.status_code==200
        assert hashlib.sha256(result.content).hexdigest()==attachment['sha256']
        assert result.headers['referrer-policy']=='no-referrer'
        s.revoke(b)
        assert client.get(attachment['download_url']).status_code==404


def test_legacy_mcp_wire_protocol_and_scopes(seeded):
    s,a,b=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream',
                 'MCP-Protocol-Version':'2025-11-25'}
        def rpc(method,params=None):
            return client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
        init=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'probe-legacy-test','version':'1'}})
        assert init.status_code==200
        assert init.json()['result']['serverInfo']['version']==s.capabilities(b)['version']
        tools=rpc('tools/list').json()['result']['tools']
        assert len(tools)==54
        send=next(t for t in tools if t['name']=='send_message')
        assert send['annotations']['readOnlyHint'] is False and send['annotations']['openWorldHint'] is True
        value=rpc('tools/call',{'name':'search','arguments':{'query':'Atlas'}}).json()['result']
        assert not value.get('isError')
        assert value['structuredContent']['items']==[]
        denied=rpc('tools/call',{'name':'get_context','arguments':{'message_id':'demo-private-1'}}).json()['result']
        assert denied['isError']
        assert 'PRIVATE_SCOPE_SENTINEL' not in str(denied)


def test_sync_status_rest_serializes_real_timestamps(seeded):
    s,a,_=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        response=client.post('/v1/get_sync_status',json={},headers={'Authorization':'Bearer '+a})
    assert response.status_code==200
    result=response.json()
    assert result['sources'] and all(isinstance(row['oldest_message_at'],str) for row in result['sources'])
