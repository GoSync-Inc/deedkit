"""Omi contract tests: synthetic HTTP only, no wearable/provider sessions."""
import asyncio
from datetime import datetime, timezone
import hashlib
import importlib
import json
from pathlib import Path
import sys

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/recordings/src'))
SCOPES = ['recordings:read', 'recordings:content']
HEADERS = {'Authorization': 'Bearer backend-a'}


def private(path, value):
    path.write_text(value)
    path.chmod(0o600)
    return str(path)


def config(tmp_path):
    secret = private(tmp_path / 'cursor.key', 'c' * 64)
    token = private(tmp_path / 'omi.key', 'omi_dev_synthetic')
    value = {'version': 1, 'cursor_secret_file': secret,
             'accounts': [{'id': 'omi-a', 'provider': 'omi', 'label': 'Synthetic', 'token_file': token}],
             'principals': [{'id': 'agent-a', 'key_sha256': hashlib.sha256(b'backend-a').hexdigest(),
                             'accounts': ['omi-a'], 'permissions': SCOPES.copy(),
                             'expires_at': '2099-01-01T00:00:00Z'}]}
    path = tmp_path / 'config.json'
    private(path, json.dumps(value))
    return path, value


def conversation(text='Synthetic ' * 60, **extra):
    return {'id': 'conv-a', 'created_at': '2026-10-01T00:00:00Z', 'transcript_included': True,
            'structured': {'title': 'Synthetic title', 'overview': 'Synthetic overview'},
            'transcript_segments': [{'text': text, 'speaker': 'SPEAKER_00', 'start': 0, 'end': 60}], **extra}


def application(path, handler, **kwargs):
    module = importlib.import_module('deedkit_recordings.app')
    return module.create_app(path, transport=httpx.MockTransport(handler), **kwargs)


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test', headers=HEADERS)


# spec: ANYM-648
def test_omi_list_is_metadata_only_and_preserves_bound_provider_continuation(tmp_path):
    path, _ = config(tmp_path)
    seen = []
    def provider(request):
        seen.append(request)
        return httpx.Response(200, json=[conversation()], headers={
            'X-Omi-Has-More': 'true', 'X-Omi-Next-Cursor': 'provider-page-two',
            'X-Omi-Effective-Limit': '1', 'X-Omi-List-Truncated': 'false'})
    async def run():
        app = application(path, provider)
        async with client(app) as c:
            args = {'account': 'omi-a', 'limit': 1, 'start_date': '2026-10-01T00:00:00Z'}
            one = (await c.post('/v1/list_recordings', json=args)).json()
            assert one['items'][0]['title'] == 'Synthetic title'
            assert 'transcript' not in json.dumps(one) and 'Synthetic overview' not in json.dumps(one)
            assert one['pagination']['has_more'] is True and one['pagination']['complete'] is False
            cursor = one['next_cursor']
            assert (await c.post('/v1/list_recordings', json={**args, 'cursor': cursor})).status_code == 200
            assert (await c.post('/v1/list_recordings', json={**args, 'limit': 2, 'cursor': cursor})).status_code == 400
            assert (await c.post('/v1/list_recordings', json={**args, 'cursor': cursor[:-2]+'xx'})).status_code == 400
        assert len(seen) == 2
        assert seen[0].url.host == 'api.omi.me' and seen[0].url.scheme == 'https'
        assert seen[0].headers['Authorization'] == 'Bearer omi_dev_synthetic'
        assert seen[0].url.params['include_transcript'] == 'false'
        assert seen[1].url.params['cursor'] == 'provider-page-two'
    asyncio.run(run())


def row(identity, created_at=None):
    value = {'id': identity, 'structured': {'title': 'Synthetic ' + identity}}
    if created_at is not None:
        value['created_at'] = created_at
    return value


def pages(*bodies):
    """A synthetic Omi list that serves `bodies` in order, chained by provider cursors."""
    seen = []
    def provider(request):
        seen.append(dict(request.url.params))
        index = int(request.url.params.get('cursor', 'page-0')[5:])
        more = index + 1 < len(bodies)
        headers = {'X-Omi-Has-More': 'true' if more else 'false', 'X-Omi-List-Truncated': 'false',
                   'X-Omi-Effective-Limit': '25'}
        if more:
            headers['X-Omi-Next-Cursor'] = 'page-%d' % (index + 1)
        return httpx.Response(200, json=bodies[index], headers=headers)
    return provider, seen


