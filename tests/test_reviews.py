"""Shared review provenance, immutable attribution, scope and ACK independence."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import copy
import json
from pathlib import Path
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.archive import Archive
from messaging_service.backup import database_manifest
from messaging_service.store import APIError, Store


@pytest.fixture()
def reviewers(seeded):
    s,a,b=seeded
    s.set_access('agent-a',permissions=['read','review-write','cursor-write'])
    s.set_access('agent-b',permissions=['read','review-write','cursor-write'])
    return s,a,b


def payload(**changes):
    p={'message_id':'demo-tg-1','revision':1,'result_ref':'urn:personal-os:task:fixture',
       'request_id':str(uuid4())}
    p.update(changes)
    return p


def test_other_agent_sees_review_without_ack_or_provider_read(reviewers):
    s,a,b=reviewers
    before=s.get_updates(b,'independent')
    other=s.get_updates(a,'separate')
    result=s.record_review(a,**payload())
    assert result['review']['author']=='agent-a' and not result['review']['stale']
    assert result['review']['evidence'][0]['revision']==1
    shared=s.get_context(b,'demo-tg-1')['reviews']['items']
    assert shared==[result['review']]
    assert shared[0]['assertion']=='agent_reported_review'
    assert s.get_updates(b,'independent')['items']==before['items']
    assert s.get_updates(a,'separate')['items']==other['items']
    assert s.ack_updates(b,'independent',before['receipt'],before['ack_version'])['acked']
    assert s.get_updates(a,'separate')['ack_version']==0


def test_reviews_filter_all_provenance_before_pagination_and_cursor_lookup(reviewers):
    s,a,b=reviewers
    visible=[]
    for n in range(8):
        hidden=s.record_review(a,**payload(result_ref='https://example.invalid/HIDDEN_REVIEW_'+str(n),
            evidence=[{'message_id':'demo-wa-1','revision':1}]))['review']
        visible.append(s.record_review(a,**payload(result_ref='urn:personal-os:task:visible'+str(n)))['review'])
    page=s.get_reviews(b,'demo-tg-1',limit=3)
    assert page['items']==visible[:3]
    page2=s.get_reviews(b,'demo-tg-1',after=page['next_cursor'],limit=3)
    assert page2['items']==visible[3:6]
    page3=s.get_reviews(b,'demo-tg-1',after=page2['next_cursor'],limit=3)
    assert page3['items']==visible[6:] and page3['next_cursor'] is None
    context=s.get_context(b,'demo-tg-1')['reviews']
    assert context['items']==visible[:5] and context['next_cursor']==visible[4]['id']
    assert 'HIDDEN_REVIEW' not in json.dumps([page,page2,page3,context])
    with pytest.raises(APIError,match='not_found'):
        s.get_reviews(b,'demo-tg-1',after=hidden['id'])
    with pytest.raises(APIError,match='not_found'):
        s.get_reviews(b,'demo-tg-2',after=visible[0]['id'])
    # Narrowing the author's own scope also hides their old multi-source rows.
    s.set_access('agent-a',chats=['telegram-demo-chat'])
    assert s.get_reviews(a,'demo-tg-1')['items']==visible
    with pytest.raises(APIError,match='not_found'):
        s.record_review(a,**payload(evidence=[{'message_id':'demo-wa-1','revision':1}]))
    s.set_access('agent-a',chats=[])
    with pytest.raises(APIError,match='not_found'):s.get_reviews(a,'demo-tg-1')


def test_same_account_hidden_source_and_unauthorized_revision_are_not_disclosed(reviewers):
    s,a,b=reviewers
    with s.pool.connection() as c:
        c.execute("INSERT INTO chats VALUES('other-chat','telegram-demo','Other')")
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            VALUES('other-message','telegram-demo','other-chat','other','other','secret',now())""")
        eid=s.append_event(c,'telegram-demo','other-message')
        c.execute('INSERT INTO message_revisions(message,revision,snapshot) SELECT message,revision,snapshot FROM events WHERE id=%s',(eid,))
    s.record_review(a,**payload(evidence=[{'message_id':'other-message','revision':1}]))
    s.set_access('agent-b',chats=['telegram-demo-chat'])
    assert s.get_reviews(b,'demo-tg-1')['items']==[]
    with pytest.raises(APIError,match='not_found'):
        s.record_review(b,**payload(evidence=[{'message_id':'other-message','revision':999}]))
    with pytest.raises(APIError,match='revision_not_found'):s.record_review(b,**payload(revision=999))


