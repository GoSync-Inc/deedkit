"""ANYM-963: old WhatsApp rows stored as unknown regain text or a file from the native journal, as a new revision.

The Go fixtures run through test_whatsmeow_contract.py's binary harness (`--republish-fixture`): a throwaway journal
of synthetic messages stored the way the classifier before ANYM-471 did, then the ANYM-809 count and the republish
pass. Their envelopes go through the production spool validator into the archive. Synthetic data only.
"""
import json
import subprocess

import pytest

from messaging_service.archive import Archive,whatsapp_attachment_completion,whatsapp_body_completion
from messaging_service.collector_state import validate_capture
from messaging_service.envelope import stable_id
from messaging_service.presentation import render
from messaging_service.subscriptions import match_subscriptions
from messaging_service.whatsapp_collector import WhatsAppState,validate_native_repair_status
from test_whatsmeow_contract import _private_json,native_fixture_binary as native_fixture_binary  # noqa: F401  (a fixture)

ACCOUNT='wa-native-test'
CHAT='123@c.us'
BASE={'synthetic':True,'account':ACCOUNT,'chat':CHAT,'sender':CHAT,'owner':'999@c.us','timestamp':1700000000,'from_me':False}
MESSAGES=[
    {'id':'T1','kind':'template','text':'Check-in at 14:00'},
    {'id':'P1','kind':'album_photo','text':'site visit','mime':'image/jpeg','bytes':2048},
    {'id':'D1','kind':'document','text':'','filename':'synthetic.pdf','mime':'application/pdf','bytes':512},
    {'id':'L1','kind':'location','text':'Synthetic place'},
]


def republish(binary,tmp_path,messages=MESSAGES):
    source=_private_json(tmp_path/'republish.json',{'synthetic':True,'messages':[{**BASE,**m} for m in messages]})
    answer=subprocess.run([str(binary),'--republish-fixture',str(source)],text=True,capture_output=True,timeout=60)
    assert answer.returncode==0,answer.stderr
    result=json.loads(answer.stdout)
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('republish-fixture-')]  # the throwaway journal is gone
    return result


def envelope(raw):
    return validate_capture(raw)['envelope']


def revision(store,message,number):
    with store.pool.connection() as c:
        row=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s AND revision=%s',(message,number)).fetchone()
    return row and row['snapshot']


@pytest.fixture
def archive(seeded):
    """The spool consumer's own path: WhatsAppState.apply under a collector lease."""
    store=seeded[0]
    archive=Archive(store)
    archive.register(ACCOUNT,'whatsapp','Synthetic native',include_chats=[CHAT])
    epoch=archive.acquire(ACCOUNT,'republish-test')
    yield store,archive,WhatsAppState(archive,ACCOUNT,('republish-test',epoch))
    archive.release(ACCOUNT,'republish-test',epoch)


# spec: ANYM-963
def test_republished_envelopes_complete_the_unknown_revision_as_a_new_one(native_fixture_binary,tmp_path,archive):
    store,a,state=archive
    result=republish(native_fixture_binary,tmp_path)
    journaled=[envelope(raw) for raw in result['journaled']]
    assert [e['metadata']['message_kind'] for e in journaled]==['unknown']*4
    assert all(e['body']=='' and e['media']==[] for e in journaled)
    messages={e['provider_id']:state.apply(raw)['message'] for e,raw in zip(journaled,result['journaled'])}
    published=[envelope(raw) for raw in result['published']]
    # The location is would_add_content but no completion rule takes it: unsupported, never published.
    assert sorted(e['provider_id'].split('_')[-1] for e in published)==['D1','P1','T1']
    status=result['status']['republish']
    assert status=={'version':1,'done':True,'rows_scanned':4,'published':3,'unsupported':1,'parse_error':0,'last_error':''}
    assert status['published']+status['unsupported']==result['status']['counts']['would_add_content']
    for candidate,raw in zip(published,result['published']):
        assert candidate['origin']=='reconcile'
        message=messages[candidate['provider_id']]
        retained=revision(store,message,1)
        assert whatsapp_body_completion(retained,candidate) or whatsapp_attachment_completion(retained,candidate)
        assert state.apply(raw)['outcome']=='edited'
        # The new revision has the text or the file; the empty one stays readable.
        assert revision(store,message,1)['body']=='' and revision(store,message,1)['media']==[]
        latest=revision(store,message,2)
        assert latest['body']==candidate['body'] and latest['media']==candidate['media']
        assert latest['metadata']['message_kind'] in ('text','photo','document')
    # A repeat is the archive's duplicate, not a third revision.
    for candidate,raw in zip(published,result['published']):
        assert state.apply(raw)['outcome']=='duplicate'
        assert revision(store,messages[candidate['provider_id']],3) is None