async def listed(path, provider, **args):
    async with client(application(path, provider)) as c:
        return await c.post('/v1/list_recordings', json={'account': 'omi-a', **args})


# spec: ANYM-754
@pytest.mark.parametrize('arguments,sent', [
    ({'start_date': '2026-11-03'}, {'start_date': '2026-11-03T00:00:00Z'}),
    ({'end_date': '2026-11-03'}, {'end_date': '2026-11-03T23:59:59.999999Z'}),
    ({'start_date': '2026-11-03', 'end_date': '2026-11-03'},
     {'start_date': '2026-11-03T00:00:00Z', 'end_date': '2026-11-03T23:59:59.999999Z'}),
    ({'start_date': '2026-11-02', 'end_date': '2026-11-03T10:00:00+02:00'},
     {'start_date': '2026-11-02T00:00:00Z', 'end_date': '2026-11-03T08:00:00Z'}),
    ({'start_date': '2026-11-03T10:00:00Z'}, {'start_date': '2026-11-03T10:00:00Z'}),
])
def test_list_accepts_plain_dates_as_whole_utc_days(tmp_path, arguments, sent):
    path, _ = config(tmp_path)
    provider, seen = pages([])
    async def run():
        response = await listed(path, provider, **arguments)
        assert response.status_code == 200
        assert {k: v for k, v in seen[0].items() if k in ('start_date', 'end_date')} == sent
    asyncio.run(run())


# spec: ANYM-754
@pytest.mark.parametrize('arguments', [
    {'start_date': '2026-11-04', 'end_date': '2026-11-03'},
    {'start_date': '2026-11-03T12:00:00Z', 'end_date': '2026-11-03T11:59:59Z'},
    {'start_date': '2026-11-04', 'end_date': '2026-11-03T23:00:00Z'},
    {'start_date': '2026-11-03Z'}, {'start_date': '2026-11-03+00:00'}, {'end_date': '2026-11'},
    {'start_date': '2026-11-31'}, {'end_date': '20261103'}, {'start_date': '2026-11-03T10:00:00'},
])
def test_list_refuses_reversed_or_unplaceable_dates(tmp_path, arguments):
    path, _ = config(tmp_path)
    provider, seen = pages([])
    async def run():
        response = await listed(path, provider, **arguments)
        assert response.status_code == 422 and response.json() == {'error': 'invalid_parameters'}
        assert seen == []
    asyncio.run(run())


# spec: ANYM-754
def test_list_filter_honoured_by_provider_reports_nothing_dropped(tmp_path):
    path, _ = config(tmp_path)
    provider, seen = pages([row('in-a', '2026-11-03T09:00:00Z'), row('in-b', '2026-11-03T23:59:59.5+00:00')])
    async def run():
        result = (await listed(path, provider, start_date='2026-11-03', end_date='2026-11-03')).json()
        assert [i['id'] for i in result['items']] == ['in-a', 'in-b']
        assert result['date_filter'] == {'requested': True, 'filtered_locally': 0, 'pages_read': 1}
    asyncio.run(run())


# spec: ANYM-754
def test_list_drops_rows_outside_dates_when_provider_ignores_them(tmp_path):
    path, _ = config(tmp_path)
    provider, seen = pages([row('old', '2026-11-02T23:59:59Z'), row('in', '2026-11-03T00:00:00Z'),
                            row('new', '2026-11-04T00:00:00Z'), row('offset', '2026-11-04T01:00:00+02:00'),
                            row('missing'), row('garbled', 'not a date'), row('naive', '2026-10-01T00:00:00')])
    async def run():
        result = (await listed(path, provider, start_date='2026-11-03', end_date='2026-11-03')).json()
        assert [i['id'] for i in result['items']] == ['in', 'offset', 'missing', 'garbled', 'naive']
        assert result['date_filter'] == {'requested': True, 'filtered_locally': 2, 'pages_read': 1}
        assert result['next_cursor'] is None and result['pagination']['has_more'] is False
    asyncio.run(run())


