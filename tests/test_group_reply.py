"""Personal reply queue: explicit group attention only, using synthetic sources."""
import pytest

from messaging_service.archive import Archive
from messaging_service.envelope import stable_id,validate
from messaging_service.presentation import render
from test_archive import event


@pytest.fixture()
def groups(seeded):
    s,_,_=seeded
    ar=Archive(s)
    ar.register('reply-tg','telegram','Synthetic groups',discover=True)
    key=s.issue_key('reply-reader',['reply-tg'])
    def add(chat,mid,second,**changes):
        at=f'2026-10-01T12:00:{second:02d}Z'
        result=ar.ingest(event(**{'account':'reply-tg','chat':chat,'provider_id':mid,
            'sent_at':at,'updated_at':at,**changes}))
        with s.pool.connection() as c:
            c.execute("UPDATE chats SET chat_type='group' WHERE id=%s",(stable_id('chat','reply-tg',chat),))
        return result['message']
    return s,key,add


def test_group_mentions_survive_other_peoples_chatter(groups):
    s,key,add=groups
    target=add('100','1',1,body='Owner, please check',metadata={'mentions_owner':True})
    noise=add('100','2',2,body='Unrelated conversation')
    add('200','1',3,body='Just a group message')
    result=s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)
    assert result['awaiting_reply_total']==1
    row=result['items'][0]
    assert row['awaiting_reply_message_id']==target
    assert row['last_message_id']==noise
    assert row['awaiting_reply_basis']=='owner_mention'
    assert result['awaiting_reply_scope']=='private_and_explicit_group_attention'
    compact=render('list_chats',result,'compact')['items'][0]
    assert compact['awaiting_reply_basis']=='owner_mention'
    assert compact['awaiting_reply_message_id']==target


def test_only_replies_to_owners_archived_message_enter_queue(groups):
    s,key,add=groups
    add('100','10',1,direction='outgoing')
    target=add('100','11',2,metadata={'reply_to_provider_id':'10'})
    add('100','12',3)
    add('200','10',1,direction='incoming')
    add('200','11',2,metadata={'reply_to_provider_id':'10'})
    add('300','11',2,metadata={'reply_to_provider_id':'10'})
    result=s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)
    assert result['awaiting_reply_total']==1
    assert result['items'][0]['awaiting_reply_message_id']==target
    assert result['items'][0]['awaiting_reply_basis']=='reply_to_owner_message'


def test_owner_message_clears_group_hint_but_new_mention_reopens(groups):
    s,key,add=groups
    add('100','1',1,metadata={'mentions_owner':True})
    add('100','2',2,direction='outgoing')
    assert not s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items']
    target=add('100','3',3,metadata={'mentions_owner':True})
    assert s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items'][0]['awaiting_reply_message_id']==target


def test_attention_respects_chat_scope_channel_type_deletion_and_revision(groups):
    s,key,add=groups
    add('100','1',1,metadata={'mentions_owner':True})
    channel=add('200','1',2,metadata={'mentions_owner':True})
    removed=add('300','1',3,metadata={'mentions_owner':True})
    stale=add('400','1',4,metadata={'mentions_owner':True})
    with s.pool.connection() as c:
        c.execute("UPDATE chats SET chat_type='channel' WHERE id=(SELECT chat FROM messages WHERE id=%s)",(channel,))
        c.execute('UPDATE messages SET deleted=true WHERE id=%s',(removed,))
        c.execute('UPDATE message_metadata SET revision=0 WHERE message=%s',(stale,))
    s.set_access('reply-reader',chats=[stable_id('chat','reply-tg',x) for x in ['200','300','400']])
    assert not s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items']


def test_mention_metadata_requires_boolean():
    assert validate(event(metadata={'mentions_owner':True}))['metadata']['mentions_owner'] is True
    for value in ['true',1,[],{}]:
        with pytest.raises(ValueError,match='invalid_metadata'):
            validate(event(metadata={'mentions_owner':value}))


def test_partial_reobservation_does_not_erase_known_owner_attention(groups):
    s,key,add=groups
    mid=add('100','1',1,metadata={'mentions_owner':True})
    add('100','1',1,metadata={'mentions_owner':None,'forwarded':False})
    with s.pool.connection() as c:
        assert c.execute('SELECT metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone()['metadata']['mentions_owner'] is True
    assert s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['awaiting_reply_total']==1


def test_older_metadata_cannot_erase_a_newer_owner_observation(groups):
    s,key,add=groups
    add('100','1',1,updated_at='2026-10-01T12:01:00Z',metadata={'mentions_owner':True})
    add('100','1',1,metadata={'mentions_owner':False})
    assert s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['awaiting_reply_total']==1


def test_whatsapp_stanza_reply_matches_only_same_account_and_group(seeded):
    s,_,_=seeded;ar=Archive(s)
    for account in ['reply-wa','other-wa']:
        ar.register(account,'whatsapp','Synthetic WA',discover=True)
    def add(account,chat,provider,direction,at,metadata=None):
        ar.ingest(event(account=account,platform='whatsapp',chat=chat,provider_id=provider,
            direction=direction,sent_at=at,updated_at=at,metadata=metadata or {}))
        with s.pool.connection() as c:c.execute("UPDATE chats SET chat_type='group' WHERE id=%s",(stable_id('chat',account,chat),))
    add('reply-wa','123@g.us','true_123@g.us_ABC_42@lid','outgoing','2026-10-01T12:00:01Z')
    add('reply-wa','123@g.us','false_123@g.us_DEF_43@lid','incoming','2026-10-01T12:00:02Z',{'reply_to_provider_id':'ABC'})
    add('reply-wa','999@g.us','false_999@g.us_GHI','incoming','2026-10-01T12:00:03Z',{'reply_to_provider_id':'ABC'})
    add('other-wa','123@g.us','false_123@g.us_JKL','incoming','2026-10-01T12:00:04Z',{'reply_to_provider_id':'ABC'})
    key=s.issue_key('wa-reply-reader',['reply-wa','other-wa'])
    rows=s.list_chats(key,awaiting_reply=True,awaiting_reply_days=0,include_coverage=False)['items']
    assert len(rows)==1 and rows[0]['account']=='reply-wa'
    assert rows[0]['provider_chat_id']=='123@g.us'
