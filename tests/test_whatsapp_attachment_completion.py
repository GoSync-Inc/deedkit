"""ANYM-808: a WhatsApp photo, video or document completes a revision stored as unknown, no text, no file."""
from datetime import datetime
import hashlib

import pytest

from messaging_service.archive import Archive,ingest_outcome
from messaging_service.envelope import fingerprint,validate

UNREADABLE_FORMAT={'version':1,'state':'unknown','basis':None,'wire_sha256':None,'entities':None,
    'unsupported_entities':[],'warnings':['native_entities_not_captured']}
PHOTO={'provider_id':'f1','mime':'image/jpeg','bytes':10,'filename':'p.jpg'}
VIDEO={'provider_id':'f2','mime':'video/mp4','bytes':20,'filename':'v.mp4'}
DOCUMENT={'provider_id':'f3','mime':'application/pdf','bytes':2048,'filename':'report.pdf'}
FILES={'photo':PHOTO,'video':VIDEO,'document':DOCUMENT}


def wire(body):
    return {'version':1,'state':'wire_observed','basis':'whatsapp_native_wire','wire_sha256':hashlib.sha256(body.encode()).hexdigest(),
        'entities':None,'unsupported_entities':[],'warnings':['syntax_interpretation_unknown']}


def wa_event(**changes):
    value={'account':'archive-wa','platform':'whatsapp','chat':'900@lid','chat_title':'Synthetic Contact',
        'provider_id':'false_900@lid_3EB0SYNTH','sender':'900@lid','body':'','sent_at':'2026-10-01T12:00:00+00:00',
        'updated_at':'2026-10-01T12:00:00+00:00','deleted':False,'direction':'incoming','origin':'live','media':[],
        'formatting':UNREADABLE_FORMAT,'metadata':{'message_kind':'unknown','chat_type':'private'}}
    value.update(changes); return value


def empty(**changes):
    """The stored shape: kind unknown, no text, no file."""
    return wa_event(**changes)


def attachment(kind='photo',caption='',**changes):
    return wa_event(**{'origin':'reconcile','body':caption,'formatting':wire(caption),'media':[FILES.get(kind,PHOTO)],
        'metadata':{'message_kind':kind,'chat_type':'private'},**changes})


def snapshot(value,revision=1):
    return {**validate(value),'message':'m','provider_chat':value['chat'],'chat':'c','revision':revision}


def retained_row(value):
    e=validate(value)
    return {'sent_at':datetime.fromisoformat(e['sent_at']),'direction':e['direction'],'sender':e['sender'],
        'deleted':e['deleted'],'body':e['body'],'content_hash':fingerprint(e),'revision':1,
        'provider_updated_at':datetime.fromisoformat(e['updated_at'])}


def outcome(retained,candidate):
    return ingest_outcome(retained_row(retained),validate(candidate),snapshot(retained))


# spec: ANYM-808
@pytest.mark.parametrize('kind',['photo','video','document'])
@pytest.mark.parametrize('caption',['','Подпись lighthouse'])
@pytest.mark.parametrize('retained',[
    {},
    {'metadata':{'chat_type':'private'}},
    {'formatting':wire('')}],ids=['unknown','no_kind','known_empty_format'])
def test_an_attachment_completes_an_empty_unknown_revision(kind,caption,retained):
    from messaging_service.archive import whatsapp_attachment_completion
    stored,candidate=empty(**retained),attachment(kind,caption)
    result=outcome(stored,candidate)
    assert result[0]=='edited' and result[-1] is True
    assert whatsapp_attachment_completion(snapshot(stored),validate(candidate)) is True


