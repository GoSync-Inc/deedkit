"""Starlette's TestClient reports the peer 'testclient', which VoiceBoundary refuses as not an IP address."""
import pytest
from starlette import testclient


@pytest.fixture(autouse=True)
def loopback_test_client(monkeypatch):
    original = testclient.TestClient.__init__

    def init(self, *args, client=('127.0.0.1', 50000), **kwargs):
        original(self, *args, client=client, **kwargs)
    monkeypatch.setattr(testclient.TestClient, '__init__', init)
