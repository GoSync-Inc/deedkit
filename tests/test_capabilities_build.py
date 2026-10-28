"""capabilities name the commit the messaging server was built from and since when it runs (ANYM-592)."""
from datetime import datetime, timezone
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from messaging_service import __version__, store
from messaging_service.app import create_app
from test_capabilities_summary import all_permissions_key, structured
from test_client_contract import rpc

ROOT=Path(__file__).resolve().parents[1]
SOURCE_SHA='0123456789abcdef0123456789abcdef01234567'
STARTED=datetime(2026,10,28,9,30,tzinfo=timezone.utc)
STARTED_TEXT='2026-10-28T09:30:00.000000Z'


# spec: ANYM-592
@pytest.mark.parametrize('value',[None,'','abc',SOURCE_SHA+'0',SOURCE_SHA.upper()],
                         ids=['absent','empty','short','41_chars','uppercase'])
def test_build_source_sha_is_null_without_a_valid_commit(value,monkeypatch):
    if value is None:monkeypatch.delenv('DEEDKIT_SOURCE_SHA',raising=False)
    else:monkeypatch.setenv('DEEDKIT_SOURCE_SHA',value)
    monkeypatch.setattr(store,'STARTED_AT',STARTED)
    assert store.build()=={'source_sha':None,'started_at':STARTED_TEXT}


# spec: ANYM-592
def test_capabilities_name_the_source_commit_in_every_view(seeded,monkeypatch):
    s,_,_=seeded
    monkeypatch.setenv('DEEDKIT_SOURCE_SHA',SOURCE_SHA)
    monkeypatch.setattr(store,'STARTED_AT',STARTED)
    expected={'source_sha':SOURCE_SHA,'started_at':STARTED_TEXT}
    key=all_permissions_key(s)
    full=s.capabilities(key,detail='full')
    assert full['build']==expected
    assert s.capabilities(key,detail='summary')['build']==expected
    assert s.capabilities(key,sections=['identity'])['build']==expected
    assert full['version']==full['tool_manifest']['version']==__version__
    headers={'Authorization':'Bearer '+key}
    with TestClient(create_app(s,['testserver'])) as client:
        assert structured(rpc(client,key,'capabilities',{}))['build']==expected
        assert client.post('/v1/capabilities',headers=headers,json={}).json()['build']==expected
        # Computed on every read: a new value shows on the next call, nothing is cached.
        monkeypatch.setenv('DEEDKIT_SOURCE_SHA','f'*40)
        assert client.post('/v1/capabilities',headers=headers,json={'detail':'full'}).json()['build']['source_sha']=='f'*40


# spec: ANYM-592
def test_capabilities_description_names_the_source_commit(seeded):
    s,key,_=seeded
    with TestClient(create_app(s,['testserver'])) as client:
        tools={t['name']:t for t in client.post('/mcp',headers={'Authorization':'Bearer '+key,
            'Accept':'application/json, text/event-stream'},
            json={'jsonrpc':'2.0','id':1,'method':'tools/list'}).json()['result']['tools']}
    description=tools['capabilities']['description']
    catalog=json.loads((ROOT/'services/gateway/src/agent_gateway/catalog.json').read_text())
    assert next(t for t in catalog if t['name']=='messages_capabilities')['description']==description
    assert 'build.source_sha' in description