# spec: ANYM-754
def test_list_follows_provider_pages_past_an_all_outside_page(tmp_path):
    path, _ = config(tmp_path)
    provider, seen = pages([row('old-a', '2026-10-01T00:00:00Z'), row('old-b', '2026-10-02T00:00:00Z')],
                           [row('in', '2026-11-03T12:00:00Z'), row('old-c', '2026-10-03T00:00:00Z')],
                           [row('later', '2026-11-03T13:00:00Z')])
    async def run():
        async with client(application(path, provider)) as c:
            args = {'account': 'omi-a', 'start_date': '2026-11-03'}
            result = (await c.post('/v1/list_recordings', json=args)).json()
            assert [i['id'] for i in result['items']] == ['in']
            assert result['date_filter'] == {'requested': True, 'filtered_locally': 3, 'pages_read': 2}
            assert result['pagination']['has_more'] is True and result['next_cursor']
            assert [p.get('cursor') for p in seen] == [None, 'page-1']
            assert all(p['start_date'] == '2026-11-03T00:00:00Z' for p in seen)
            more = (await c.post('/v1/list_recordings', json={**args, 'cursor': result['next_cursor']})).json()
            assert [i['id'] for i in more['items']] == ['later'] and more['next_cursor'] is None
            assert more['date_filter'] == {'requested': True, 'filtered_locally': 0, 'pages_read': 1}
            assert seen[-1]['cursor'] == 'page-2'
    asyncio.run(run())


# spec: ANYM-754
def test_list_stops_after_five_all_outside_pages_with_a_cursor(tmp_path):
    path, _ = config(tmp_path)
    provider, seen = pages(*([row('old-%d' % n, '2026-10-01T00:00:00Z')] for n in range(7)))
    async def run():
        result = (await listed(path, provider, start_date='2026-11-03')).json()
        assert result['items'] == [] and result['next_cursor']
        assert result['date_filter'] == {'requested': True, 'filtered_locally': 5, 'pages_read': 5}
        assert len(seen) == 5 and seen[-1]['cursor'] == 'page-4'
    asyncio.run(run())


# spec: ANYM-754
def test_list_without_dates_is_unchanged_and_reads_one_page(tmp_path):
    path, _ = config(tmp_path)
    provider, seen = pages([row('old', '2020-01-01T00:00:00Z')], [row('next', '2020-01-02T00:00:00Z')])
    async def run():
        result = (await listed(path, provider)).json()
        assert set(result) == {'account', 'provider', 'items', 'pagination', 'next_cursor'}
        assert [i['id'] for i in result['items']] == ['old'] and len(seen) == 1
        assert 'start_date' not in seen[0] and 'end_date' not in seen[0]
        empty = (await listed(path, pages([])[0])).json()
        assert empty['items'] == [] and 'date_filter' not in empty
    asyncio.run(run())


# spec: ANYM-754
def test_list_does_not_follow_pages_past_the_gateway_deadline(tmp_path):
    path, _ = config(tmp_path)
    provider, seen = pages([row('old', '2026-10-01T00:00:00Z')], [row('in', '2026-11-03T12:00:00Z')])
    async def run():
        async with client(application(path, provider, provider_timeout=31)) as c:
            result = (await c.post('/v1/list_recordings', json={'account': 'omi-a', 'start_date': '2026-11-03'})).json()
        assert result['items'] == [] and result['next_cursor'] and len(seen) == 1
        assert result['date_filter'] == {'requested': True, 'filtered_locally': 1, 'pages_read': 1}
    asyncio.run(run())


# spec: ANYM-754
def test_list_followed_page_waiting_on_the_ledger_past_the_deadline_returns_the_last_page(tmp_path, monkeypatch):
    path, _ = config(tmp_path)
    module = importlib.import_module('deedkit_recordings.app')
    monkeypatch.setattr(module, 'DATED_DEADLINE_S', 1.0)
    provider, seen = pages([row('old', '2026-10-01T00:00:00Z')], [row('in', '2026-11-03T12:00:00Z')])
    async def run():
        app = application(path, provider, provider_timeout=0.5)
        counted = []
        async def slow_ledger(token, detail, now=None):
            counted.append(detail)
            if len(counted) > 1:
                await asyncio.sleep(5)
        monkeypatch.setattr(app.state.archive, 'count', slow_ledger)
        async with client(app) as c:
            started = asyncio.get_running_loop().time()
            result = (await c.post('/v1/list_recordings', json={'account': 'omi-a', 'start_date': '2026-11-03'})).json()
            assert asyncio.get_running_loop().time() - started < 2
        assert result['items'] == [] and result['next_cursor'] and len(seen) == 1 and len(counted) == 2
        assert result['pagination']['has_more'] is True
        assert result['date_filter'] == {'requested': True, 'filtered_locally': 1, 'pages_read': 1}
        assert app.state.provider.active == 0
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('headers,complete', [({}, None), ({'X-Omi-Has-More':'oops'}, None),
    ({'X-Omi-Has-More':'false','X-Omi-List-Truncated':'true'}, False)])
