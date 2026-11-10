"""The USB host outlives the gateway's five-second idle connection pool."""

import sys

from deedkit_android import cli


# spec: ANYM-953
def test_android_http_keepalive_exceeds_the_gateway_idle_expiry(monkeypatch):
    options = {}
    app = object()
    monkeypatch.setattr(sys, 'argv', ['deedkit-android', '--config', '/synthetic/config'])
    monkeypatch.setattr(cli, 'create_app', lambda path: app)
    monkeypatch.setattr(cli.uvicorn, 'run', lambda application, **kwargs: options.update(kwargs))
    cli.main()
    assert options['timeout_keep_alive'] == 30
    assert options['host'] == '127.0.0.1' and options['access_log'] is False
