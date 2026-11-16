"""An agent `start` that omits a required field names that field, never a bare `$ format` (DK-16, ANYM-984)."""
import json
import uuid

from test_device_service import post, service  # noqa: F401 - `service` is the shared fixture

START = {'action': 'start', 'device_id': 'home', 'session_id': 'held-lease-placeholder',
         'goal': 'Open the synthetic settings screen', 'owner_instruction': 'Synthetic owner words only',
         'params': {'QUERY': 'synthetic-param-value'}}
REQUIRED = ('device_id', 'session_id', 'goal', 'owner_instruction')


def start(**changes):
    body = {**START, 'request_id': str(uuid.uuid4()), **changes}
    return {key: value for key, value in body.items() if value is not None}


def refused(service, body):  # noqa: F811
    response = post(service, 'agent', body)
    assert response.status_code == 422, response.text
    answer = response.json()
    assert answer['error'] == 'invalid_arguments' and answer['next_action'] == 'check_parameters'
    text = json.dumps(answer)
    assert not any(value in text for value in (
        START['goal'], START['owner_instruction'], START['session_id'], 'synthetic-param-value'))
    return answer['field_errors']


# spec: ANYM-984
def test_an_agent_start_without_its_session_names_session_id_as_required(service):  # noqa: F811
    assert refused(service, start(session_id=None)) == [{'field': 'session_id', 'reason': 'required'}]


# spec: ANYM-984
def test_an_agent_start_names_every_omitted_required_field_and_nothing_else(service):  # noqa: F811
    for name in REQUIRED:
        assert refused(service, start(**{name: None})) == [{'field': name, 'reason': 'required'}]
    bare = {'action': 'start', 'request_id': str(uuid.uuid4())}
    assert refused(service, bare) == [{'field': name, 'reason': 'required'} for name in sorted(REQUIRED)]


# spec: ANYM-984
def test_other_start_refusals_and_unknown_actions_stay_refused(service):  # noqa: F811
    assert refused(service, start(wait_s=3)) == [{'field': '$', 'reason': 'format'}]
    assert refused(service, start(text='a dialogue field')) == [{'field': '$', 'reason': 'format'}]
    unknown = refused(service, start(surprise=1))
    assert {'field': 'surprise', 'reason': 'unknown_field'} in unknown
    assert refused(service, start(action='launch'))[0]['field'] == 'action'
    assert refused(service, start(request_id=None)) == [{'field': 'request_id', 'reason': 'required'}]