# spec: ANYM-808
@pytest.mark.parametrize('retained,candidate,expected',[
    # The retained revision has text or a file.
    ({'body':'Старый текст','formatting':wire('Старый текст')},{},'conflict'),
    ({'media':[DOCUMENT]},{},'conflict'),
    # Kinds outside photo, video and document.
    ({},{'metadata':{'message_kind':'unknown','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'message_kind':'audio','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'message_kind':'voice','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'message_kind':'sticker','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'message_kind':'location','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'message_kind':'text','chat_type':'private'}},'conflict'),
    ({},{'metadata':{'chat_type':'private'}},'conflict'),
    # Two files, no file.
    ({},{'media':[PHOTO,VIDEO]},'conflict'),
    ({},{'media':[],'formatting':UNREADABLE_FORMAT},'unchanged'),
    # Another identity.
    ({},{'sender':'901@lid'},'conflict'),
    ({},{'sent_at':'2026-10-01T11:59:59+00:00'},'conflict'),
    ({},{'direction':'outgoing'},'conflict'),
    # A tombstone on either side.
    ({'deleted':True},{},'tombstoned'),
    ({},{'deleted':True,'media':[],'formatting':UNREADABLE_FORMAT},'deleted'),
    # Another provider time: the ordinary edit and the stale observation.
    ({},{'updated_at':'2026-10-01T12:01:00+00:00'},'edited'),
    ({'updated_at':'2026-10-01T12:01:00+00:00'},{},'stale')],
    ids=['retained_text','retained_file','unknown','audio','voice','sticker','location','text',
         'no_kind','two_files','no_file','sender','sent_at','direction','retained_tombstone','candidate_tombstone',
         'newer','older'])
def test_other_shapes_keep_todays_outcome(retained,candidate,expected):
    from messaging_service.archive import whatsapp_attachment_completion
    stored,observed=empty(**retained),attachment(**candidate)
    result=outcome(stored,observed)
    assert result[0]==expected and result[-1] is False
    assert whatsapp_attachment_completion(snapshot(stored),validate(observed)) is False


# spec: ANYM-808
def test_unreadable_evidence_is_no_completion():
    from messaging_service.archive import whatsapp_attachment_completion
    candidate=validate(attachment())
    retained=snapshot(empty())
    for broken in (None,{},{**retained,'metadata':'unknown'},{**retained,'updated_at':'not a time'},{**retained,'media':None}):
        assert whatsapp_attachment_completion(broken,candidate) is False
    # A retained known formatting that states text the revision does not have contradicts the completion.
    stated={**retained,'formatting':wire('report.pdf')}
    assert whatsapp_attachment_completion(stated,candidate) is False
    assert ingest_outcome(retained_row(empty()),candidate,stated)[0]=='conflict'
    assert whatsapp_attachment_completion(retained,{**candidate,'media':['not a descriptor']}) is False
    telegram={**candidate,'platform':'telegram'}
    assert whatsapp_attachment_completion(retained,telegram) is False


# spec: ANYM-808
def test_conflict_reevaluation_no_longer_raises_a_stored_completion():
    from messaging_service.conflict_reevaluation import still_raised,telegram_link_completed
    stored=empty()
    row={'envelope':validate(attachment()),'observed':snapshot(stored),
         'candidate_updated_at':datetime.fromisoformat(stored['updated_at']),'collected':True,'tombstoned':False,
         'reconstructed':False,'observed_revision':1,'archive_revision':1,
         'archive_version':datetime.fromisoformat(stored['updated_at'])}
    assert still_raised(row) is False
    # Another shape stays raised, and the reader of element [0] for Telegram links is unaffected.
    assert still_raised({**row,'envelope':validate(attachment(media=[PHOTO,VIDEO]))}) is True
    assert telegram_link_completed(row) is None


@pytest.fixture()
def archive(seeded):
    s,_,_=seeded
    ar=Archive(s); ar.register('archive-wa','whatsapp','Synthetic WhatsApp',include_chats=['900@lid'])
    return ar,s


def state(s,mid):
    with s.pool.connection() as c:
        return {
            'message':c.execute('SELECT body,revision,content_hash FROM messages WHERE id=%s',(mid,)).fetchone(),
            'revisions':[r['snapshot'] for r in c.execute(
                'SELECT snapshot FROM message_revisions WHERE message=%s ORDER BY revision',(mid,))],
            'metadata':c.execute('SELECT revision,metadata FROM message_metadata WHERE message=%s',(mid,)).fetchone(),
            'jobs':[dict(r) for r in c.execute('SELECT descriptor,state FROM media_jobs WHERE message=%s ORDER BY id',(mid,))],
            'events':[(r['kind'],r['revision'],r['origin']) for r in c.execute(
                'SELECT kind,revision,origin FROM events WHERE message=%s ORDER BY id',(mid,))],
            'conflicts':[(r['reason'],r['resolved_at']) for r in c.execute(
                'SELECT reason,resolved_at FROM message_conflicts WHERE message=%s ORDER BY observed_at',(mid,))]}


# spec: ANYM-808
@pytest.mark.parametrize('kind',['photo','video','document'])
def test_ingest_completes_the_revision_and_queues_its_file(archive,kind):
    ar,s=archive
    mid=ar.ingest(empty())['message']
    assert ar.ingest(attachment(kind))['outcome']=='edited'
    after=state(s,mid)
    assert after['message']['revision']==2 and after['conflicts']==[]
    # Revision 1 stays readable as it was; revision 2 carries the file.
    first,second=after['revisions']
    assert first['media']==[] and first['body']=='' and first['metadata']['message_kind']=='unknown'
    assert [m['provider_id'] for m in second['media']]==[FILES[kind]['provider_id']] and second['revision']==2
    assert [(j['descriptor']['provider_id'],j['state']) for j in after['jobs']]==[(FILES[kind]['provider_id'],'queued')]
    assert after['metadata']['revision']==2 and after['metadata']['metadata']['message_kind']==kind
    assert after['events']==[('message.created',1,'live'),('message.edited',2,'reconcile')]


# spec: ANYM-808
def test_a_completion_resolves_no_open_conflict(archive):
    ar,s=archive
    mid=ar.ingest(empty(updated_at='2026-10-01T12:00:30+00:00'))['message']
    # An older contradiction: any later evidence would resolve it, a completion is none.
    assert ar.ingest(empty(direction='outgoing',origin='reconcile'))['outcome']=='conflict'
    before=state(s,mid)['conflicts']
    assert len(before)==1 and before[0][1] is None
    assert ar.ingest(attachment(updated_at='2026-10-01T12:00:30+00:00'))['outcome']=='edited'
    assert state(s,mid)['conflicts']==before


# spec: ANYM-808
def test_replay_and_order_after_a_completion(archive):
    ar,s=archive
    mid=ar.ingest(empty())['message']
    assert ar.ingest(attachment())['outcome']=='edited'
    # The same envelope again is answered by the receipt check.
    assert ar.ingest(attachment())['outcome']=='duplicate'
    after=state(s,mid)
    assert len(after['revisions'])==2 and len(after['jobs'])==1
    assert [e for e in after['events'] if e[0]=='message.edited']==[('message.edited',2,'reconcile')]
    # The original empty shape read again (another origin, so another receipt) stays a conflict: the unreadable
    # rule needs retained text (ANYM-613), and this card adds no rule for it.
    assert ar.ingest(empty(origin='backfill'))['outcome']=='conflict'
    # A second, different file at the same provider version against the completed revision stays a conflict.
    assert ar.ingest(attachment('document'))['outcome']=='conflict'
    final=state(s,mid)
    assert final['message']['revision']==2 and len(final['jobs'])==1 and len(final['conflicts'])==2
