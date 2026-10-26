"""Without the gateway the `subscriptions` and `events` capability blocks are unchanged (ANYM-528 criterion 2)."""
import json

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from test_capabilities_summary import SNAPSHOT, all_permissions_key, masked, structured
from test_client_contract import rpc


# spec: ANYM-528
@pytest.mark.parametrize('permissions',[['read'],None],ids=['reader','all_permissions'])
def test_direct_subscription_blocks_are_unchanged(seeded,permissions):
    s,_,_=seeded
    key=all_permissions_key(s) if permissions is None else s.issue_key('agent-reader',['telegram-demo'],permissions=permissions)
    expected={k:json.loads(SNAPSHOT.read_text())[k] for k in ('subscriptions','events')}
    # The backend names its own operations; the gateway's filter and public names never reach it.
    assert expected['events']['wait']=='wait_for_subscription_events' and expected['subscriptions']['ack'] is True
    assert expected['subscriptions']['delivery_policy']['tool']=='configure_subscription_delivery'
    with TestClient(create_app(s,['testserver'])) as client:
        mcp=structured(rpc(client,key,'capabilities',{'sections':['subscriptions']}))
        rest=client.post('/v1/capabilities',json={'sections':['subscriptions']},headers={'Authorization':'Bearer '+key}).json()
    full=s.capabilities(key,detail='full')
    assert masked(mcp)==masked(rest)=={k:masked(full[k]) for k in expected}==expected
