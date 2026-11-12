"""The caller's model and effort reach Android unchanged."""

import uuid

from starlette.testclient import TestClient

from test_android import setup
from test_android_agent import START, call


# spec: ANYM-993
def test_bare_model_effort_are_forwarded_and_invalid_effort_is_refused(tmp_path):
    app, calls = setup(tmp_path)
    with TestClient(app) as client:
        result = call(client, **START, request_id=str(uuid.uuid4()), model='gpt-6.1-terra', effort='high')
        assert not result.get('isError'), result
        assert calls[-1][2]['model'] == 'gpt-6.1-terra'
        assert calls[-1][2]['effort'] == 'high'
        count = len(calls)
        result = call(client, **START, request_id=str(uuid.uuid4()), effort='invalid')
        assert result.get('isError') and len(calls) == count
