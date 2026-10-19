"""Archive retrieval across scopes, query modes and bidirectional history."""
import json

import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.store import APIError


def ids(result):
    return [row['id'] for row in result['items']]


def test_multi_filters_intersect_scope_and_legacy_filters(seeded):
    s,a,b=seeded
    chats=['telegram-demo-chat','whatsapp-demo-1-chat']  # ANYM-253: a named chat outside the scope is not_found
    assert ids(s.search(a,chats=chats,senders=['demo-person-1','demo-person-3']))==['demo-tg-1','demo-wa-1']
    assert ids(s.search(b,chats=['telegram-demo-chat'],accounts=['whatsapp-demo-1','telegram-demo']))==['demo-tg-1','demo-tg-2']
    assert ids(s.search(a,chats=chats,chat='telegram-demo-chat',sender='demo-person-1'))==['demo-tg-1']
    for field in ['accounts','chats','senders']:
        assert ids(s.search(a,**{field:[]}))==[]
    s.set_access('agent-a',chats=['telegram-demo-chat'])
    assert ids(s.search(a,chats=['telegram-demo-chat']))==['demo-tg-1','demo-tg-2']
    with pytest.raises(APIError,match='not_found'):s.search(a,chats=chats)
    assert s.get_updates(a,'search-only')['ack_version']==0


def test_phrase_boolean_and_direction(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        c.execute("UPDATE messages SET direction='outgoing' WHERE id='demo-tg-2'")
    assert ids(s.search(a,'budget review',query_mode='phrase'))==['demo-wa-1']
    assert ids(s.search(a,'review budget',query_mode='phrase'))==[]
    assert ids(s.search(a,'review budget'))==['demo-wa-1']
    assert ids(s.search(a,'Маяк OR Atlas',query_mode='web'))==['demo-tg-1','demo-wa-1']
    assert ids(s.search(a,'Маяк -встреча',query_mode='web'))==[]
    assert ids(s.search(a,direction='outgoing'))==['demo-tg-2']
    assert ids(s.search(a,query_mode='web',query='"unmatched " OR'))==[]


def test_descending_cursor_and_exclusive_history_boundaries(seeded):
    s,a,b=seeded
    first=s.search(a,order='desc',limit=1)
    assert ids(first)==['demo-wa-1']
    second=s.search(a,order='desc',after=first['next_cursor'],limit=1)
    assert ids(second)==['demo-tg-2']
    third=s.search(a,order='desc',after=second['next_cursor'],limit=1)
    assert ids(third)==['demo-tg-1'] and third['next_cursor'] is None
    assert ids(s.get_messages(b,'telegram-demo-chat',order='desc',before='demo-tg-2'))==['demo-tg-1']
    assert ids(s.get_messages(b,'telegram-demo-chat',from_time='2026-10-02T00:00:00Z'))==['demo-tg-2']
    assert ids(s.search(a,before='demo-tg-2'))==['demo-tg-1']
    for kwargs in [{'after':'demo-private-1'},{'before':'demo-private-1'}]:
        with pytest.raises(APIError,match='not_found'):
            s.search(a,order='desc',**kwargs)


def test_context_contains_anchor_and_expands_without_duplicates_at_equal_timestamps(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        for i in range(100):
            c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
                VALUES(%s,'telegram-demo','telegram-demo-chat',%s,'same-person','simultaneous',
                '2026-10-02T12:00:00Z')""",(f'same-{i:03}',f'same-{i:03}'))
    context=s.get_context(b,'same-050',limit=10)
    assert len(context['context'])==10
    assert any(m['id']=='same-050' for m in context['context'])
    window=context['context_window']
    older=s.get_messages(b,'telegram-demo-chat',before=window['older_cursor'],order='desc')
    newer=s.get_messages(b,'telegram-demo-chat',after=window['newer_cursor'])
    chosen={m['id'] for m in context['context']}
    assert not chosen.intersection(ids(older)+ids(newer))
    assert len(chosen | set(ids(older)) | set(ids(newer)))==len(chosen)+len(ids(older))+len(ids(newer))
    with s.pool.connection() as c:c.execute("UPDATE messages SET deleted=true WHERE id='same-050'")
    deleted=s.get_context(b,'same-050')
    assert deleted['message']['deleted']
    assert all(m['id']!='same-050' for m in deleted['context'])


def test_context_window_does_not_skip_older_timestamp_ties(seeded):
    s,a,b=seeded
    with s.pool.connection() as c:
        for i in range(20):
            c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
                VALUES(%s,'telegram-demo','telegram-demo-chat',%s,'person','older tie',
                '2026-10-02T11:59:59Z')""",(f'older-{i:02}',f'older-{i:02}'))
        c.execute("""INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at)
            VALUES('closer','telegram-demo','telegram-demo-chat','closer','person','newer',
            '2026-10-02T12:00:00.5Z')""")
    context=s.get_context(b,'demo-tg-2',limit=10)
    assert [m['id'] for m in context['context']]==[f'older-{i:02}' for i in range(12,20)]+['demo-tg-2','closer']
    older=s.get_messages(b,'telegram-demo-chat',before=context['context_window']['older_cursor'],order='desc')
    assert ids(older)[:12]==[f'older-{i:02}' for i in range(11,-1,-1)]


@pytest.mark.parametrize('params',[
    {'accounts':'telegram-demo'}, {'chats':[True]}, {'senders':[1]},
    {'chats':['a']*101}, {'senders':['']}, {'accounts':['x\x00y']},
    {'order':'desc; DROP TABLE messages'}, {'direction':'any'},
    {'query_mode':{}}, {'query':'bad\x00query'}, {'before':True},
    {'after':'demo-tg-1','before':'demo-tg-2'},
])
def test_invalid_search_parameters_have_safe_errors(seeded,params):
    s,a,b=seeded
    with pytest.raises(APIError):s.search(a,**params)
    audit=s.audit_tail(principal='agent-a',limit=1)[0]
    assert audit['outcome']=='denied' and audit['resource_ids']==[]


def test_rest_and_mcp_share_extended_filters_and_keep_scope(seeded):
    s,a,b=seeded
    kwargs={'view':'archive','chats':['telegram-demo-chat'],
            'senders':['demo-person-1'],'query':'Маяк OR Atlas','query_mode':'web','order':'desc'}
    with TestClient(create_app(s,['testserver'])) as client:
        headers={'Authorization':'Bearer '+b,'Accept':'application/json, text/event-stream'}
        rest=client.post('/v1/search',headers=headers,json=kwargs)
        assert rest.status_code==200 and ids(rest.json())==['demo-tg-1']
        rpc=client.post('/mcp',headers=headers,json={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'search','arguments':kwargs}}).json()['result']
        assert rpc['structuredContent']==rest.json() and not rpc.get('isError')
        assert b not in json.dumps(rpc)
        s.set_access('agent-b',permissions=[])
        assert client.post('/v1/search',headers=headers,json=kwargs).status_code==403
