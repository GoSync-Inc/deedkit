"""Cached provider formatting remains scoped, immutable, separate archive evidence."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
import hashlib

import pytest
from telethon import types

from messaging_service.adapters import telegram_message, whatsapp_message
from messaging_service.envelope import fingerprint, validate
from messaging_service.presentation import message_summary
from messaging_service.store import APIError
from test_archive import archive as archive, event


def tg(body='😀 привет', entities=(), **changes):
    value=types.Message(id=17,peer_id=types.PeerUser(100),date=datetime(2026,10,1,12,tzinfo=timezone.utc),
        message=body,from_id=types.PeerUser(42),entities=None if entities is None else list(entities))
    for key,val in changes.items():setattr(value,key,val)
    return value


def proof(body='Первый текст lighthouse', entities=None, **changes):
    value={'version':1,'state':'observed','basis':'telegram_native_entities',
        'wire_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'entities':entities if entities is not None else [{'type':'bold','offset':0,'length':6}],
        'unsupported_entities':[],'warnings':[]}
    value.update(changes);return value


def public_proof(value=None):
    return {**(proof() if value is None else value),'offsets_reference':'full_provider_body_utf16'}


def test_real_telegram_native_utf16_styles_and_cached_links():
    body='😀 привет сайт'
    result=telegram_message('telegram-demo',tg(body,[types.MessageEntityBold(3,6),
        types.MessageEntityTextUrl(10,4,'https://example.com/page'),types.MessageEntityPre(3,6,'python')]))
    fmt=result['formatting']
    assert result['body']==body
    assert fmt==proof(body,[{'type':'bold','offset':3,'length':6},
        {'type':'text_link','offset':10,'length':4,'url':'https://example.com/page'},
        {'type':'pre','offset':3,'length':6,'language':'python'}])


def test_missing_native_capture_is_unknown_and_observed_empty_is_distinct():
    missing=telegram_message('telegram-demo',tg(entities=None))['formatting']
    empty=telegram_message('telegram-demo',tg())['formatting']
    assert missing['state']=='unknown' and missing['entities'] is None and missing['wire_sha256'] is None
    assert empty['state']=='observed' and empty['entities']==[]
    assert empty['wire_sha256']==hashlib.sha256('😀 привет'.encode()).hexdigest()


def test_unsupported_entities_keep_only_safe_type_and_offset_metadata():
    msg=tg('hello',[types.MessageEntityMentionName(0,5,999999),types.MessageEntityCustomEmoji(0,5,777777),
        types.MessageEntityUnknown(0,5)])
    result=telegram_message('telegram-demo',msg)['formatting']
    assert result['state']=='partial' and result['entities']==[]
    assert result['unsupported_entities']==[
        {'type':'mention_name','offset':0,'length':5},{'type':'custom_emoji','offset':0,'length':5},
        {'type':'unknown','offset':0,'length':5}]
    assert '999999' not in repr(result) and '777777' not in repr(result)


@pytest.mark.parametrize('url',['file:///secret','javascript:alert(1)','https://host/a\nsecret','https://user:pass@example.com/','https://host/a\u202esecret','https://host/a\u0085secret'])
def test_unsafe_native_links_do_not_drop_provider_message_or_copy_url(url):
    value=telegram_message('telegram-demo',tg('site',[types.MessageEntityTextUrl(0,4,url)]))
    assert value['body']=='site'
    assert value['formatting']['state']=='partial'
    assert value['formatting']['unsupported_entities']==[{'type':'text_link','offset':0,'length':4}]
    assert url not in repr(value['formatting'])


@pytest.mark.parametrize('offset,length',[(1,1),(0,1),(2,100),(True,2),(-1,1)])
def test_bad_native_offsets_fail_closed_without_dropping_text(offset,length):
    value=telegram_message('telegram-demo',tg('😀 hello',[types.MessageEntityBold(offset,length)]))
    assert value['body']=='😀 hello'
    assert value['formatting']['state']=='partial'
    assert value['formatting']['entities']==[]
    assert 'native_entities_invalid' in value['formatting']['warnings']


def test_capture_bounds_do_not_use_send_limit_and_do_not_truncate_entities():
    body='😀'*32768+'x'*32768
    value=telegram_message('telegram-demo',tg(body,[types.MessageEntityBold(65536,32768)]))
    assert value['body']==body and value['formatting']['entities'][0]['offset']==65536
    value=telegram_message('telegram-demo',tg('hello',[types.MessageEntityBold(0,5)]*257))
    assert value['formatting']['state']=='partial' and value['formatting']['entities']==[]
    assert 'native_entities_limit' in value['formatting']['warnings']


def test_unknown_objects_and_sensitive_fields_are_never_serialized():
    value=telegram_message('telegram-demo',tg('hello',[SimpleNamespace(offset=0,length=5,token='PRIVATE_SENTINEL',url='secret')]))
    assert value['formatting']['state']=='partial'
    assert 'PRIVATE_SENTINEL' not in repr(value)
    assert value['formatting']['unsupported_entities']==[]


def test_whatsapp_wire_is_observed_but_native_syntax_interpretation_is_unknown():
    body='*bold* 5*3 _maybe_'
    value=whatsapp_message('whatsapp-demo-1',{'chatId':'100@c.us','messageId':'false_100@c.us_17',
        'timestamp':1790856000,'body':body,'formatting':{'version':1,'state':'wire_observed',
        'basis':'whatsapp_native_wire','wire_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'entities':None,'unsupported_entities':[],'warnings':['syntax_interpretation_unknown']}})
    assert value['body']==body and value['formatting']['state']=='wire_observed'
    assert value['formatting']['entities'] is None
    assert whatsapp_message('whatsapp-demo-1',{'chatId':'100@c.us','messageId':'false_100@c.us_17',
        'timestamp':1790856000,'body':body})['formatting']['state']=='unknown'


@pytest.mark.parametrize('change',[
    {'state':'invented'},{'version':True},{'basis':'unverified_private_data'}, {'wire_sha256':'0'*64},
    {'unexpected':'PRIVATE_SENTINEL'},{'entities':[{'type':'bold','offset':0,'length':999}]},
    {'entities':[{'type':'bold','offset':0,'length':True}]},
    {'entities':[{'type':'text_link','offset':0,'length':1,'url':'javascript:alert(1)'}]},
    {'entities':[{'type':'mention_name','offset':0,'length':1,'user_id':99}]},
    {'unsupported_entities':[{'type':'custom_emoji','offset':0,'length':1,'document_id':99}]},
    {'warnings':['raw PRIVATE_SENTINEL']},{'entities':[{'type':'bold','offset':0,'length':1}]*257},
    {'entities':[{'type':'pre','offset':0,'length':1,'language':'bad\nsecret'}]},
])
def test_untrusted_envelope_formatting_is_strict(change):
    with pytest.raises(ValueError,match='invalid_formatting'):
        validate(event(formatting=proof(**change)))


def test_legacy_fingerprint_and_deleted_capture_do_not_claim_stale_wire():
    original=event()
    assert fingerprint(validate({**original,'formatting':proof()}))==fingerprint(validate(original))
    deleted=validate({**original,'deleted':True,'formatting':proof()})
    assert deleted['body']=='' and deleted['formatting']['state']=='unknown'


def test_compact_retains_proof_but_states_offsets_reference_full_body():
    body='x'*400
    result=message_summary({'body':body,'formatting':proof(body,[{'type':'bold','offset':300,'length':5}])})
    assert len(result['snippet'])==96
    assert result['formatting']['entities']==[{'type':'bold','offset':300,'length':5}]
    assert result['formatting']['offsets_reference']=='full_provider_body_utf16'
    many=message_summary({'body':body,'formatting':proof(body,[{'type':'bold','offset':i,'length':1} for i in range(40)])})
    assert len(many['formatting']['entities'])==16
    assert many['formatting']['entity_count']==40 and many['formatting']['entities_truncated'] is True


def test_first_known_and_formatting_edits_are_immutable_revisions(archive):
    ar,s,key,_=archive
    mid=ar.ingest(event())['message']
    frozen=deepcopy(s.get_updates(key,'formatting')['items'][0])
    assert frozen['formatting']['state']=='unknown'
    assert ar.ingest(event(formatting=proof()))['outcome']=='edited'
    bold=deepcopy(s.get_context(key,mid)['message'])
    assert bold['revision']==2 and bold['formatting']['entities'][0]['type']=='bold'
    changed=proof(entities=[{'type':'italic','offset':0,'length':6}])
    assert ar.ingest(event(formatting=changed,updated_at='2026-10-01T12:01:00Z'))['outcome']=='edited'
    assert ar.ingest(event(formatting=changed,updated_at='2026-10-01T12:01:00Z'))['outcome']=='duplicate'
    history=s.get_updates(key,'formatting')['items']
    assert [x['revision'] for x in history]==[1,2,3]
    assert history[0]['formatting']==frozen['formatting']
    assert history[1]['formatting']==bold['formatting']
    assert history[2]['formatting']==public_proof(changed)
    with s.pool.connection() as c:
        values=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s ORDER BY revision',(mid,)).fetchall()
        assert values[0]['snapshot'].get('formatting') is None
        assert values[1]['snapshot']['updated_at']==values[0]['snapshot']['updated_at']


def test_unknown_observations_cannot_downgrade_known_same_content(archive):
    ar,s,key,_=archive
    mid=ar.ingest(event(formatting=proof()))['message']
    assert ar.ingest(event(updated_at='2026-10-01T12:01:00Z'))['outcome']=='unchanged'
    assert s.get_context(key,mid)['message']['formatting']==public_proof()
    assert ar.ingest(event(formatting=proof(entities=[{'type':'italic','offset':0,'length':6}]),
        updated_at='2026-10-01T12:00:30Z'))['outcome']=='stale'
    assert s.get_context(key,mid)['message']['revision']==1


def test_same_time_conflicting_known_native_formatting_stays_visible(archive):
    ar,s,key,_=archive
    mid=ar.ingest(event(formatting=proof()))['message']
    contradictory=proof(entities=[{'type':'italic','offset':0,'length':6}])
    assert ar.ingest(event(formatting=contradictory))['outcome']=='conflict'
    row=s.get_context(key,mid)['message']
    assert row['formatting']==public_proof() and row['evidence']['active_conflicts']==1
    assert ar.ingest(event(formatting=proof(),origin='backfill'))['outcome']=='unchanged'
    assert s.get_context(key,mid)['message']['evidence']['active_conflicts']==1


def test_sender_completion_does_not_hide_formatting_conflicts(archive):
    ar,s,key,_=archive
    original=event(sender='',direction='outgoing',metadata={'chat_type':'private'},formatting=proof())
    mid=ar.ingest(original)['message']
    candidate={**original,'sender':'42','metadata':{'chat_type':'private','sender_identity_basis':'authenticated_self'},
        'formatting':proof(entities=[{'type':'italic','offset':0,'length':6}])}
    assert ar.ingest(candidate)['outcome']=='conflict'
    assert s.get_context(key,mid)['message']['sender']==''
    assert ar.ingest({**candidate,'formatting':proof()})['outcome']=='edited'
    assert s.get_context(key,mid)['message']['evidence']['active_conflicts']==1


def test_current_reads_align_exact_revision_and_acl(archive):
    ar,s,key,other=archive
    mid=ar.ingest(event(formatting=proof()))['message']
    values=[s.get_context(key,mid)['message'],s.get_messages(key,stable_chat(ar,s,mid))['items'][0],s.search(key,'lighthouse')['items'][0]]
    assert all(x['formatting']==public_proof() and x['revision']==1 for x in values)
    with pytest.raises(APIError):s.get_context(other,mid)
    assert all(x['account']!='archive-test' for x in s.search(other)['items'])


def stable_chat(ar,s,mid):
    with s.pool.connection() as c:return c.execute('SELECT chat FROM messages WHERE id=%s',(mid,)).fetchone()['chat']


def test_new_body_unknown_formatting_does_not_reuse_wrong_wire(archive):
    ar,s,key,_=archive
    mid=ar.ingest(event(formatting=proof()))['message']
    assert ar.ingest(event(body='new text',updated_at='2026-10-01T12:01:00Z'))['outcome']=='edited'
    assert s.get_context(key,mid)['message']['formatting']['state']=='unknown'
    assert s.get_updates(key,'different-content')['items'][0]['formatting']==public_proof()


def test_later_unknown_capture_does_not_resolve_known_formatting_contradiction(archive):
    ar,s,key,_=archive
    mid=ar.ingest(event(formatting=proof()))['message']
    contradiction=proof(entities=[{'type':'italic','offset':0,'length':6}])
    assert ar.ingest(event(formatting=contradiction))['outcome']=='conflict'
    assert ar.ingest(event(updated_at='2026-10-01T12:01:00Z'))['outcome']=='unchanged'
    assert s.get_context(key,mid)['message']['evidence']['active_conflicts']==1
    assert ar.ingest(event(formatting=proof(),updated_at='2026-10-01T12:02:00Z'))['outcome']=='unchanged'
    assert s.get_context(key,mid)['message']['evidence']['active_conflicts']==0


def test_exact_revision_evidence_helper_does_not_query_latest_message():
    from messaging_service.result_evidence import formatting_evidence
    class Cursor:
        def fetchall(self):return [{'message':'synthetic-one','revision':1,'formatting':proof()}]
    class Connection:
        def execute(self,sql,params):
            assert 'message_revisions' in sql and 'revision' in sql and 'messages m' not in sql
            assert params==(['synthetic-one','synthetic-two'],[1,9])
            return Cursor()
    frozen=proof(entities=[])
    rows=[{'id':'synthetic-one','revision':1},{'message':'synthetic-two','revision':9},
        {'message':'synthetic-one','revision':2,'formatting':frozen}]
    formatting_evidence(Connection(),rows)
    assert rows[0]['formatting']==proof() and rows[1]['formatting']['state']=='unknown'
    assert rows[2]['formatting']==frozen


def test_native_strikethrough_name_and_expandable_quote_are_explicit():
    value=telegram_message('telegram-demo',tg('hello',[types.MessageEntityStrike(0,5),
        types.MessageEntityBlockquote(0,5,collapsed=True)]))['formatting']
    assert value['state']=='observed'
    assert value['entities']==[{'type':'strikethrough','offset':0,'length':5},
        {'type':'expandable_blockquote','offset':0,'length':5}]


def test_native_invalid_code_language_remains_safe_partial_evidence():
    value=telegram_message('telegram-demo',tg('hello',[types.MessageEntityPre(0,5,'PRIVATE\nSENTINEL')]))['formatting']
    assert value['state']=='partial' and value['entities']==[]
    assert value['unsupported_entities']==[{'type':'pre','offset':0,'length':5}]
    assert 'PRIVATE' not in repr(value)


def test_whatsapp_live_capture_spool_accepts_exact_wire_evidence(tmp_path):
    from messaging_service.collector_state import validate_capture, CaptureSpool
    body='*hello*'
    raw={'kind':'whatsapp_event','account':'whatsapp-demo-1','change':'message',
        'observed_at':'2026-10-01T12:00:00Z','origin':'live','event':{
            'chatId':'100@c.us','messageId':'false_100@c.us_17','timestamp':1790856000,'body':body,
            'formatting':{'version':1,'state':'wire_observed','basis':'whatsapp_native_wire',
                'wire_sha256':hashlib.sha256(body.encode()).hexdigest(),'entities':None,
                'unsupported_entities':[],'warnings':['syntax_interpretation_unknown']}}}
    assert validate_capture(raw)['envelope']['formatting']==raw['event']['formatting']
    spool=CaptureSpool(tmp_path/'incoming-formatting',reserve_bytes=0)
    spool.put(raw)
    received=[]
    assert spool.drain(lambda value: received.append(value))==1
    assert received[0]['envelope']['formatting']==raw['event']['formatting']


@pytest.mark.parametrize('platform',['telegram','whatsapp'])
def test_deleted_envelope_clears_native_proof_before_empty_body_validation(platform):
    body='hello'
    fmt=proof(body,[{'type':'bold','offset':0,'length':5}]) if platform=='telegram' else {
        'version':1,'state':'wire_observed','basis':'whatsapp_native_wire','wire_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'entities':None,'unsupported_entities':[],'warnings':['syntax_interpretation_unknown']}
    value=validate(event(platform=platform,body='',deleted=True,formatting=fmt))
    assert value['body']=='' and value['formatting']['state']=='unknown'
    assert value['formatting']['wire_sha256'] is None


@pytest.mark.parametrize('platform',['telegram','whatsapp'])
def test_real_collector_tombstones_clear_formatted_proof_and_replay_safely(seeded,platform,monkeypatch):
    from messaging_service.archive import Archive
    from messaging_service.collector_state import CollectorState
    store,_,_=seeded
    account='formatted-deletion-'+platform
    chat='100' if platform=='telegram' else '100@c.us'
    provider_id='17' if platform=='telegram' else 'false_100@c.us_17'
    body='hello'
    fmt=proof(body,[{'type':'bold','offset':0,'length':5}]) if platform=='telegram' else {
        'version':1,'state':'wire_observed','basis':'whatsapp_native_wire','wire_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'entities':None,'unsupported_entities':[],'warnings':['syntax_interpretation_unknown']}
    archive=Archive(store);archive.register(account,platform,'Synthetic formatted deletion',include_chats=[chat])
    epoch=archive.acquire(account,'formatted-deletion-worker')
    state=CollectorState(archive,account,('formatted-deletion-worker',epoch))
    try:
        state.chat(chat,'user')
        key=store.issue_key('formatted-deletion-reader',[account])
        mid=archive.ingest(event(account=account,platform=platform,chat=chat,provider_id=provider_id,
            body=body,formatting=fmt),lease=state.lease)['message']
        original=deepcopy(store.get_updates(key,'format-deletion')['items'][0])
        deleted={'kind':platform+'_delete','account':account,'chat':chat,'ids':[17 if platform=='telegram' else provider_id],
            'observed_at':'2026-10-01T12:01:00Z'}
        # The tombstone is committed before revision publication: exercise a
        # crash at that boundary, then durable recovery and ordinary replay.
        apply_tombstones=state.apply_tombstones
        def interrupted():raise RuntimeError('synthetic_delete_publication_interrupted')
        monkeypatch.setattr(state,'apply_tombstones',interrupted)
        with pytest.raises(RuntimeError,match='synthetic_delete_publication_interrupted'):
            state.apply(deleted)
        monkeypatch.setattr(state,'apply_tombstones',apply_tombstones)
        assert state.apply_tombstones()==1
        state.apply(deleted)
        assert state.apply_tombstones()==0
        current=store.get_context(key,mid)['message']
        assert current['deleted'] is True and current['body']=='' and current['revision']==2
        assert current['formatting']['state']=='unknown' and current['formatting']['wire_sha256'] is None
        history=store.get_updates(key,'format-deletion')['items']
        assert history[0]==original
        assert history[1]['kind']=='message.deleted' and history[1]['formatting']['state']=='unknown'
        with store.pool.connection() as c:
            snapshots=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s ORDER BY revision',(mid,)).fetchall()
        assert snapshots[0]['snapshot']['formatting']==fmt
        assert snapshots[0]['snapshot']['body']==body and snapshots[1]['snapshot']['body']==''
        assert snapshots[1]['snapshot']['formatting']['state']=='unknown'
    finally:
        archive.release(account,'formatted-deletion-worker',epoch)


def test_compact_unknown_formatting_is_small_and_explicit():
    from messaging_service.incoming_formatting import unknown
    value=message_summary({'body':'hello','formatting':unknown()})
    assert value['formatting']=={'state':'unknown'}


def test_full_truncated_reads_explain_offsets_for_every_query(archive):
    ar,s,key,_=archive
    body='hello lighthouse '+('x'*1000)
    fmt=proof(body,[{'type':'bold','offset':800,'length':5}])
    mid=ar.ingest(event(body=body,formatting=fmt))['message']
    chat=stable_chat(ar,s,mid)
    rows=[s.get_context(key,mid,body_chars=3)['message'],s.get_messages(key,chat,body_chars=3)['items'][0],
        s.search(key,'lighthouse',body_chars=3)['items'][0],s.get_updates(key,'format-short',body_chars=3)['items'][0]]
    assert all(row['body_truncated'] is True and len(row['body'])==3 for row in rows)
    assert all(row['formatting']=={**fmt,'offsets_reference':'full_provider_body_utf16'} for row in rows)
    with s.pool.connection() as c:
        snapshot=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s AND revision=1',(mid,)).fetchone()['snapshot']
    assert snapshot['formatting']==fmt