def test_empty_or_malformed_provider_page_never_claims_complete(tmp_path, headers, complete):
    path, _ = config(tmp_path)
    async def run():
        async with client(application(path, lambda r: httpx.Response(200, json=[], headers=headers))) as c:
            result = (await c.post('/v1/list_recordings', json={'account':'omi-a'})).json()
            assert result['items'] == [] and result['pagination']['complete'] is complete
            assert result.get('next_cursor') is None
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('headers,complete', [
    ({'X-Omi-Has-More':'false','X-Omi-Effective-Limit':'25'}, True),
    ({'X-Omi-Has-More':'false','X-Omi-Effective-Limit':'25','X-Omi-List-Truncated':'maybe'}, None)])
def test_a_last_page_without_the_truncation_header_is_complete_and_a_malformed_one_is_not(tmp_path, headers, complete):
    path, _ = config(tmp_path)
    async def run():
        async with client(application(path, lambda r: httpx.Response(200, json=[], headers=headers))) as c:
            result = (await c.post('/v1/list_recordings', json={'account':'omi-a'})).json()
            assert result['items'] == [] and result['pagination']['complete'] is complete
            assert result['pagination']['list_truncated'] is (False if complete else None)
            assert result.get('next_cursor') is None
    asyncio.run(run())


# spec: ANYM-648
def test_content_continuation_rejects_changed_source_and_cross_credential_reuse(tmp_path):
    path, value = config(tmp_path)
    current = [conversation()]
    def provider(request):
        assert request.url.path == '/v1/dev/user/conversations/conv-a'
        return httpx.Response(200, json=current[0])
    async def run():
        app = application(path, provider)
        async with client(app) as c:
            args = {'account':'omi-a','recording_id':'conv-a','page_chars':100}
            first = (await c.post('/v1/read_recording', json=args)).json()
            assert first['content_state'] == 'available' and len(first['text']) == 100
            cursor = first['next_cursor']
            second = (await c.post('/v1/read_recording', json={**args,'cursor':cursor})).json()
            assert second['offset'] == 100 and second['source_digest'] == first['source_digest']
            value['principals'].append({**value['principals'][0], 'id':'agent-b',
                'key_sha256':hashlib.sha256(b'backend-b').hexdigest()})
            private(path,json.dumps(value))
            cross = await c.post('/v1/read_recording',json={**args,'cursor':cursor},
                                 headers={'Authorization':'Bearer backend-b'})
            assert cross.status_code == 400 and cross.json()['error'] == 'invalid_cursor'
            current[0] = conversation('changed transcript')
            changed = await c.post('/v1/read_recording',json={**args,'cursor':cursor})
            assert changed.status_code == 409 and changed.json()['error'] == 'source_changed'
    asyncio.run(run())


