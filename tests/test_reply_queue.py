"""Unanswered-message queue regressions, exclusively synthetic provider data."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import types

from messaging_service.archive import Archive
from messaging_service.adapters import telegram_message, whatsapp_message
from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.store import APIError
from test_archive import event


@pytest.fixture()
def inbox(seeded):
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('queue-test', 'telegram', 'Synthetic queue', discover=True)
    key = s.issue_key('queue-agent', ['queue-test'])
    def add(chat, mid, seconds, **changes):
        at = (datetime.now(timezone.utc)-timedelta(hours=2)+timedelta(seconds=seconds)).isoformat()
        return ar.ingest(event(account='queue-test', chat=chat, provider_id=str(mid),
            sent_at=at, updated_at=at, **{'metadata':{'chat_type':'private'}, **changes}))['message']
    def queue(**kwargs):
        return s.list_chats(key, awaiting_reply=True, include_coverage=False, **kwargs)
    return s, ar, key, add, queue


def test_private_hint_has_source_context_and_consecutive_incoming_count(inbox):
    s, ar, key, add, queue = inbox
    add('100', 1, 1, direction='outgoing')
    add('100', 2, 2, body='First question?')
    last = add('100', 3, 3, body='Second question?', sender_profile={'display_name':'Synthetic Person','username':'synthetic'})
    row = render('list_chats', queue(), 'compact')['items'][0]
    assert row['awaiting_reply_message_id'] == last
    assert row['awaiting_reply_provider_message_id'] == '3'
    assert row['awaiting_reply_sent_at']
    assert row['awaiting_reply_snippet'] == 'Second question?'
    assert row['awaiting_reply_sender_name'] == 'Synthetic Person'
    assert row['awaiting_reply_sender'] == '42'
    assert row['awaiting_reply_incoming_count'] == 2


def test_empty_service_does_not_create_or_clear_private_attention(inbox):
    s, ar, key, add, queue = inbox
    original = add('100', 1, 1, body='A real question?')
    add('100', 2, 2, body='')
    add('100', 3, 3, body='System notice', direction='outgoing', metadata={'chat_type':'private','is_service':True})
    add('200', 1, 1, body='')
    add('777000', 1, 1, body='Telegram service')
    add('300', 1, 1, metadata={'chat_type':'private','is_self_chat':True})
    assert [r['awaiting_reply_message_id'] for r in queue()['items']] == [original]


def test_media_only_message_is_meaningful(inbox):
    s, ar, key, add, queue = inbox
    add('100', 1, 1, body='', media=[{'provider_id':'a','filename':'voice.ogg','mime':'audio/ogg','bytes':100}],
        metadata={'chat_type':'private','media_kind':'voice'})
    row = queue()['items'][0]
    assert row['awaiting_reply_snippet'] == '[voice]'


def test_same_second_telegram_owner_reply_clears_group_hint(inbox):
    s, ar, key, add, queue = inbox
    at = (datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    for mid, direction, metadata in [('10','incoming',{'mentions_owner':True}), ('11','outgoing',{'reply_to_provider_id':'10'})]:
        ar.ingest(event(account='queue-test', chat='100', provider_id=mid, sent_at=at, updated_at=at,
            direction=direction, metadata={'chat_type':'group', **metadata}))
    assert queue()['items'] == []
    ar.ingest(event(account='queue-test', chat='100', provider_id='12', sent_at=at, updated_at=at,
        metadata={'chat_type':'group','mentions_owner':True}))
    assert queue()['items'][0]['awaiting_reply_provider_message_id'] == '12'


def test_priority_and_exclusion_filters_are_scoped_and_cursor_bound(inbox):
    s, ar, key, add, queue = inbox
    # ANYM-276: a private chat the owner never wrote in is demoted, so these two have owner history.
    add('100', 0, 0, direction='outgoing')
    add('200', 0, 0, direction='outgoing')
    add('100', 1, 1, body='A question?')
    add('200', 1, 2, body='A statement', sender='99')
    add('300', 1, 3, body='Group question?', metadata={'chat_type':'group','mentions_owner':True})
    page = queue(order='priority', limit=1)
    assert page['items'][0]['provider_chat_id'] == '100'
    assert 'question_mark' in page['items'][0]['awaiting_reply_priority_reasons']
    next_page = queue(order='priority', limit=1, after=page['next_cursor'])
    assert next_page['items'][0]['provider_chat_id'] == '200'
    with pytest.raises(APIError, match='invalid_cursor'):
        queue(order='priority', after=page['next_cursor'], only_questions=True)
    filtered = queue(chat_types=['private'], only_questions=True)
    assert [r['provider_chat_id'] for r in filtered['items']] == ['100']
    assert queue(exclude_senders=['42'])['items'][0]['provider_chat_id'] == '200'
    assert queue(exclude_chats=[stable_id('chat','queue-test','100')], only_questions=True)['items'][0]['type'] == 'group'


def test_dismiss_is_key_local_and_new_incoming_reopens(inbox):
    s, ar, key, add, queue = inbox
    mid = add('100', 1, 1)
    chat = stable_id('chat','queue-test','100')
    second = s.issue_key('queue-agent', ['queue-test'])
    result = s.dismiss_reply(key, chat, mid, 1)
    assert result['dismissed'] is True and result['provider_read_changed'] is False
    assert queue()['items'] == []
    assert s.list_chats(second, awaiting_reply=True)['items']
    assert s.dismiss_reply(key, chat, mid, 1)['dismissed'] is True
    add('100', 2, 2)
    assert queue()['items']
    with pytest.raises(APIError, match='reply_target_changed'):
        s.dismiss_reply(key, chat, mid, 1)
    restricted = s.issue_key('queue-hidden', ['queue-test'], chats=[])
    with pytest.raises(APIError, match='not_found'):
        s.dismiss_reply(restricted, chat, mid, 1)
    readonly = s.issue_key('queue-readonly', ['queue-test'], permissions=['read'])
    with pytest.raises(APIError, match='forbidden'):
        s.dismiss_reply(readonly, chat, mid, 1)


def test_provider_read_observation_is_not_recipient_delivery(inbox):
    from messaging_service.reply_state import observe
    s, ar, key, add, queue = inbox
    add('100', 10, 1)
    add('200', 20, 2)
    chat = stable_id('chat','queue-test','100')
    assert queue(only_unread=True)['items'] == []
    assert queue(only_unread=True)['read_state_unknown_total'] == 2
    with s.pool.connection() as c:
        observe(c, 'queue-test', '100', {'observed_at':datetime.now(timezone.utc).isoformat(),
            'unread_count':1, 'read_inbox_max_id':9, 'basis':'telegram_dialog'})
    row = queue(only_unread=True)['items'][0]
    assert row['id'] == chat and row['read_by_owner'] is False and row['provider_unread_count'] == 1
    with s.pool.connection() as c:
        observe(c, 'queue-test', '100', {'observed_at':datetime.now(timezone.utc).isoformat(),
            'unread_count':0, 'read_inbox_max_id':10, 'basis':'telegram_dialog'})
    assert queue(only_unread=True)['items'] == []
    assert next(r for r in queue()['items'] if r['id']==chat)['read_by_owner'] is True


def test_cached_adapter_marks_service_and_authenticated_self_without_network():
    now = datetime.now(timezone.utc)
    owner = types.User(id=42, is_self=True, first_name='Synthetic owner')
    msg = SimpleNamespace(chat_id=42, id=1, sender_id=42, message='saved', date=now, edit_date=None,
        out=False, sender=owner, chat=owner, action=None)
    assert telegram_message('synthetic', msg, verified_self=owner)['metadata']['is_self_chat'] is True
    msg.chat_id=100; msg.chat=None; msg.action=types.MessageActionHistoryClear(); msg.message=''
    assert telegram_message('synthetic', msg, verified_self=owner)['metadata']['is_service'] is True
    wa = {'chatId':'100@c.us','messageId':'false_100@c.us_x','timestamp':int(now.timestamp()),
        'body':'System notice','messageType':'notification','isSelfChat':False}
    assert whatsapp_message('synthetic', wa)['metadata']['is_service'] is True


def test_stale_or_delayed_read_observations_do_not_claim_current_unread(inbox):
    from messaging_service.reply_state import observe
    s, ar, key, add, queue = inbox
    add('100', 1, 1)
    now=datetime.now(timezone.utc)
    with s.pool.connection() as c:
        observe(c,'queue-test','100',{'observed_at':(now-timedelta(minutes=10)).isoformat(),
            'unread_count':1,'read_inbox_max_id':0,'basis':'telegram_dialog'})
    row=queue()['items'][0]
    assert row['read_state']=='stale' and row['read_by_owner'] is None
    assert not queue(only_unread=True)['items']
    with s.pool.connection() as c:
        observe(c,'queue-test','100',{'observed_at':now.isoformat(),
            'unread_count':0,'read_inbox_max_id':1,'basis':'telegram_dialog'})
        observe(c,'queue-test','100',{'observed_at':(now-timedelta(seconds=1)).isoformat(),
            'unread_count':10,'read_inbox_max_id':0,'basis':'telegram_dialog'})
    assert queue()['items'][0]['read_by_owner'] is True


def test_dialog_inventory_persists_read_snapshot_under_collector_fence(inbox):
    from messaging_service.collector_state import CollectorState
    s, ar, key, add, queue = inbox
    epoch=ar.acquire('queue-test','synthetic-reader',60)
    state=CollectorState(ar,'queue-test',('synthetic-reader',epoch))
    at=datetime.now(timezone.utc).isoformat()
    state.inventory([('100','user','Synthetic','private',{'observed_at':at,'unread_count':2,
        'read_inbox_max_id':0,'basis':'telegram_dialog','is_self_chat':False})],None)
    add('100',1,1)
    assert queue()['items'][0]['provider_unread_count']==2
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) n FROM chat_read_observations').fetchone()['n']==1
    ar.release('queue-test','synthetic-reader',epoch)
    with pytest.raises(RuntimeError,match='collector_lease_lost'):
        state.inventory([('200','user','Synthetic','private',{'observed_at':at,'unread_count':0,
            'basis':'telegram_dialog'})],None)


def test_mcp_and_rest_expose_queue_filters_and_local_dismissal(inbox):
    from starlette.testclient import TestClient
    from messaging_service.app import create_app
    from test_client_contract import rpc
    s, ar, key, add, queue=inbox
    mid=add('100',1,1,body='A question?')
    chat=stable_id('chat','queue-test','100')
    with TestClient(create_app(s,['testserver'])) as client:
        r=rpc(client,key,'list_chats',{'awaiting_reply':True,'order':'priority','only_questions':True})
        assert 'error' not in r.json()
        assert r.json()['result']['structuredContent']['items'][0]['awaiting_reply']['message_id']==mid
        r=client.post('/v1/dismiss_reply',headers={'Authorization':'Bearer '+key},
            json={'chat_id':chat,'message_id':mid,'expected_revision':1})
        assert r.status_code==200 and r.json()['dismissed'] is True
        assert queue()['items']==[]


def test_passive_telegram_owner_read_capture_is_durable_scoped_and_not_delivery(inbox):
    from messaging_service.reply_state import telegram_read_capture
    from messaging_service.collector_state import CollectorState, validate_capture
    s, ar, key, add, queue = inbox
    add('100',10,1)
    owner=types.User(id=42,is_self=True)
    update=types.UpdateReadHistoryInbox(types.PeerUser(100),10,0,1,1)
    capture=telegram_read_capture('queue-test',update,verified_self=owner,expected_user_id=42)
    assert capture['kind']=='owner_read_state'
    assert telegram_read_capture('queue-test',types.UpdateReadHistoryOutbox(types.PeerUser(100),10,1,1),
        verified_self=owner,expected_user_id=42) is None
    assert telegram_read_capture('queue-test',update,verified_self=None,expected_user_id=42) is None
    epoch=ar.acquire('queue-test','synthetic-read-events',60)
    state=CollectorState(ar,'queue-test',('synthetic-read-events',epoch))
    assert state.apply(validate_capture(capture))['outcome']=='owner_read_observed'
    assert queue()['items'][0]['read_by_owner'] is True
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) n FROM message_receipts').fetchone()['n']==0
    ar.release('queue-test','synthetic-read-events',epoch)
    with pytest.raises(RuntimeError,match='collector_lease_lost'):state.apply(capture)


def test_owner_read_telegram_subscription_is_optional_and_no_rpc():
    from messaging_service.telegram_collector import TelethonProvider
    registered=[]
    provider=TelethonProvider.__new__(TelethonProvider)
    provider.client=SimpleNamespace(add_event_handler=lambda cb,ev:registered.append((cb,ev)))
    owner_read=object()
    provider.subscribe(object(),object(),object(),owner_reads=owner_read)
    assert len(registered)==5 and registered[-1][0] is owner_read
    assert types.UpdateReadHistoryInbox in registered[-1][1].types
    assert types.UpdateReadChannelInbox in registered[-1][1].types


def test_whatsapp_chat_count_does_not_assert_exact_message_was_read(seeded):
    from messaging_service.reply_state import observe
    s,_,_=seeded;ar=Archive(s)
    ar.register('wa-queue','whatsapp','Synthetic',discover=True)
    key=s.issue_key('wa-queue-agent',['wa-queue'])
    at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
    ar.ingest(event(account='wa-queue',platform='whatsapp',chat='100@c.us',
        provider_id='false_100@c.us_x',sent_at=at,updated_at=at,metadata={'chat_type':'private'}))
    with s.pool.connection() as c:
        observe(c,'wa-queue','100@c.us',{'observed_at':datetime.now(timezone.utc).isoformat(),
            'unread_count':0,'basis':'whatsapp_cached_chat'})
    row=s.list_chats(key,awaiting_reply=True)['items'][0]
    assert row['provider_unread'] is False and row['read_by_owner'] is None


def test_native_whatsapp_read_basis_is_valid_and_inventory_uses_existing_schema(seeded):
    from messaging_service.collector_state import CollectorState
    from messaging_service.reply_state import validate_observation
    s,_,_=seeded;ar=Archive(s)
    ar.register('wa-native-read','whatsapp','Synthetic native',discover=True)
    at=datetime.now(timezone.utc).isoformat()
    observation={'observed_at':at,'unread_count':None,
        'basis':'whatsmeow_captured_chat','is_self_chat':True}
    assert validate_observation(observation)['basis']=='whatsapp_cached_chat'
    epoch=ar.acquire('wa-native-read','synthetic-native-read',60)
    state=CollectorState(ar,'wa-native-read',('synthetic-native-read',epoch))
    try:
        state.inventory([('100@c.us','user','Synthetic self','private',observation)],None)
        with s.pool.connection() as c:
            row=c.execute('SELECT * FROM chat_read_observations WHERE account=%s',
                          ('wa-native-read',)).fetchone()
        assert row['basis']=='whatsapp_cached_chat' and row['is_self_chat'] is True
        assert row['unread_count'] is None and row['read_inbox_max_id'] is None
    finally:
        ar.release('wa-native-read','synthetic-native-read',epoch)


def test_native_whatsapp_basis_cannot_claim_telegram_boundary_or_account(inbox):
    from messaging_service.reply_state import observe,validate_observation
    s,_,_,add,_=inbox;add('100',1,1)
    observation={'observed_at':datetime.now(timezone.utc).isoformat(),
        'basis':'whatsmeow_captured_chat','unread_count':None}
    with pytest.raises(ValueError,match='invalid_read_observation'):
        validate_observation({**observation,'read_inbox_max_id':10})
    with s.pool.connection() as c:
        with pytest.raises(ValueError,match='invalid_read_observation'):
            observe(c,'queue-test','100',observation)


@pytest.mark.parametrize('native_self',[None,False])
def test_native_unknown_does_not_erase_or_refresh_previous_whatsapp_unread(seeded,native_self):
    from messaging_service.reply_state import observe
    s,_,_=seeded;ar=Archive(s)
    ar.register('wa-read-handover','whatsapp','Synthetic handover',discover=True)
    key=s.issue_key('wa-read-reader',['wa-read-handover'])
    chat='100@c.us';at=datetime.now(timezone.utc)-timedelta(minutes=10)
    ar.ingest(event(account='wa-read-handover',platform='whatsapp',chat=chat,
        provider_id='false_100@c.us_x',sent_at=(at-timedelta(minutes=1)).isoformat(),
        updated_at=(at-timedelta(minutes=1)).isoformat(),metadata={'chat_type':'private'}))
    with s.pool.connection() as c:
        observe(c,'wa-read-handover',chat,{'observed_at':at.isoformat(),
            'unread_count':3,'basis':'whatsapp_cached_chat'})
        before=c.execute('SELECT * FROM chat_read_observations WHERE account=%s',
                         ('wa-read-handover',)).fetchone()
        observe(c,'wa-read-handover',chat,{'observed_at':datetime.now(timezone.utc).isoformat(),
            'unread_count':None,'basis':'whatsmeow_captured_chat','is_self_chat':native_self})
        after=c.execute('SELECT * FROM chat_read_observations WHERE account=%s',
                        ('wa-read-handover',)).fetchone()
    assert after['observed_at']==before['observed_at']
    assert after['unread_count']==3 and after['read_inbox_max_id'] is None
    assert after['is_self_chat'] is native_self
    row=s.list_chats(key,awaiting_reply=True)['items'][0]
    assert row['read_state']=='stale' and row['provider_unread'] is None
    assert not s.list_chats(key,awaiting_reply=True,only_unread=True)['items']


@pytest.mark.parametrize('partial',[
    {'unread_count':None,'read_inbox_max_id':None},
    {'unread_count':None,'read_inbox_max_id':11},
    {'unread_count':2,'read_inbox_max_id':None},
])
def test_partial_owner_read_snapshot_preserves_known_evidence_and_time(inbox,partial):
    from messaging_service.reply_state import observe
    s,_,_,add,_=inbox;add('100',1,1)
    at=datetime.now(timezone.utc)-timedelta(minutes=10)
    with s.pool.connection() as c:
        observe(c,'queue-test','100',{'observed_at':at.isoformat(),
            'unread_count':0,'read_inbox_max_id':10,'basis':'telegram_dialog','is_self_chat':False})
        before=c.execute('SELECT * FROM chat_read_observations WHERE account=%s',('queue-test',)).fetchone()
        observe(c,'queue-test','100',{'observed_at':datetime.now(timezone.utc).isoformat(),
            'basis':'telegram_dialog','is_self_chat':None,**partial})
        after=c.execute('SELECT * FROM chat_read_observations WHERE account=%s',('queue-test',)).fetchone()
    assert after==before


def test_unknown_can_update_proven_self_without_refreshing_read_or_accepting_delayed_self(inbox):
    from messaging_service.reply_state import observe
    s,_,_,add,_=inbox;add('100',1,1)
    at=datetime.now(timezone.utc)-timedelta(minutes=10)
    with s.pool.connection() as c:
        observe(c,'queue-test','100',{'observed_at':at.isoformat(),
            'unread_count':0,'read_inbox_max_id':1,'basis':'telegram_dialog'})
        before=c.execute('SELECT * FROM chat_read_observations WHERE account=%s',('queue-test',)).fetchone()
        observe(c,'queue-test','100',{'observed_at':datetime.now(timezone.utc).isoformat(),
            'basis':'telegram_dialog','is_self_chat':True})
        observe(c,'queue-test','100',{'observed_at':(at-timedelta(seconds=1)).isoformat(),
            'basis':'telegram_dialog','is_self_chat':False})
        after=c.execute('SELECT * FROM chat_read_observations WHERE account=%s',('queue-test',)).fetchone()
    assert after=={**before,'is_self_chat':True}


def test_edit_delete_and_old_backfill_respect_exact_dismissal(inbox):
    s, ar, key, add, queue=inbox
    mid=add('100',5,5)
    chat=stable_id('chat','queue-test','100')
    s.dismiss_reply(key,chat,mid,1)
    add('100',2,2,origin='backfill')
    assert queue()['items']==[]
    with s.pool.connection() as c:
        sent_at=c.execute('SELECT sent_at FROM messages WHERE id=%s',(mid,)).fetchone()['sent_at'].isoformat()
    ar.ingest(event(account='queue-test',chat='100',provider_id='5',body='Edited question?',
        sent_at=sent_at,
        updated_at=datetime.now(timezone.utc).isoformat(),metadata={'chat_type':'private'}))
    assert queue()['items'][0]['awaiting_reply_revision']==2
    s.dismiss_reply(key,chat,mid,2)
    ar.ingest(event(account='queue-test',chat='100',provider_id='5',deleted=True,
        sent_at=sent_at,updated_at=datetime.now(timezone.utc).isoformat(),metadata={'chat_type':'private'}))
    assert queue()['items'][0]['awaiting_reply_provider_message_id']=='2'


def test_reply_totals_and_snapshots_do_not_leak_excluded_chat_scope(inbox):
    s, ar, key, add, queue=inbox
    mid=add('100',1,1)
    add('200',1,1)
    chat=stable_id('chat','queue-test','100')
    scoped=s.issue_key('scoped-queue',['queue-test'],chats=[chat])
    result=s.list_chats(scoped,awaiting_reply=True,only_unread=True,include_coverage=False)
    assert result['awaiting_reply_total']==0 and result['read_state_unknown_total']==1
    s.set_access('scoped-queue',chats=[])
    with pytest.raises(APIError,match='not_found'):s.dismiss_reply(scoped,chat,mid,1)


def test_backup_hashes_exact_owner_observations_and_key_local_dismissal(inbox):
    from messaging_service.backup import database_manifest
    from messaging_service.reply_state import observe
    s, ar, key, add, queue=inbox
    mid=add('100',1,1);chat=stable_id('chat','queue-test','100')
    s.dismiss_reply(key,chat,mid,1)
    with s.pool.connection() as c:
        observe(c,'queue-test','100',{'observed_at':datetime.now(timezone.utc).isoformat(),
            'unread_count':0,'read_inbox_max_id':1,'basis':'telegram_dialog'})
        manifest=database_manifest(c)
        assert len(manifest)==70
        assert manifest['reply_dismissals']['rows']==manifest['chat_read_observations']['rows']==1
        c.execute('UPDATE chat_read_observations SET unread_count=1 WHERE chat=%s',(chat,))
        changed=database_manifest(c)
    assert changed['chat_read_observations']['sha256']!=manifest['chat_read_observations']['sha256']
    assert changed['reply_dismissals']==manifest['reply_dismissals']


def test_gateway_can_dismiss_but_cannot_forge_provider_read_state(inbox):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    s, ar, key, add, queue=inbox
    mid=add('100',1,1);chat=stable_id('chat','queue-test','100')
    role='test_reply_gateway_'+uuid4().hex
    ddl=Path(__file__).resolve().parents[1].joinpath('services/messaging/messaging_service/gateway_role.sql').read_text().replace('messaging_gateway',role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    restricted=copy(s);restricted.pool=RestrictedPool()
    with s.pool.connection() as c:c.execute(ddl)
    try:
        assert restricted.list_chats(key,awaiting_reply=True,include_coverage=False)['items']
        assert restricted.dismiss_reply(key,chat,mid,1)['dismissed']
        assert not restricted.list_chats(key,awaiting_reply=True,include_coverage=False)['items']
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with restricted.pool.connection() as c:c.execute('UPDATE chat_read_observations SET unread_count=0')
        with s.pool.connection() as c:
            assert c.execute("SELECT count(*) n FROM audit_log WHERE operation='dismiss_reply' AND outcome='ok'").fetchone()['n']==1
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_whatsapp_queue_hint_never_names_a_sender_by_its_raw_id(seeded):
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('queue-wa', 'whatsapp', 'Synthetic WhatsApp queue', discover=True)
    key = s.issue_key('queue-wa-agent', ['queue-wa'])
    at = (datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    for chat in ('321@c.us', '903@lid'):
        ar.ingest(event(account='queue-wa', platform='whatsapp', chat=chat, sender=chat, provider_id='false_'+chat,
            sent_at=at, updated_at=at, metadata={'chat_type':'private'}))
    full = {r['awaiting_reply_sender']: r for r in s.list_chats(key, awaiting_reply=True, include_coverage=False)['items']}
    assert full['321@c.us']['awaiting_reply_sender_name'] == '+321'
    assert full['321@c.us']['awaiting_reply_sender_name_basis'] == 'phone_number'
    assert full['903@lid']['awaiting_reply_sender_name'] is None
    lean = render('list_chats', s.list_chats(key, awaiting_reply=True, include_coverage=False, view='conversation'), 'compact')
    hints = {r['awaiting_reply']['sender_id']: r['awaiting_reply'] for r in lean['items']}
    assert (hints['321@c.us']['sender_name'], hints['321@c.us']['sender_name_basis']) == ('+321', 'phone_number')
    assert 'sender_name' not in hints['903@lid']


# spec: ANYM-282
def test_queue_row_explains_a_passed_date_without_changing_its_priority(inbox, monkeypatch):
    s, ar, key, add, queue = inbox
    monkeypatch.delenv('ANYMSG_OWNER_TIMEZONE', raising=False)
    at = (datetime.now(timezone.utc)-timedelta(days=3)).isoformat()
    before = (datetime.now(timezone.utc)-timedelta(days=4)).isoformat()
    for chat, body in (('100', 'Сможешь завтра?'), ('200', 'Сможешь потом?')):
        ar.ingest(event(account='queue-test', chat=chat, provider_id='0', sent_at=before, updated_at=before,
            direction='outgoing', metadata={'chat_type':'private'}))
        ar.ingest(event(account='queue-test', chat=chat, provider_id='1', sent_at=at, updated_at=at,
            body=body, metadata={'chat_type':'private'}))
    rows = {r['provider_chat_id']: r for r in queue(order='priority')['items']}
    flagged, plain = rows['100'], rows['200']
    assert flagged['awaiting_reply_priority'] == plain['awaiting_reply_priority']
    assert flagged['awaiting_reply_priority_reasons'] == [*plain['awaiting_reply_priority_reasons'], 'referenced_date_passed']
    assert flagged['awaiting_reply_time_reference'] == {'text':'завтра', 'status':'passed', 'time_zone':'UTC_fallback',
        'suggest':'acknowledge_delay_offer_new_date',
        'resolved_date':(datetime.fromisoformat(at)+timedelta(days=1)).date().isoformat()}
    assert 'awaiting_reply_time_reference' not in plain
    compact = {r['provider_chat_id']: r for r in render('list_chats', queue(order='priority'), 'compact')['items']}
    assert compact['100']['awaiting_reply_time_reference'] == {'resolved_date':flagged['awaiting_reply_time_reference']['resolved_date'],
        'text':'завтра', 'suggest':'acknowledge_delay_offer_new_date'}


# spec: ANYM-282
def test_queue_page_survives_a_message_the_date_reader_cannot_resolve(inbox):
    s, ar, key, add, queue = inbox
    add('100', 0, 0, direction='outgoing')
    add('200', 0, 0, direction='outgoing')
    add('100', 1, 1, body='on Frİday?')
    add('200', 1, 2, body='A question?')
    rows = queue(order='priority')['items']
    assert sorted(r['provider_chat_id'] for r in rows) == ['100', '200']
    assert not any('awaiting_reply_time_reference' in r for r in rows)


# spec: ANYM-278
@pytest.mark.parametrize('linked', [True, False])
def test_owner_reply_in_the_phone_chat_clears_the_lid_chat_hint(seeded, linked):
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('queue-pair', 'whatsapp', 'Synthetic WhatsApp pair', discover=True)
    key = s.issue_key('queue-pair-agent', ['queue-pair'])
    def add(chat, n, hours, direction):
        at = (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        return ar.ingest(event(account='queue-pair', platform='whatsapp', chat=chat,
            sender=chat if direction == 'incoming' else '555@c.us', direction=direction,
            provider_id=f'{str(direction == "outgoing").lower()}_{chat}_{n}', body=f'Synthetic {n}',
            sent_at=at, updated_at=at, metadata={'chat_type': 'private'}))['message']
    incoming = add('222@lid', 1, 2, 'incoming')
    add('111@c.us', 2, 1, 'outgoing')
    if linked:
        with s.pool.connection() as c:
            c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,
                pn,lid,provenance,first_observed_at,observed_at)
                VALUES('queue-pair',%s,'222@lid','111@c.us','222@lid','whatsmeow_message_alt_v1',now(),now())''', (incoming,))
    result = s.list_chats(key, awaiting_reply=True, include_coverage=False)
    if linked:
        assert (result['items'], result['awaiting_reply_total']) == ([], 0)
    else:
        assert [r['provider_chat_id'] for r in result['items']] == ['222@lid'] and result['awaiting_reply_total'] == 1
    # A newer incoming message in the LID chat is one hint for the conversation.
    add('222@lid', 3, 0.5, 'incoming')
    result = s.list_chats(key, awaiting_reply=True, include_coverage=False)
    assert [r['provider_chat_id'] for r in result['items']] == ['222@lid'] and result['awaiting_reply_total'] == 1
    assert result['items'][0]['awaiting_reply_incoming_count'] == (1 if linked else 2)


# spec: ANYM-278
def test_incoming_after_the_reply_in_both_chats_is_one_hint_counting_both(seeded):
    s, _, _ = seeded
    ar = Archive(s)
    ar.register('queue-both', 'whatsapp', 'Synthetic WhatsApp pair', discover=True)
    key = s.issue_key('queue-both-agent', ['queue-both'])
    def add(chat, n, hours, direction):
        at = (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        return ar.ingest(event(account='queue-both', platform='whatsapp', chat=chat,
            sender=chat if direction == 'incoming' else '555@c.us', direction=direction,
            provider_id=f'{str(direction == "outgoing").lower()}_{chat}_{n}', body=f'Synthetic {n}',
            sent_at=at, updated_at=at, metadata={'chat_type': 'private'}))['message']
    first = add('222@lid', 1, 4, 'incoming')
    add('111@c.us', 2, 3, 'outgoing')
    add('111@c.us', 3, 2, 'incoming')
    newest = add('222@lid', 4, 1, 'incoming')
    with s.pool.connection() as c:
        c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,
            pn,lid,provenance,first_observed_at,observed_at)
            VALUES('queue-both',%s,'222@lid','111@c.us','222@lid','whatsmeow_message_alt_v1',now(),now())''', (first,))
    result = s.list_chats(key, awaiting_reply=True, include_coverage=False)
    assert [r['awaiting_reply_message_id'] for r in result['items']] == [newest] and result['awaiting_reply_total'] == 1
    assert result['items'][0]['awaiting_reply_incoming_count'] == 2