def test_replay_rotation_immutable_conflicts_and_principal_attribution(reviewers):
    s,a,b=reviewers;p=payload()
    first=s.record_review(a,**p)
    rotated=s.issue_key('agent-a',['telegram-demo','whatsapp-demo-1'])
    assert s.record_review(rotated,**p)=={'review':first['review'],'replayed':True}
    for changes in [{'result_ref':'urn:personal-os:task:changed'},
                    {'evidence':[{'message_id':'demo-tg-2','revision':1}]},
                    {'message_id':'demo-tg-2'}]:
        with pytest.raises(APIError,match='review_conflict'):s.record_review(a,**{**p,**changes})
    # Same UUID on another principal is independent, never impersonates agent-a.
    assert s.record_review(b,**p)['review']['author']=='agent-b'
    s.revoke(a)
    assert len(s.get_reviews(b,'demo-tg-1')['items'])==2
    with pytest.raises(APIError,match='unauthorized'):s.record_review(a,**p)
    again=Store(s.pool.conninfo,s.files,s.public_url)
    try:assert again.get_reviews(b,'demo-tg-1')['items']==s.get_reviews(b,'demo-tg-1')['items']
    finally:again.close()


def test_actual_ingest_edit_and_delete_mark_old_source_revision_stale(reviewers):
    s,a,b=reviewers
    ar=Archive(s);ar.register('review-live','telegram','Fixture',discover=True)
    key=s.issue_key('review-live-reader',['review-live'],permissions=['read','review-write'])
    e={'account':'review-live','platform':'telegram','chat':'100','chat_title':'Fixture',
       'provider_id':'1','sender':'1','body':'original','sent_at':'2026-10-01T00:00:00Z',
       'updated_at':'2026-10-01T00:00:00Z','deleted':False,'direction':'incoming','origin':'live','media':[]}
    mid=ar.ingest(e)['message']
    initial=s.record_review(key,**payload(message_id=mid))['review']
    assert not initial['stale']
    ar.ingest({**e,'body':'edited','updated_at':'2026-10-01T00:01:00Z'})
    old=s.get_context(key,mid)['reviews']['items'][0]
    assert old['revision']==1 and old['stale'] and old['evidence'][0]['current_revision']==2
    fresh=s.record_review(key,**payload(message_id=mid,revision=2))['review']
    assert not fresh['stale']
    ar.ingest({**e,'deleted':True,'updated_at':'2026-10-01T00:02:00Z'})
    assert all(r['stale'] for r in s.get_reviews(key,mid)['items'])
    # Tombstones stay visible as stale evidence, not as an unreviewed live message.
    assert s.get_context(key,mid)['message']['deleted']


@pytest.mark.parametrize('permissions',[[],['read'],['review-write'],['read','review-write']])
def test_permissions_and_author_binding_for_rest_and_mcp(seeded,permissions):
    s,a,b=seeded;s.set_access('agent-b',permissions=permissions)
    p=payload();allowed=set(permissions)=={'read','review-write'}
    with TestClient(create_app(s,['testserver'])) as client:
        h={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream'}
        result=client.post('/v1/record_review',headers=h,json=p)
        assert result.status_code==(200 if allowed else 403)
        rpc=client.post('/mcp',headers=h,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'record_review','arguments':p}}).json()['result']
        assert bool(rpc.get('isError'))!=allowed
        if allowed:
            assert rpc['structuredContent']['review']['author']=='agent-b'
            assert rpc['structuredContent']['replayed']
            forged=client.post('/v1/record_review',headers=h,json={**p,'principal':'agent-a'})
            assert forged.status_code==400
            invalid_rpc=client.post('/mcp',headers=h,json={'jsonrpc':'2.0','id':2,'method':'tools/call',
                'params':{'name':'record_review','arguments':{**p,'revision':True}}}).json()['error']
            assert invalid_rpc['code']==-32602 and invalid_rpc['data']['fields']==['revision']
        read=client.post('/v1/get_reviews',headers=h,json={'message_id':'demo-tg-1'})
        assert read.status_code==(200 if 'read' in permissions else 403)
    caps=s.capabilities(b)
    assert ('record_review' in caps['available'])==allowed


@pytest.mark.parametrize('changes',[
    {'revision':True},{'revision':0},{'request_id':'not-a-uuid'},
    {'result_ref':'javascript:alert(1)'},{'result_ref':'https://user:pass@example.invalid'},
    {'result_ref':'https://@example.invalid'},
    {'result_ref':'https://example.invalid:wrong/'},{'result_ref':'file:///private/session'},
    {'result_ref':'https://example.invalid/\nInjected'},{'result_ref':'https://example.invalid\\@secret'},
    {'evidence':[{'message_id':'demo-tg-1','revision':2}]},
    {'evidence':[{'message_id':'demo-tg-2','revision':1,'account':'spoofed'}]},
    {'evidence':[{'message_id':'demo-tg-2','revision':1}]*20},
])
def test_invalid_review_is_denied_without_writes_or_content_in_audit(reviewers,changes):
    s,a,b=reviewers
    with pytest.raises(APIError,match='invalid_review'):s.record_review(b,**payload(**changes))
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM reviews').fetchone()['n']==0
    audit=s.audit_tail(principal='agent-b',limit=1)[0]
    assert audit['operation']=='record_review' and audit['outcome']=='denied' and audit['resource_ids']==[]
    assert 'result_ref' not in json.dumps(audit)