# spec: ANYM-648
def test_backend_scope_account_and_revocation_are_checked_before_egress(tmp_path):
    path, value = config(tmp_path)
    seen = []
    def provider(request):
        seen.append(request)
        return httpx.Response(200, json=conversation())
    async def run():
        async with client(application(path,provider)) as c:
            caps = (await c.post('/v1/capabilities',json={})).json()
            assert caps['accounts'] == [{'id':'omi-a','provider':'omi','label':'Synthetic'}]
            assert 'token_file' not in json.dumps(caps) and caps['audio'] == 'unsupported'
            args = {'account':'other-account','recording_id':'conv-a'}
            assert (await c.post('/v1/read_recording',json=args)).status_code == 403
            value['principals'][0]['permissions'] = ['recordings:read']
            private(path,json.dumps(value))
            assert (await c.post('/v1/read_recording',json={**args,'account':'omi-a'})).status_code == 403
            value['principals'] = []
            private(path,json.dumps(value))
            assert (await c.post('/v1/capabilities',json={})).status_code == 401
        assert seen == []
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('payload,kind,state', [
    ({'transcript_included':False},'transcript','unavailable'),
    ({'transcript_segments':[]},'transcript','empty'),
    ({'processing_deferred':True},'summary','pending'),
    ({'structured':{'title':'Synthetic','overview':''}},'summary','empty'),
])
def test_content_states_and_strict_arguments(tmp_path,payload,kind,state):
    path,_ = config(tmp_path)
    async def run():
        app=application(path,lambda r:httpx.Response(200,json=conversation(**payload)))
        async with client(app) as c:
            args={'account':'omi-a','recording_id':'conv-a','kind':kind}
            result=(await c.post('/v1/read_recording',json=args)).json()
            assert result['content_state'] == state and result.get('next_cursor') is None
            for invalid in ({**args,'recording_id':'../other'}, {**args,'url':'https://other.invalid'},
                            {**args,'page_chars':True}, {**args,'kind':'audio'}):
                rejected=await c.post('/v1/read_recording',json=invalid)
                assert rejected.status_code == 422 and 'https://other.invalid' not in rejected.text
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('status,body,code',[(302,b'private provider text','provider_unavailable'),
    (401,b'private provider text','provider_auth_required'),
    (200,b'not json private','invalid_provider_response'),
    (200,b'x'*1025,'response_too_large')])
def test_provider_failures_are_bounded_content_free_and_not_retried(tmp_path,status,body,code):
    path,_=config(tmp_path)
    seen=[]
    def provider(r):
        seen.append(r)
        return httpx.Response(status,content=body,headers={'Location':'http://127.0.0.1/private'})
    async def run():
        async with client(application(path,provider,provider_bytes=1024)) as c:
            response=await c.post('/v1/read_recording',json={'account':'omi-a','recording_id':'conv-a'})
            assert response.json()['error'] == code
            assert 'private' not in response.text and 'omi_dev_' not in response.text
        assert len(seen)==1
    asyncio.run(run())


SECRET = 'secret-provider-value'
LIST_HEADERS = {'X-Omi-Effective-Limit': '1'}


# spec: ANYM-753
@pytest.mark.parametrize('operation,response,detail', [
    ('read_recording', dict(json=[SECRET]), 'not_object'),
    ('read_recording', dict(json={'id': '../' + SECRET}), 'id_invalid'),
    ('read_recording', dict(json=conversation(id='conv-secret')), 'id_mismatch'),
    ('read_recording', dict(json=conversation(created_at=[SECRET])), 'metadata_field'),
    ('read_recording', dict(json=conversation(transcript_segments=[SECRET])), 'segment_shape'),
    ('read_recording', dict(json=conversation(transcript_segments=[{'text': [SECRET]}])), 'segment_text'),
    ('read_recording', dict(json=conversation(
        transcript_segments=[{'text': 'a', 'speaker': [SECRET]}])), 'segment_speaker'),
    ('read_recording', dict(json=conversation(
        transcript_segments=[{'text': 'a', 'start': SECRET}])), 'segment_offset'),
    ('list_recordings', dict(json={'items': SECRET}), 'list_shape'),
    ('list_recordings', dict(json=[conversation(), conversation()], headers=LIST_HEADERS),
     'list_pagination'),
    ('read_recording', dict(stream=httpx.ByteStream(SECRET.encode()), headers={'Content-Encoding': 'gzip'}), 'encoding'),
    ('read_recording', dict(content=b'not json ' + SECRET.encode()), 'not_json'),
    ('read_recording', dict(content=b'{"id":"conv-a","transcript_included":true,'
     b'"transcript_segments":[{"text":"\\ud800 secret-provider-value"}]}'), 'projection_error'),
])
def test_invalid_provider_response_names_the_rejected_part_without_provider_values(tmp_path, operation, response,
                                                                                    detail):
    path, _ = config(tmp_path)
    async def run():
        async with client(application(path, lambda r: httpx.Response(200, **response))) as c:
            args = {'account': 'omi-a', 'limit': 2} if operation == 'list_recordings' else {
                'account': 'omi-a', 'recording_id': 'conv-a'}
            result = await c.post('/v1/' + operation, json=args)
            assert result.status_code == 503
            assert result.json() == {'error': 'invalid_provider_response', 'detail_code': detail}
            assert SECRET not in result.text and 'conv-secret' not in result.text
    asyncio.run(run())


