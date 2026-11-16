"""A refused phone session names its holder, and for the caller's own key the lease's end (ANYM-1001)."""
import json
import time
import uuid

from test_device_service import execute, post, service, session  # noqa: F401 - `service` is the shared fixture

ACQUIRE = {'device_id': 'home', 'action': 'acquire'}


def refusal(response):
    assert response.status_code == 409, response.text
    return response.json()


def no_foreign_lease(body, *tokens):
    text = json.dumps(body)
    assert not any(token in text for token in tokens) and '"owner"' not in text and 'expires' not in text
    assert '"one"' not in text and '"two"' not in text


# spec: ANYM-1001
def test_device_reserved_says_own_with_its_end_or_other_with_nothing_more(service):  # noqa: F811
    before = time.time()
    token = session(service)
    # A second agent under the same key: its own key holds the phone, until the lease's end.
    own = refusal(post(service, 'session', ACQUIRE))
    assert own['error'] == 'device_reserved' and own['holder'] == 'own'
    assert before + 30 <= own['expires_at'] <= time.time() + 900 and own['next_action']
    assert token not in json.dumps(own)
    other = refusal(post(service, 'session', ACQUIRE, key='two'))
    assert other == {'error': 'device_reserved', 'holder': 'other', 'next_action': other['next_action']}
    no_foreign_lease(other, token)


# spec: ANYM-1001
def test_a_wrong_session_id_says_whether_the_phone_is_free_own_or_another_keys(service):  # noqa: F811
    lost = str(uuid.uuid4())

    def renew(key='one', token=lost):
        return refusal(post(service, 'session', {'device_id': 'home', 'action': 'renew', 'session_id': token}, key=key))

    free = renew()
    assert free['error'] == 'reservation_expired_or_forbidden' and free['holder'] == 'free' and free['next_action']
    token = session(service)
    own = renew()
    assert own['error'] == 'reservation_expired_or_forbidden' and own['holder'] == 'own'
    assert own['expires_at'] > time.time() and token not in json.dumps(own) and lost not in json.dumps(own)
    released = post(service, 'session', {'device_id': 'home', 'action': 'release', 'session_id': lost})
    assert refusal(released)['holder'] == 'own'
    observed = refusal(post(service, 'observe', {'device_id': 'home', 'session_id': lost}))
    assert observed['error'] == 'reservation_expired_or_forbidden' and observed['holder'] == 'own'
    other = renew(key='two', token=token)
    assert other['holder'] == 'other'
    no_foreign_lease(other, token, lost)


# spec: ANYM-1001
def test_a_lapsed_lease_reads_free_and_a_new_acquire_takes_it(service, monkeypatch):  # noqa: F811
    token = session(service)
    real = time.time
    monkeypatch.setattr('deedkit_android.store.time.time', lambda: real() + 10000)
    lapsed = refusal(post(service, 'session', {'device_id': 'home', 'action': 'renew', 'session_id': token}))
    assert lapsed['holder'] == 'free'
    assert post(service, 'session', ACQUIRE, key='two').status_code == 200


# spec: ANYM-1001
def test_device_busy_while_an_operation_runs_names_its_holder(service):  # noqa: F811
    token, request_id = session(service), str(uuid.uuid4())
    assert execute(service, token, request_id, ['shell', 'slow'], wait_s=0, timeout_s=2).json()['state'] == 'running'
    own = refusal(post(service, 'session', ACQUIRE))
    assert own['error'] == 'device_busy' and own['holder'] == 'own' and own['expires_at'] > time.time()
    assert token not in json.dumps(own)
    other = refusal(post(service, 'session', ACQUIRE, key='two'))
    assert other['error'] == 'device_busy' and other['holder'] == 'other'
    no_foreign_lease(other, token)
    post(service, 'get_operation', {'request_id': request_id, 'wait_s': 4})
