"""Synthetic HTTP tests: no provider sessions, database or network credentials."""
import json
import math

import httpx
import pytest

from anymsg import AnyMsgError, Client


def test_send_helpers_do_not_retry_after_transport_failure():
    calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('synthetic private diagnostic')
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnyMsgError,match='request_failed'):
            client.send_message(account='fixture',chat_id='fixture-chat',text='Approved synthetic',
                request_id='synthetic-request-0001')
    assert len(calls)==1 and str(calls[0].url).endswith('/v1/send_message')


@pytest.mark.parametrize('url',[
    'http://public.example', 'https://user:pass@example.test',
    'https://example.test?q=token', 'https://example.test/#fragment',
    'https://example.test:99999', 'ftp://127.0.0.1', 'https://example.test/\npath',
])
def test_rejects_unsafe_bearer_destinations(url):
    with pytest.raises(ValueError):Client(url,'synthetic-token')


@pytest.mark.parametrize('url',['http://127.0.0.1:8080','http://localhost:8080','http://[::1]:8080','https://archive.example/prefix'])
def test_https_or_loopback_and_prefix_routes(url):
    seen=[]
    def handler(request):
        seen.append(request)
        assert request.headers['Authorization']=='Bearer synthetic-token'
        assert request.headers['Accept-Encoding']=='identity'
        assert 'synthetic-token' not in str(request.url) and b'synthetic-token' not in request.content
        return httpx.Response(200,json={'items':[]})
    with Client(url,'synthetic-token',transport=httpx.MockTransport(handler)) as client:
        assert client.search('invoice',account='fixture',limit=4)=={'items':[]}
    assert str(seen[0].url)==url+'/v1/search'
    assert json.loads(seen[0].content)=={'query':'invoice','account':'fixture','limit':4}


def test_redirect_is_not_followed_and_token_never_reaches_location():
    seen=[]
    def handler(request):
        seen.append(request)
        return httpx.Response(307,headers={'Location':'https://other.example/collect-token'})
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnyMsgError,match='redirect_rejected') as error:client.capabilities()
    assert error.value.status==307 and len(seen)==1


def test_remote_errors_expose_only_known_code_and_request_id():
    responses=iter([
        httpx.Response(401,json={'error':'unauthorized','request_id':'a'*32,'details':'synthetic-secret'}),
        httpx.Response(503,json={'error':'synthetic_secret','request_id':'synthetic_secret','message':'synthetic-secret'}),
    ])
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(lambda _:next(responses))) as client:
        with pytest.raises(AnyMsgError) as first:client.capabilities()
        assert first.value.code=='unauthorized' and first.value.status==401 and first.value.request_id=='a'*32
        with pytest.raises(AnyMsgError) as second:client.capabilities()
        assert second.value.code=='http_error' and second.value.request_id is None
        assert 'synthetic' not in str(second.value) and 'secret' not in repr(first.value)


@pytest.mark.parametrize('operation',['ack_updates','record_review'])
def test_ambiguous_state_mutations_are_not_retried(operation):
    seen=[]
    def handler(request):
        seen.append(request);raise httpx.ReadTimeout('synthetic-private-body',request=request)
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnyMsgError,match='request_failed') as error:client.call(operation,{'consumer':'fixture'})
    assert len(seen)==1 and 'private' not in str(error.value)


def test_rate_limit_is_returned_once_without_implicit_retry():
    seen=[]
    def handler(request):seen.append(request);return httpx.Response(429,json={'error':'rate_limited'})
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnyMsgError,match='rate_limited'):client.capabilities()
    assert len(seen)==1


def test_request_bounds_and_injection_reject_before_network():
    def forbidden(_):raise AssertionError('invalid request must not leave client')
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(forbidden)) as client:
        for name in ('../search','search?token=x','/search',''):
            with pytest.raises(ValueError,match='invalid_operation'):client.call(name)
        for params in ({'token':'other'},{'value':math.nan},{'value':object()},[1],{1:'x'}):
            with pytest.raises(ValueError,match='invalid_parameters'):client.call('search',params)
        with pytest.raises(ValueError,match='request_too_large'):client.search('x'*20000)


@pytest.mark.parametrize('body',[b'{"bad":NaN}',b'[]',b'not json',b'\xff'])
def test_invalid_json_is_bounded_safe_error(body):
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(lambda _:httpx.Response(200,content=body))) as client:
        with pytest.raises(AnyMsgError,match='invalid_response'):client.capabilities()


def test_oversized_stream_is_stopped_and_closed():
    class Stream(httpx.SyncByteStream):
        chunks=0;closed=False
        def __iter__(self):
            for _ in range(100):self.chunks+=1;yield b'x'*65536
        def close(self):self.closed=True
    stream=Stream()
    with Client('https://archive.example','synthetic-token',max_response_bytes=1024,
            transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=stream))) as client:
        with pytest.raises(AnyMsgError,match='response_too_large'):client.capabilities()
    assert stream.chunks==1 and stream.closed


def test_compression_is_not_decoded_before_bounds_check():
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(
            lambda _:httpx.Response(200,headers={'Content-Encoding':'gzip'},stream=httpx.ByteStream(b'not-a-gzip-stream')))) as client:
        with pytest.raises(AnyMsgError,match='unsupported_content_encoding'):client.capabilities()


@pytest.mark.parametrize('token',['','has space','has\nnewline','unicode-ключ',False])
def test_bearer_headers_are_not_injectable(token):
    with pytest.raises(ValueError,match='invalid_bearer_token'):Client('https://archive.example',token)


def test_local_reply_dismissal_preserves_target_and_safe_conflict():
    import httpx
    seen=[]
    def handler(request):
        seen.append((request.url.path,json.loads(request.content)))
        return httpx.Response(409,json={'error':'reply_target_changed','detail':'synthetic-sensitive'})
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AnyMsgError,match='reply_target_changed'):
            client.dismiss_reply(chat_id='chat_synthetic',message_id='msg_synthetic',expected_revision=3)
    assert seen==[('/v1/dismiss_reply',{'chat_id':'chat_synthetic','message_id':'msg_synthetic','expected_revision':3})]


# spec: ANYM-270
def test_capabilities_sends_detail_and_sections_only_when_given():
    bodies=[]
    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200,json={'service':'anymsg'})
    with Client('https://archive.example','synthetic-token',transport=httpx.MockTransport(handler)) as client:
        client.capabilities()
        client.capabilities(detail='summary')
        client.capabilities(sections=('search_options','sending'))
    assert bodies==[{},{'detail':'summary'},{'sections':['search_options','sending']}]
