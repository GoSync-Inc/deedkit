"""capabilities: a short summary by default over MCP, named sections on request, today's payload as full (ANYM-270)."""
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from messaging_service import __version__
from messaging_service.app import create_app
from messaging_service.policy import PERMISSIONS
from messaging_service.store import APIError
from test_access import hidden_chat
from test_client_contract import rpc

ROOT=Path(__file__).resolve().parents[1]
SNAPSHOT=ROOT/'tests/fixtures/capabilities-full-snapshot.json'
# Values that follow the clock, the issued key or the release, not the payload's shape.
VOLATILE={'credential_id','expires_at','scope_version','key_permission_version','last_ingest_at','started_at',
          'updated_at','observed_at','checked_at','captured_at','created_at'}


def all_permissions_key(s):
    return s.issue_key('agent-all',['telegram-demo','whatsapp-demo-1'],permissions=sorted(PERMISSIONS))


def masked(value):
    if isinstance(value,dict):return {k:'<volatile>' if k in VOLATILE else masked(v) for k,v in value.items()}
    if isinstance(value,list):return [masked(v) for v in value]
    return '<version>' if value==__version__ else value


def structured(response):
    body=response.json()
    assert 'error' not in body and not body['result'].get('isError'),body
    return body['result']['structuredContent']


def tool_error(response):
    body=response.json()
    assert body['result']['isError'],body
    text=body['result']['content'][0]['text']
    return json.loads(text[text.index('{'):])


# spec: ANYM-270
def test_capabilities_summary_within_1500_chars(seeded):
    s,_,_=seeded
    key=all_permissions_key(s)
    full=s.capabilities(key,detail='full')
    with TestClient(create_app(s,['testserver'])) as client:
        summary=structured(rpc(client,key,'capabilities',{}))
    assert len(json.dumps(summary))<=1500,len(json.dumps(summary))
    assert summary==s.capabilities(key,detail='summary')
    assert summary['detail']=='summary' and summary['sections']==[
        'identity','accounts','coverage','tool_manifest','search_options','sending','file_uploads',
        'subscriptions','feedback','messenger_operations','limits']
    assert 'sections' in summary['next_action'] and 'detail=full' in summary['next_action']
    for name in ('service','version','principal','credential_id','permissions','expires_at'):
        assert summary[name]==full[name]
    assert summary['accounts']==[{k:a[k] for k in ('id','platform','label','self_provider_id')} for a in full['accounts']]
    assert len(summary['accounts'])==2
    assert summary['chat_scope']=={'all_in_permitted_accounts':True,'chat_count':None}
    assert summary['tool_manifest']=={'version':full['tool_manifest']['version']}


# spec: ANYM-270
def test_capabilities_full_matches_previous_payload(seeded,monkeypatch):
    s,_,_=seeded
    # The snapshot's build.source_sha is null; an image's test stage may carry a real commit (ANYM-592).
    monkeypatch.delenv('DEEDKIT_SOURCE_SHA',raising=False)
    key=all_permissions_key(s)
    full=s.capabilities(key,detail='full')
    assert masked(full)==json.loads(SNAPSHOT.read_text())
    headers={'Authorization':'Bearer '+key}
    with TestClient(create_app(s,['testserver'])) as client:
        assert client.post('/v1/capabilities',headers=headers,json={'detail':'full'}).json()==full
        assert structured(rpc(client,key,'capabilities',{'detail':'full'}))==full
    assert s.capabilities(key)==full