# spec: ANYM-963
def test_a_deleted_message_stays_deleted_and_an_excluded_chat_is_excluded(native_fixture_binary,tmp_path,archive):
    store,a,state=archive
    result=republish(native_fixture_binary,tmp_path,MESSAGES[:2])
    template,photo=result['published']
    deleted=state.apply(result['journaled'][0])['message']
    state.apply(result['journaled'][1])
    state.apply({'kind':'whatsapp_delete','account':ACCOUNT,'chat':CHAT,'ids':[envelope(template)['provider_id']],
        'observed_at':'2026-11-11T00:00:00+00:00'})
    # The provider tombstone turns the re-read into a deletion (archive _ingest_dedupe): nothing to restore.
    assert state.apply(template)['outcome'] in ('tombstoned','unchanged')
    with store.pool.connection() as c:
        row=c.execute('SELECT body,deleted FROM messages WHERE id=%s',(deleted,)).fetchone()
        bodies=[r['snapshot']['body'] for r in c.execute('SELECT snapshot FROM message_revisions WHERE message=%s',(deleted,))]
    assert row['deleted'] is True and row['body']=='' and set(bodies)=={''}
    # The chat left the collection policy before the pass reached it.
    a.register(ACCOUNT,'whatsapp','Synthetic native',discover=True,exclude_chats=[CHAT])
    assert state.apply(photo)['outcome']=='excluded'
    assert a.ingest(envelope(photo))['outcome']=='excluded'  # the archive's own policy check too


# spec: ANYM-963
def test_a_default_live_subscription_is_not_woken_by_the_republished_revision(native_fixture_binary,tmp_path,archive):
    store,a,state=archive
    result=republish(native_fixture_binary,tmp_path,MESSAGES[:1])
    state.apply(result['journaled'][0])
    permissions=['read','subscription-write']
    token=store.issue_key('republish-subscriber',[ACCOUNT],permissions=permissions,key_permissions=permissions)
    chat=stable_id('chat',ACCOUNT,CHAT)
    kinds=['message.created','message.edited']
    live=store.create_subscription(token,[chat],kinds=kinds)['subscription_id']  # origins default to ['live']
    every=store.create_subscription(token,[chat],kinds=kinds,origins=['live','reconcile'])['subscription_id']
    assert state.apply(result['published'][0])['outcome']=='edited'
    match_subscriptions(store)
    assert store.get_subscription_events(token,live,limit=50)['items']==[]
    items=store.get_subscription_events(token,every,limit=50)['items']
    assert [i['kind'] for i in items]==['message.edited']


def repair_status(**republish):
    value={'version':1,'done':True,'rows_scanned':107,
           'counts':{'would_add_content':68,'label_only':4,'same':2,'no_raw':0,'placeholder':30,'sender_key_only':3,'parse_error':0},
           'last_error':'','observed_at':'2026-11-11T10:00:00Z'}
    if republish:
        value['republish']={'version':1,'done':False,'rows_scanned':50,'published':20,'unsupported':2,'parse_error':0,
                            'last_error':'',**republish}
    return value


# spec: ANYM-963
def test_repair_status_carries_the_republish_counts_to_sync_status(seeded):
    s,*_=seeded
    # The ANYM-809 shape alone stays valid: a count without a started republish pass.
    assert 'republish' not in validate_native_repair_status(repair_status())
    status=validate_native_repair_status(repair_status(last_error='republish_spool'))
    assert status['republish']=={'version':1,'done':False,'rows_scanned':50,'published':20,'unsupported':2,
                                 'parse_error':0,'last_error':'republish_spool'}
    for unsafe in ({'body':'private text'},{'last_error':'message 123 failed'},{'published':-1},{'published':True},
                   {'done':'yes'},{'version':0},{'last_error':'repair_read'}):
        with pytest.raises(ValueError,match='invalid_native_repair_status'):
            validate_native_repair_status(repair_status(**unsafe))
    with pytest.raises(ValueError,match='invalid_native_repair_status'):
        validate_native_repair_status({**repair_status(),'republish':None})
    a=Archive(s)
    a.register('wa-republish','whatsapp','Synthetic native republish',discover=True)
    epoch=a.acquire('wa-republish','republish-test')
    state=WhatsAppState(a,'wa-republish',('republish-test',epoch))
    key=s.issue_key('republish-agent',['wa-republish'],permissions=['read'])
    state.provider_observation('whatsmeow')
    state.native_repair_observation(repair_status(done=True,published=66,unsupported=2,rows_scanned=107))
    result=s.get_sync_status(key)
    row=next(x for x in result['sources'] if x['account']=='wa-republish')
    assert row['native_repair']['publishes'] is True
    assert row['native_repair']['republish']=={'version':1,'done':True,'rows_scanned':107,'published':66,
                                               'unsupported':2,'parse_error':0,'last_error':''}
    full=render('get_sync_status',result,'full')
    assert next(x for x in full['sources'] if x['account']=='wa-republish')['native_repair']['republish']['published']==66
    a.release('wa-republish','republish-test',epoch)