def test_concurrent_idempotency_and_audit_failure_are_atomic(reviewers):
    s,a,b=reviewers;p=payload()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:s.record_review(a,**p),range(2)))
    assert results[0]['review']==results[1]['review']
    assert sorted(r['replayed'] for r in results)==[False,True]
    with s.pool.connection() as c:
        counter=c.execute('SELECT value FROM review_counter').fetchone()['value']
        c.execute("""CREATE FUNCTION fail_review_audit() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.operation='record_review' THEN RAISE EXCEPTION 'audit_unavailable'; END IF;
            RETURN NEW; END $$""")
        c.execute('CREATE TRIGGER fail_review BEFORE INSERT ON audit_log FOR EACH ROW EXECUTE FUNCTION fail_review_audit()')
    try:
        with pytest.raises(psycopg.Error,match='audit_unavailable'):s.record_review(a,**payload())
        with s.pool.connection() as c:
            assert c.execute('SELECT count(*) AS n FROM reviews').fetchone()['n']==1
            assert c.execute('SELECT count(*) AS n FROM review_sources').fetchone()['n']==1
            assert c.execute('SELECT value FROM review_counter').fetchone()['value']==counter
    finally:
        with s.pool.connection() as c:
            c.execute('DROP TRIGGER fail_review ON audit_log');c.execute('DROP FUNCTION fail_review_audit()')


def test_commit_order_does_not_skip_a_late_review(reviewers,monkeypatch):
    import messaging_service.reviews as module
    s,a,b=reviewers;inserted=Event();release=Event();entered=Event()
    original=module.display
    def paused(c,rows):
        if rows and rows[0]['result_ref'].endswith(':slow'):
            inserted.set();assert release.wait(5)
        return original(c,rows)
    monkeypatch.setattr(module,'display',paused)
    def second():
        entered.set();return s.record_review(b,**payload(result_ref='urn:personal-os:task:fast'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow=pool.submit(s.record_review,a,**payload(result_ref='urn:personal-os:task:slow'))
        assert inserted.wait(5)
        fast=pool.submit(second);assert entered.wait(5)
        assert s.get_reviews(b,'demo-tg-1')['items']==[]
        release.set();first=slow.result(5);second_result=fast.result(5)
    page=s.get_reviews(b,'demo-tg-1',limit=1)
    assert page['items']==[first['review']]
    assert s.get_reviews(b,'demo-tg-1',after=page['next_cursor'])['items']==[second_result['review']]


def test_gateway_role_append_only_and_backup_covers_provenance(reviewers):
    import messaging_service.store as module
    s,a,b=reviewers;role='test_review_gateway'
    ddl=Path(module.__file__).with_name('gateway_role.sql').read_text().replace('messaging_gateway',role)
    with s.pool.connection() as c:c.execute(ddl)
    class Pool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute('SET LOCAL ROLE '+role);yield c
    gateway=copy(s);gateway.pool=Pool()
    try:
        review=gateway.record_review(a,**payload())['review']
        assert gateway.get_context(b,'demo-tg-1')['reviews']['items']==[review]
        for sql in ['DELETE FROM reviews','UPDATE reviews SET result_ref=\'forged\'',
                    'DELETE FROM review_sources','UPDATE message_revisions SET snapshot=\'{}\'',
                    "INSERT INTO reviews(created_at) VALUES(now())"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with gateway.pool.connection() as c:c.execute(sql)
        with s.pool.connection() as c:
            manifest=database_manifest(c)
        assert len(manifest)==87 and manifest['reviews']['rows']==manifest['review_sources']['rows']==1
    finally:
        with s.pool.connection() as c:c.execute('DROP OWNED BY '+role);c.execute('DROP ROLE '+role)


def test_operator_cli_can_issue_review_key_and_retains_safe_defaults(seeded,tmp_path):
    import os
    import subprocess
    import sys
    s,a,b=seeded
    assert 'review-write' not in s.capabilities(a)['permissions']
    key=tmp_path/'review.token'
    env={**os.environ,'DATABASE_URL_FILE':os.environ['PROBE_TEST_DSN_FILE']}
    command=[sys.executable,'-m','messaging_service','issue-key','--principal','cli-review',
        '--accounts','telegram-demo','--permissions','read','review-write','--key-file',str(key)]
    r=subprocess.run(command,env=env,capture_output=True,text=True,timeout=15)
    assert r.returncode==0, r.stderr
    token=key.read_text().strip()
    assert token not in r.stdout+r.stderr and key.stat().st_mode & 0o777==0o600
    assert s.record_review(token,**payload())['review']['author']=='cli-review'
    assert s.capabilities(token)['permissions']==['read','review-write']
