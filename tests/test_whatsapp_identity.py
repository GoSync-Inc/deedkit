import pytest

from messaging_service.archive import Archive
from messaging_service.collector_state import validate_capture
from messaging_service.whatsapp_collector import WhatsAppState
from messaging_service.whatsapp_identity import personal_id
from messaging_service.store import APIError
from test_whatsapp import event,capture


def observed(mid=1,*,account='wa-identities',chat='900@lid',sender='900@lid',pn='1234567890@c.us',name='Alice'):
    e=event(mid,chat=chat);e.update(senderId=sender,senderName=name)
    e['identityObservations']=[{'identity_id':sender,'pn':pn,'lid':sender.split(':')[0]+'@lid' if ':' in sender else sender,
        'provenance':'webjs_contact_cache_v1','observed_at':'2026-10-14T00:00:00Z'}]
    return capture(e,account=account)


@pytest.mark.parametrize('value,expected',[('900:2@lid','900@lid'),('1234567890:4@c.us','1234567890@c.us'),
    ('900@lid','900@lid'),('900@g.us',None),('900@s.whatsapp.net',None),('name@lid',None),
    ('900:999999@lid',None),('٩٠٠@lid',None),('900:٢@lid',None),(None,None)])
def test_namespace_normalization_never_guesses_phone(value,expected):
    assert personal_id(value)==expected


def test_capture_identity_metadata_stays_out_of_message_revision_and_survives_revalidation():
    value=observed(sender='900:2@lid')
    normalized=validate_capture(value)
    assert normalized['identity_observations'][0]['lid']=='900@lid'
    assert 'identity_observations' not in normalized['envelope']
    assert 'identityObservations' not in str(normalized['envelope'])
    assert normalized==validate_capture(normalized)
    assert normalized['envelope']['sender']=='900:2@lid'


@pytest.mark.parametrize('edit',[
    {'pn':'900@lid'},{'pn':None},{'lid':'1234567890@c.us'},
    {'pn':'١٢٣٤٥٦٧٨٩٠@c.us'},{'lid':'٩٠٠@lid'},
    {'identity_id':'901@lid'},{'provenance':'name_match'},{'observed_at':'yesterday'},
    {'secret':'SYNTHETIC_PRIVATE_PROVIDER_KEY'}])
def test_unverified_malformed_or_foreign_identity_rejected(edit):
    value=observed();value['event']['identityObservations'][0].update(edit)
    with pytest.raises(ValueError):validate_capture(value)


@pytest.fixture
def identity_archive(seeded):
    s,*_=seeded;a=Archive(s)
    a.register('wa-identities','whatsapp','Synthetic identities',discover=True)
    owner='identity-fixture';epoch=a.acquire('wa-identities',owner)
    state=WhatsAppState(a,'wa-identities',(owner,epoch))
    key=s.issue_key('identity-reader',['wa-identities'])
    return s,a,state,key


def test_mapping_enables_phone_search_and_reference_without_rekeying(identity_archive):
    s,a,state,key=identity_archive
    value=observed(sender='900:2@lid');first=state.apply(value)
    rows=s.search_people(key,'1234567890')['items']
    assert len(rows)==1 and rows[0]['sender_id']=='900:2@lid'
    assert rows[0]['identity']['state']=='verified'
    assert rows[0]['identity']['historical_identity_verified'] is False
    assert rows[0]['identity']['source_message']==first['message']
    original=s.get_context(key,first['message'])['message']
    assert original['revision']==1 and original['sender']=='900:2@lid'
    resolved=s.resolve_reference(key,'wa-identities','1234567890@c.us',value['event']['messageId'])
    assert resolved['chat']['provider_chat']=='900@lid'
    assert resolved['chat']['reference_resolution']['method']=='verified_whatsapp_alias'
    assert resolved['message']['id']==first['message']
    assert state.apply(value)['outcome']=='duplicate'
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM whatsapp_identity_observations').fetchone()['n']==1
    assert s.get_context(key,first['message'])['message']['revision']==1
    grouped=s.search_people(key,'1234567890',transliterate=True,group_by_sender=True)['items']
    assert grouped[0]['observations'][0]['identity']['pn']=='1234567890@c.us'


def test_unknown_name_cannot_create_phone_link_and_empty_name_can_use_provider_mapping(identity_archive):
    s,a,state,key=identity_archive
    value=observed(name='');state.apply(value)
    assert s.search_people(key,'1234567890')['items'][0]['display_name']==''
    unverified=observed(mid=2,sender='901@lid',chat='901@lid',pn='1111111111@c.us',name='1234567890')
    del unverified['event']['identityObservations'];state.apply(unverified)
    with pytest.raises(APIError,match='not_found'):s.resolve_reference(key,'wa-identities','1111111111@c.us')


def test_conflicting_pair_is_visible_as_conflict_but_not_used_for_search_or_alias(identity_archive):
    s,a,state,key=identity_archive
    state.apply(observed())
    state.apply(observed(mid=2,pn='1111111111@c.us'))
    assert s.search_people(key,'1234567890')['items']==[]
    row=s.search_people(key,'Alice')['items'][0]
    assert row['identity']['state']=='conflict' and row['identity']['pn'] is None
    assert row['identity']['lid'] is None
    with pytest.raises(APIError,match='not_found'):s.resolve_reference(key,'wa-identities','1234567890@c.us')
    # Exact original IDs remain readable even while namespace links disagree.
    assert s.resolve_reference(key,'wa-identities','900@lid')['chat']['provider_chat']=='900@lid'