# spec: ANYM-753
def test_other_failures_carry_no_detail_code(tmp_path):
    path, _ = config(tmp_path)
    async def run():
        async with client(application(path, lambda r: httpx.Response(500, text=SECRET))) as c:
            result = await c.post('/v1/read_recording', json={'account': 'omi-a', 'recording_id': 'conv-a'})
            assert result.status_code == 503 and result.json() == {'error': 'provider_unavailable'}
    asyncio.run(run())


# spec: ANYM-648
def test_timeout_saturation_and_cancellation_release_owned_admission(tmp_path):
    path,_=config(tmp_path)
    async def run():
        entered=asyncio.Event()
        async def provider(r):
            entered.set()
            await asyncio.Event().wait()
        app=application(path,provider,provider_timeout=0.1)
        async with client(app) as c:
            args={'account':'omi-a'}
            tasks=[asyncio.create_task(c.post('/v1/list_recordings',json=args)) for _ in range(4)]
            await entered.wait()
            busy=await c.post('/v1/list_recordings',json=args)
            assert busy.status_code==503 and busy.json()['error']=='provider_busy'
            tasks[0].cancel()
            await asyncio.gather(tasks[0],return_exceptions=True)
            remaining=await asyncio.gather(*tasks[1:])
            assert all(r.json()['error']=='provider_timeout' for r in remaining)
            assert app.state.provider.active==0
    asyncio.run(run())


# spec: ANYM-648
@pytest.mark.parametrize('permissions',[[],['recordings:content']])
def test_content_alone_and_empty_permissions_grant_no_access(tmp_path,permissions):
    path,value=config(tmp_path)
    value['principals'][0]['permissions']=permissions
    private(path,json.dumps(value))
    seen=[]
    async def run():
        app=application(path,lambda r:seen.append(r))
        async with client(app) as c:
            for op,args in [('capabilities',{}),('list_recordings',{'account':'omi-a'}),
                            ('read_recording',{'account':'omi-a','recording_id':'conv-a'})]:
                assert (await c.post('/v1/'+op,json=args)).status_code==403
        assert not seen
    asyncio.run(run())


# spec: ANYM-648
def test_continuation_cannot_cross_accounts_or_provider_token_rotation(tmp_path):
    path,value=config(tmp_path)
    other=private(tmp_path/'second.key','omi_dev_second_synthetic')
    value['accounts'].append({'id':'omi-b','provider':'omi','label':'Other','token_file':other})
    value['principals'][0]['accounts'].append('omi-b')
    private(path,json.dumps(value))
    seen=[]
    def provider(r):
        seen.append(r)
        return httpx.Response(200,json=[conversation()],headers={'X-Omi-Has-More':'true',
            'X-Omi-Next-Cursor':'next','X-Omi-Effective-Limit':'1','X-Omi-List-Truncated':'false'})
    async def run():
        async with client(application(path,provider)) as c:
            args={'account':'omi-a','limit':1}
            token=(await c.post('/v1/list_recordings',json=args)).json()['next_cursor']
            assert (await c.post('/v1/list_recordings',json={**args,'account':'omi-b','cursor':token})).status_code==400
            private(Path(value['accounts'][0]['token_file']),'omi_dev_rotated_synthetic')
            assert (await c.post('/v1/list_recordings',json={**args,'cursor':token})).status_code==400
        assert len(seen)==1
    asyncio.run(run())