# spec: ANYM-270
def test_capabilities_sections_return_named_blocks(seeded):
    s,_,_=seeded
    key=all_permissions_key(s)
    full=s.capabilities(key,detail='full')
    assert s.capabilities(key,sections=['search_options'])=={'search_options':full['search_options']}
    assert s.capabilities(key,sections=['subscriptions','limits'])=={
        name:full[name] for name in ('subscriptions','events','limits','unavailable')}
    # Every key of today's payload sits in exactly one section.
    names=s.capabilities(key,detail='summary')['sections']
    blocks=[s.capabilities(key,sections=[name]) for name in names]
    assert sum(len(block) for block in blocks)==len(full)
    assert s.capabilities(key,sections=names)==full
    with pytest.raises(APIError,match='invalid_filter'):s.capabilities(key,sections=['search_options','unknown'])
    headers={'Authorization':'Bearer '+key}
    with TestClient(create_app(s,['testserver'])) as client:
        assert structured(rpc(client,key,'capabilities',{'sections':['sending']}))=={'sending':full['sending']}
        assert client.post('/v1/capabilities',headers=headers,json={'sections':['sending']}).json()=={'sending':full['sending']}
        assert tool_error(rpc(client,key,'capabilities',{'sections':['nope']}))['error']=='invalid_filter'
        assert client.post('/v1/capabilities',headers=headers,json={'sections':['nope']}).json()['error']=='invalid_filter'
        assert 'detail' in rpc(client,key,'capabilities',{'detail':'everything'}).json()['error']['data']['fields']
        assert client.post('/v1/capabilities',headers=headers,json={'detail':'everything'}).json()['error']=='invalid_parameters'


# spec: ANYM-270
def test_capabilities_sections_stay_inside_chat_scope(seeded):
    s,_,b=seeded;hidden_chat(s)
    s.set_access('agent-b',chats=['telegram-demo-chat'])
    names=s.capabilities(b,detail='summary')['sections']
    outputs=[s.capabilities(b,detail='summary'),*(s.capabilities(b,sections=[name]) for name in names)]
    assert 'HIDDEN_' not in json.dumps(outputs) and 'hidden-chat' not in json.dumps(outputs)
    assert outputs[0]['chat_scope']=={'all_in_permitted_accounts':False,'chat_count':1}
    assert 'whatsapp-demo-1' not in json.dumps(outputs)
    s.set_access('agent-b',chats=[])
    summary=s.capabilities(b,detail='summary')
    assert summary['accounts']==[] and s.capabilities(b,sections=['accounts'])=={'accounts':[]}
    assert s.capabilities(b,sections=['messenger_operations'])['messenger_operations']['supported_by_account']=={}


# spec: ANYM-270
def test_capabilities_operations_section_lists_only_operations_the_key_may_use(seeded):
    from messaging_service.messenger_operations import operation_manifest
    s,_,_=seeded
    whole=s.issue_key('agent-whole',['telegram-demo'],permissions=sorted(PERMISSIONS))
    scoped=s.issue_key('agent-scoped',['telegram-demo'],chats=['telegram-demo-chat'],permissions=sorted(PERMISSIONS))
    account=next(a for a in s.capabilities(scoped)['accounts'] if a['id']=='telegram-demo')
    full_account={row['operation'] for row in operation_manifest(account['platform']) if row['full_account']}
    listed=s.capabilities(scoped,detail='full')['messenger_operations']['supported_by_account']['telegram-demo']
    # full keeps the previous payload, which lists them; the precondition makes the check below bite.
    assert full_account & set(listed['operations'])
    headers={'Authorization':'Bearer '+scoped}
    with TestClient(create_app(s,['testserver'])) as client:
        views=[s.capabilities(scoped,sections=['messenger_operations']),
               structured(rpc(client,scoped,'capabilities',{'sections':['messenger_operations']})),
               client.post('/v1/capabilities',headers=headers,json={'sections':['messenger_operations']}).json()]
    for view in views:
        row=view['messenger_operations']['supported_by_account']['telegram-demo']
        assert not full_account & set(row['operations']) and not full_account & set(row['available_operations'])
        assert row['count']==len(row['operations'])>0 and set(row['operations'])<set(listed['operations'])
    assert views[0]==views[1]==views[2]
    # A key over the whole account keeps every supported operation in the section.
    assert (s.capabilities(whole,sections=['messenger_operations'])['messenger_operations']
            ==s.capabilities(whole,detail='full')['messenger_operations'])


# spec: ANYM-270
def test_rest_capabilities_default_to_summary(seeded):
    s,_,_=seeded
    key=all_permissions_key(s)
    with TestClient(create_app(s,['testserver'])) as client:
        rest=client.post('/v1/capabilities',headers={'Authorization':'Bearer '+key},json={}).json()
        assert rest==structured(rpc(client,key,'capabilities',{}))
    assert rest['detail']=='summary' and len(json.dumps(rest))<=1500
