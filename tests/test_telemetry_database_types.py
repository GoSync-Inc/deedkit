"""Database failure types stay useful without sending database content."""
import json
import threading

from psycopg import errors
import pytest
from tiden_telemetry import Client

from messaging_service import telemetry


PRIVATE = 'private-query SELECT password FROM customer WHERE token=private-parameter'
DATABASE_TYPES = (
    errors.QueryCanceled, errors.LockNotAvailable, errors.InsufficientPrivilege,
    errors.UndefinedTable, errors.UndefinedColumn, errors.DeadlockDetected,
    errors.SerializationFailure, errors.ReadOnlySqlTransaction,
)


class Transport:
    def __init__(self):
        self.envelopes = []
        self.ready = threading.Event()

    def send(self, envelope):
        self.envelopes.append(envelope)
        self.ready.set()


# spec: ANYM-173
@pytest.mark.parametrize('kind', DATABASE_TYPES, ids=lambda kind: kind.__name__)
def test_database_type_survives_sdk_sanitization_without_database_content(kind):
    try:
        exec(compile('raise kind(private)', str(telemetry.ROOT / 'store.py'), 'exec'),
             {'kind': kind, 'private': PRIVATE})
    except kind as caught:
        error = caught
    transport = Transport()
    client = Client('https://synthetic-key@ingest.example.test/synthetic-project',
                    before_send=telemetry.before_send, transport=transport)
    worker = telemetry.Reporter(client, 'subscriptions', 'local-synthetic')
    try:
        assert worker.capture(error, 'request_failed')
        assert transport.ready.wait(1)
    finally:
        worker.close()
    assert len(transport.envelopes) == 1
    raw = transport.envelopes[0].decode()
    event = json.loads(raw.splitlines()[2])
    value = event['exception']['values'][0]
    assert value == {'type': kind.__name__, 'value': 'request_failed', 'stacktrace': {
        'frames': [{'filename': 'services/messaging/messaging_service/store.py',
                    'lineno': 1, 'in_app': True}]}}
    assert event['tags'] == {'component': 'subscriptions', 'error_code': 'request_failed'}
    for canary in (PRIVATE, 'private-query', 'private-parameter', 'synthetic-key'):
        assert canary not in raw
    assert 'diag' not in raw and 'sqlstate' not in raw and 'request' not in event


# spec: ANYM-173
def test_unknown_database_subclass_stays_unknown_without_inspecting_error():
    class PrivateDatabaseFailure(errors.OperationalError):
        def __str__(self):
            raise AssertionError('exception text must not be read')

        @property
        def diag(self):
            raise AssertionError('database diagnostics must not be read')

    event = telemetry.safe_event(PrivateDatabaseFailure(PRIVATE), 'subscriptions', 'request_failed')
    assert event['exception']['values'][0]['type'] == 'UnexpectedError'
    event.update(event_id='a' * 32, timestamp=1, environment='local-synthetic')
    event['exception']['values'][0]['type'] = 'PrivateDatabaseFailure'
    event['exception']['values'][0]['value'] = PRIVATE
    clean = telemetry.before_send(event)
    assert clean['exception']['values'][0]['type'] == 'UnexpectedError'
    assert PRIVATE not in json.dumps(clean)
