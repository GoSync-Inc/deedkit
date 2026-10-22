"""Real ASGI error responses with synthetic stores; no database or provider calls."""
import pytest
from starlette.background import BackgroundTask
from starlette.responses import Response
from starlette.routing import Route
from starlette.testclient import TestClient

from messaging_service import app as gateway


PRIVATE = 'synthetic-private-token phone=+123456 chat=private diagnostic'
GRANT = 'synthetic-private-download-grant'


class RecordedASGI:
    """Retain wire frames so a second response cannot hide behind TestClient."""
    def __init__(self, inner):
        self.inner, self.frames = inner, []

    async def __call__(self, scope, receive, send):
        async def record(message):
            if scope['type'] == 'http':
                self.frames.append(message.copy())
            await send(message)
        await self.inner(scope, receive, record)


@pytest.mark.parametrize('channel', ['rest', 'mcp', 'download', 'download_stat', 'authentication'])
def test_unexpected_http_failures_have_one_safe_response_and_one_capture(channel, tmp_path, monkeypatch, caplog):
    failure = RuntimeError(PRIVATE)
    captures, audits, calls = [], [], []

    class Store:
        public_url = 'http://testserver'

        def authenticate(self, token):
            if channel == 'authentication':
                raise failure
            return 'synthetic-principal'

        def audit_boundary(self, code):
            audits.append(code)

        def capabilities(self, token, detail='full', sections=None):
            calls.append('capabilities')
            raise failure

        def download(self, grant):
            assert grant == GRANT
            calls.append('download')
            if channel == 'download_stat':
                # Exercise the real FileResponse stat failure before headers.
                # Store.download also returns the admitted binding (unused here).
                return {'filename': PRIVATE, 'digest': 'a' * 64}, tmp_path / 'missing-original', ()
            raise failure

    monkeypatch.setattr(gateway, 'capture_error', lambda error, code: captures.append((error, code)))
    wire = RecordedASGI(gateway.create_app(Store(), ['testserver']))
    with TestClient(wire) as client:
        if channel in ('download', 'download_stat'):
            response = client.get('/v1/download/' + GRANT)
        elif channel == 'mcp':
            response = client.post('/mcp', json={'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                'params': {'name': 'capabilities', 'arguments': {}}}, headers={
                    'Authorization': 'Bearer synthetic-token',
                    'Accept': 'application/json, text/event-stream'})
        else:
            response = client.post('/v1/capabilities', json={}, headers={'Authorization': 'Bearer synthetic-token'})

    starts = [frame for frame in wire.frames if frame['type'] == 'http.response.start']
    assert len(starts) == 1
    assert len(captures) == 1
    assert captures[0][1] == ('tool_failed' if channel == 'mcp' else 'request_failed')
    if channel != 'download_stat':
        assert captures[0][0] is failure
    if channel == 'mcp':
        assert response.status_code == 200 and response.json()['result']['isError'] is True
        assert audits == []  # Tools return a handled, safe MCP error, not an HTTP denial.
    else:
        assert response.status_code == 503
        detail = response.json()
        assert detail['error'] == 'temporarily_unavailable'
        assert detail['retryable'] is True and detail['retry_after_seconds'] == 2
        assert response.headers['x-request-id'] == detail['request_id']
        assert audits == ['temporarily_unavailable']
    assert PRIVATE not in response.text and GRANT not in response.text
    assert str(tmp_path) not in response.text
    assert PRIVATE not in caplog.text and GRANT not in caplog.text and str(tmp_path) not in caplog.text
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-content-type-options'] == 'nosniff'
    assert calls == ([] if channel == 'authentication' else ['download'] if channel.startswith('download') else ['capabilities'])


def test_failure_after_completed_response_is_captured_and_audited_once_without_a_second_response(monkeypatch, caplog):
    failure = RuntimeError(PRIVATE)
    captures, audits = [], []

    class Store:
        public_url = 'http://testserver'
        def authenticate(self, token): return 'synthetic-principal'
        def audit_boundary(self, code): audits.append(code)

    async def failed_background():
        raise failure

    async def endpoint(request):
        return Response('synthetic complete response', background=BackgroundTask(failed_background))

    monkeypatch.setattr(gateway, 'capture_error', lambda error, code: captures.append((error, code)))
    app = gateway.create_app(Store(), ['testserver'])
    app.app.routes.append(Route('/synthetic-completed-response', endpoint, methods=['GET']))
    wire = RecordedASGI(app)
    with TestClient(wire) as client:
        response = client.get('/synthetic-completed-response', headers={'Authorization': 'Bearer synthetic-token'})

    assert response.status_code == 200 and response.text == 'synthetic complete response'
    assert [frame['status'] for frame in wire.frames if frame['type'] == 'http.response.start'] == [200]
    assert [frame['body'] for frame in wire.frames if frame['type'] == 'http.response.body'] == [b'synthetic complete response']
    assert captures == [(failure, 'request_failed')]
    assert audits == ['temporarily_unavailable']
    assert PRIVATE not in caplog.text