def test_hidden_deleted_and_other_account_sources_cannot_supply_alias(identity_archive):
    s,a,state,key=identity_archive
    state.apply(observed());visible=state.apply(observed(mid=2,chat='901@lid',sender='901@lid',pn='1111111111@c.us'))
    visible_chat=s.get_context(key,visible['message'])['message']['chat']
    s.set_access('identity-reader',chats=[visible_chat])
    assert s.search_people(key,'1234567890')['items']==[]
    with pytest.raises(APIError,match='not_found'):s.resolve_reference(key,'wa-identities','1234567890@c.us')
    s.set_access('identity-reader',chats=None)
    delete=observed();delete['change']='delete';delete['observed_at']='2026-10-14T01:00:00Z';state.apply(delete)
    assert s.search_people(key,'1234567890')['items']==[]
    a.register('wa-other','whatsapp','Other synthetic',discover=True)
    epoch=a.acquire('wa-other','other-fixture');other=WhatsAppState(a,'wa-other',('other-fixture',epoch))
    other.apply(observed(account='wa-other',pn='2222222222@c.us'))
    assert s.search_people(key,'2222222222')['items']==[]


def test_metadata_commit_failure_replays_after_message_commit_without_revision_change(identity_archive,monkeypatch):
    s,a,state,key=identity_archive
    from messaging_service import whatsapp_identity
    record=whatsapp_identity.record
    def fail(*args):raise RuntimeError('synthetic_metadata_offline')
    monkeypatch.setattr(whatsapp_identity,'record',fail)
    with pytest.raises(RuntimeError):state.apply(observed())
    assert len(s.search(key)['items'])==1 and s.search_people(key,'1234567890')['items']==[]
    monkeypatch.setattr(whatsapp_identity,'record',record)
    assert state.apply(observed())['outcome']=='duplicate'
    assert s.search_people(key,'1234567890')['items'][0]['identity']['state']=='verified'
    assert s.search(key)['items'][0]['revision']==1


def test_visible_name_does_not_receive_alias_from_hidden_chat(identity_archive):
    s,a,state,key=identity_archive
    state.apply(observed(chat='999@g.us'))
    visible=observed(mid=2,chat='998@g.us',name='Visible Alice')
    del visible['event']['identityObservations']
    mid=state.apply(visible)['message']
    chat=s.get_context(key,mid)['message']['chat']
    s.set_access('identity-reader',chats=[chat])
    rows=s.search_people(key,'Visible Alice')['items']
    assert len(rows)==1 and 'identity' not in rows[0]
    assert s.search_people(key,'1234567890')['items']==[]
    assert s.search_people(key,'1234567890',group_by_sender=True)['items']==[]


def test_mapping_is_account_local_and_one_to_one_in_both_directions(identity_archive):
    s,a,state,key=identity_archive
    state.apply(observed())
    a.register('wa-other','whatsapp','Other synthetic',discover=True)
    epoch=a.acquire('wa-other','other-fixture');other=WhatsAppState(a,'wa-other',('other-fixture',epoch))
    other.apply(observed(account='wa-other',pn='2222222222@c.us'))
    # A different account's valid pair does not conflict with this account.
    assert s.search_people(key,'1234567890')['items'][0]['identity']['state']=='verified'
    # The same PN to two LIDs in one account is also a conflict.
    state.apply(observed(mid=2,chat='901@lid',sender='901@lid'))
    assert s.search_people(key,'1234567890')['items']==[]
    assert all(row['identity']['state']=='conflict' for row in s.search_people(key,'Alice')['items'])


def test_multiple_original_chats_are_ambiguous_and_message_id_is_never_rewritten(identity_archive):
    s,a,state,key=identity_archive
    first=state.apply(observed())
    pn_capture=observed(mid=2,chat='1234567890@c.us')
    state.apply(pn_capture)
    with pytest.raises(APIError,match='ambiguous_reference'):
        s.resolve_reference(key,'wa-identities','1234567890:2@c.us')
    with pytest.raises(APIError,match='not_found'):
        s.resolve_reference(key,'wa-identities','900@lid','false_1234567890@c.us_000001')
    assert s.get_context(key,first['message'])['message']['provider_id']=='false_900@lid_000001'


@pytest.mark.parametrize('provenance', ['webjs_contact_cache_v1',
    'whatsmeow_message_alt_v1', 'whatsmeow_lid_store_v1'])
def test_verified_provider_evidence_is_persisted_and_replayable(identity_archive, provenance):
    """Python validation alone does not prove the SQL constraint accepts evidence."""
    s, _, state, key = identity_archive
    value = observed()
    value['event']['identityObservations'][0]['provenance'] = provenance
    first = state.apply(value)
    assert state.apply(value)['outcome'] == 'duplicate'
    with s.pool.connection() as c:
        row = c.execute('SELECT provenance,source_message FROM whatsapp_identity_observations').fetchone()
    assert row == {'provenance': provenance, 'source_message': first['message']}
    assert s.search_people(key, '1234567890')['items'][0]['identity']['state'] == 'verified'
    assert s.get_context(key, first['message'])['message']['revision'] == 1