# spec: ANYM-648
def test_provider_stream_read_deadline_private_files_and_input_bounds(tmp_path):
    path,value=config(tmp_path)
    class Stalled(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'['
            await asyncio.Event().wait()
    async def run():
        app=application(path,lambda r:httpx.Response(200,stream=Stalled()),provider_timeout=0.02)
        async with client(app) as c:
            response=await c.post('/v1/list_recordings',json={'account':'omi-a'})
            assert response.json()['error']=='provider_timeout' and app.state.provider.active==0
            assert (await c.post('/v1/capabilities',content=b'x'*16385)).status_code==413
            assert (await c.post('/v1/capabilities',content=b'{"x":NaN}')).status_code==422
            value['principals'][0]['expires_at']='2000-01-01T00:00:00Z'
            private(path,json.dumps(value))
            assert (await c.post('/v1/capabilities',json={})).status_code==401
            path.chmod(0o644)
            assert (await c.post('/v1/capabilities',json={})).json()=={'error':'configuration_unavailable'}
    asyncio.run(run())


# spec: ANYM-648
def test_numeric_speaker_zero_is_distinct_from_missing_speaker():
    from deedkit_recordings.omi import content
    value = conversation(transcript_segments=[{'text':'first','speaker_id':0},{'text':'second'}])
    state, text = content(value,'transcript')
    assert state == 'available'
    assert [json.loads(line)['speaker'] for line in text.splitlines()] == ['0','unknown']


# spec: ANYM-648
def test_omi_speaker_precedence_uses_the_real_developer_fields():
    from deedkit_recordings.omi import content
    segments = [{'text': 'a', 'speaker_name': 'User', 'speaker_id': 0, 'start': 0.0, 'end': 1.0},
                {'text': 'b', 'speaker_name': None, 'speaker_id': 1, 'start': 1.0, 'end': 2.0},
                {'text': 'c', 'speaker_name': '', 'speaker_id': 2, 'start': 2.0, 'end': 3.0},
                {'text': 'd', 'speaker_name': None, 'speaker_id': None, 'start': 3.0, 'end': 4.0, 'original_text': 'D'}]
    state, text = content(conversation(transcript_segments=segments), 'transcript')
    rows = [json.loads(line) for line in text.splitlines()]
    assert state == 'available' and [row['speaker'] for row in rows] == ['User', '1', '2', 'unknown']
    assert [row['text'] for row in rows] == ['a', 'b', 'c', 'd']  # `text` is the corrected line; `original_text` unused


# spec: ANYM-648
def test_unplaced_omi_segments_have_null_offsets_and_other_negatives_are_kept():
    from deedkit_recordings.omi import content
    segments = [{'text': 'late', 'speaker_name': 'User', 'start': -1.0, 'end': -1.0},
                {'text': 'buffered', 'speaker_name': 'User', 'start': -2453.2, 'end': -2450.0},
                {'text': 'edge', 'speaker_name': 'User', 'start': -1.0, 'end': 0.5},
                {'text': 'live', 'speaker_name': 'User', 'start': 0.0, 'end': 2.0}]
    state, text = content(conversation(transcript_segments=segments), 'transcript')
    rows = [json.loads(line) for line in text.splitlines()]
    assert state == 'available'
    assert rows[0] == {'start': None, 'end': None, 'speaker': 'User', 'text': 'late', 'placed': False}
    assert [(row['start'], row['end'], 'placed' in row) for row in rows[1:]] == [
        (-2453.2, -2450.0, False), (-1.0, 0.5, False), (0.0, 2.0, False)]


# spec: ANYM-648
@pytest.mark.parametrize('structured,age,state', [
    ({'title': '', 'overview': ''}, 3600, 'pending'),
    ({'overview': ''}, 3600, 'pending'),
    ({'title': '', 'overview': ''}, 2 * 86400, 'empty'),
    ({'title': 'Synthetic', 'overview': ''}, 3600, 'empty'),
    ({'title': '', 'overview': 'Synthetic overview'}, 3600, 'available'),
])
def test_an_empty_omi_summary_is_pending_for_its_first_day_then_empty(structured, age, state):
    from deedkit_recordings.omi import content
    now = 1_800_000_000
    created = datetime.fromtimestamp(now - age, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    assert content(conversation(structured=structured, created_at=created), 'summary', now)[0] == state
    assert content(conversation(structured=structured, created_at='not a time'), 'summary', now)[0] == (
        'available' if state == 'available' else 'empty')


# spec: ANYM-648
def test_negative_provider_offsets_are_kept_and_nonfinite_or_huge_offsets_are_rejected():
    from deedkit_recordings.omi import Failure, content
    segment = {'text': 'early', 'speaker_name': 'Synthetic', 'speaker_id': 1, 'start': -3.81, 'end': -1.29}
    state, text = content(conversation(transcript_segments=[segment]), 'transcript')
    assert state == 'available'
    assert [(row['start'], row['end']) for row in map(json.loads, text.splitlines())] == [(-3.81, -1.29)]
    for bad in (float('nan'), float('inf'), float('-inf'), -1e11, 1e11, True, '1'):
        with pytest.raises(Failure):
            content(conversation(transcript_segments=[{**segment, 'start': bad}]), 'transcript')
