"""A successful local-agent run becomes a saved scenario (ANYM-973): the compiler, the draft, its guarded test, the repairs and
the publish gate. A fake engine and a fake phone; no model, no phone, no live provider."""

import json
import uuid

import httpx
import pytest
import yaml

from agent_harness import PACKAGE, Service, fake_engine, pass_probe, write_config
from deedkit_android.agent_save import SaveError, compile_log, resolve_point

SCREEN = ('<hierarchy><node text="" resource-id="" package="p" bounds="[0,0][1000,2000]">'
          '<node text="Add to cart" resource-id="p:id/add" package="p" bounds="[100,300][300,380]" clickable="true"/>'
          '<node text="Pizza" resource-id="p:id/row" package="p" bounds="[0,500][1000,600]"/>'
          '<node text="Pizza" resource-id="p:id/row2" package="p" bounds="[0,700][1000,800]"/>'
          '<node text="Plain" resource-id="" package="p" bounds="[0,900][1000,1000]"/>'
          '<node text="Plain" resource-id="" package="p" bounds="[0,1100][1000,1200]"/></node></hierarchy>')


def log(*steps):
    return [{'n': number, 'verdict': 'allowed', 'tool': tool, 'arguments': arguments, **extra}
            for number, (tool, arguments, extra) in enumerate(steps, 1)]


def compiled(actions, params=None, names=(), outputs=(), data=None):
    return yaml.safe_dump(compile_log(actions, params or {}, names, outputs, data or {}), sort_keys=False)


# spec: ANYM-973
def test_a_fixed_action_log_compiles_to_fixed_yaml_with_element_waits_parameters_and_keyed_reads():
    actions = log(('open_app', {'package': PACKAGE}, {}), ('screen', {}, {}),
                  ('tap', {'selector': {'text': 'Search'}}, {}), ('type', {'text': 'Latte please'}, {}),
                  ('type', {'text': '${secret:CARD}'}, {}), ('key', {'key': 'Enter'}, {}),
                  ('swipe', {'direction': 'up', 'duration_ms': 300}, {}),
                  ('wait', {'visible': {'text': 'Cart'}, 'timeout_ms': 4000}, {}),
                  ('tap', {'point': '50%,10%'}, {'resolved': {'resourceId': 'p:id/add'}}),
                  ('read_text', {'selector': {'id': 'total'}}, {'read': 'Total 12.50'}),
                  ('read_text', {'selector': {'id': 'note'}}, {'read': 'ignored'}))
    actions.insert(3, {'n': 99, 'verdict': 'refused', 'tool': 'tap', 'arguments': {'selector': {'text': 'Nope'}}})
    assert compiled(actions, {'ITEM': 'Latte'}, ['ITEM'], ['total'], {'total': '12.50'}) == """\
- launchApp:
    appId: example.food
    stopApp: false
- extendedWaitUntil:
    visible:
      text: Search
    timeout: 5000
- tapOn:
    text: Search
- inputText: ${ITEM} please
- inputText: ${secret:CARD}
- pressKey: Enter
- swipe:
    duration: 300
    direction: up
- extendedWaitUntil:
    visible:
      text: Cart
    timeout: 4000
- extendedWaitUntil:
    visible:
      resourceId: p:id/add
    timeout: 5000
- tapOn:
    resourceId: p:id/add
- copyTextFrom:
    id: total
    key: total
"""


# spec: ANYM-973
def test_a_point_tap_is_saved_by_the_resolved_nodes_id_or_exact_text_only_when_that_names_one_node():
    assert resolve_point(SCREEN, '150,320') == {'resourceId': 'p:id/add'}
    assert resolve_point(SCREEN, '10,520') == {'resourceId': 'p:id/row'}  # the id is unique though the text is not
    assert resolve_point(SCREEN, '10,920') is None  # no id, and the text 'Plain' is on two nodes
    assert resolve_point(SCREEN, '2000,5000') is None and resolve_point(SCREEN, 'x') is None
    with pytest.raises(SaveError) as error:
        compile_log(log(('open_app', {'package': PACKAGE}, {}), ('tap', {'point': '10,920'}, {})), {}, [], [], {})
    assert (error.value.code, error.value.step) == ('unstable_point_tap', 2)


# spec: ANYM-973
def test_a_log_that_cannot_fill_an_output_or_holds_an_unsupported_step_does_not_compile():
    reads = log(('read_text', {'selector': {'id': 'total'}}, {'read': 'Total 12.50'}))
    with pytest.raises(SaveError) as error:
        compile_log(reads, {}, [], ['total'], {'total': '99.00'})
    assert error.value.code == 'output_not_read:total'
    with pytest.raises(SaveError) as error:
        compile_log(log(('read_text', {'limit': 10}, {'read': 'Total 12.50'})), {}, [], ['total'], {'total': '12.50'})
    assert error.value.code == 'unsupported_read'
    with pytest.raises(SaveError) as error:
        compile_log(log(('run_flow', {'yaml': '- back'}, {})), {}, [], [], {})
    assert (error.value.code, error.value.step) == ('unsupported_step', 1)


# --- the run: draft, guarded test, repairs, publish -----------------------------------------------------------------------

STEPS = [{'call': 'open_app', 'args': {'package': PACKAGE}}, {'call': 'tap', 'args': {'selector': {'text': 'Add to cart'}}},
         {'call': 'read_text', 'args': {'selector': {'text': 'Total'}}},
         {'call': 'finish', 'args': {'summary': 'Total read', 'data': {'total': '12.50'}}}]
FIXED = ('- extendedWaitUntil: {visible: {text: Add to cart}, timeout: 5000}\n- tapOn: {text: Add to cart}\n'
         '- copyTextFrom: {text: Total, key: total}\n')


def saving(tmp_path, steps=None):
    binary, record = fake_engine(tmp_path, 'claude', steps or STEPS)
    config = write_config(tmp_path, claude=binary, default='claude')
    pass_probe(tmp_path, 'claude')
    return config, record


def run_saving(service, **fields):
    service.phone.field = 'Total 12.50'
    session = service.lease()
    done = service.finished(service.start(session, save_as='food-total', outputs=['total'], **fields)['request_id'])
    return session, done


def head(service, name='food-total'):
    """The library entry through the flows operation: the service's SQLite connection belongs to its own thread."""
    found = [row for row in service.post('flows', {'action': 'list'})['flows'] if row['name'] == name]
    return found[0] if found else None


def published(service, name='food-total'):
    return (head(service, name) or {}).get('version')


def source_of(service, stage='published', name='food-total'):
    return service.post('flows', {'action': 'get', 'name': name, 'stage': stage})['yaml']


# spec: ANYM-973
def test_a_passing_draft_test_publishes_and_the_saved_scenario_reruns_with_other_parameters(tmp_path):
    config, _ = saving(tmp_path, [{'call': 'open_app', 'args': {'package': PACKAGE}},
                                  {'call': 'type', 'args': {'text': 'Latte'}},
                                  {'call': 'read_text', 'args': {'selector': {'text': 'Total'}}},
                                  {'call': 'finish', 'args': {'summary': 'ok', 'data': {'total': '12.50'}}}])
    with Service(config) as service:
        session, done = run_saving(service, params={'ITEM': 'Latte'}, param_names=['ITEM'])
        scenario = done['scenario']
        assert done['state'] == 'succeeded' and done['data'] == {'total': '12.50'}
        assert scenario['published'] and scenario['repairs'] == 0 and scenario['name'] == 'food-total'
        assert scenario['version'] == published(service) and uuid.UUID(scenario['test_request_id'])
        source = source_of(service)
        assert '${ITEM}' in source and 'Latte' not in source
        service.phone.field = ''
        rerun = service.post('run_flow', {'device_id': 'home', 'session_id': session, 'request_id': str(uuid.uuid4()),
                                          'name': 'food-total', 'parameters': {'ITEM': 'Espresso'}, 'wait_s': 5})
        assert rerun['state'] == 'succeeded' and 'Espresso' in service.phone.field
        assert rerun['data'] == {'total': service.phone.field}  # the keyed read fills the output the run declared


# spec: ANYM-973
def test_a_failed_draft_test_is_repaired_with_flow_edit_and_published_only_after_a_passing_test(tmp_path):
    steps = STEPS[:-1] + [{'call': 'finish', 'args': {'summary': 'Total read', 'data': {'total': '12.50'}}},
                          {'call': 'flow_get'}, {'call': 'flow_edit', 'args': {'yaml': FIXED}}, {'call': 'flow_test'}]
    config, record = saving(tmp_path, steps)
    with Service(config) as service:
        service.phone.fail_calls = {'tapOn': {2}}  # the agent's own tap passes, the first draft test fails, the second passes
        service.phone.field = 'Total 12.50'
        request_id = service.start(service.lease(), save_as='food-total', outputs=['total'])['request_id']
        done = service.finished(request_id)
        replies = {name: reply for name, reply in json.loads(open(record).read())['replies']}
        assert done['state'] == 'succeeded' and done['scenario']['published'] and done['scenario']['repairs'] == 1
        assert 'the draft test failed' in json.dumps(replies['finish']) and replies['flow_edit'][0] == 200
        assert published(service) == done['scenario']['version']
        assert source_of(service) == FIXED


# spec: ANYM-973
def test_a_draft_that_keeps_failing_is_never_published_and_the_run_result_stays_succeeded(tmp_path):
    steps = STEPS + [{'call': 'flow_test'}, {'call': 'flow_test'}, {'call': 'flow_test'}, {'call': 'flow_test'}]
    config, _ = saving(tmp_path, steps)
    with Service(config) as service:
        service.phone.fail_from = {'extendedWaitUntil': 1}
        _, done = run_saving(service)
        assert done['state'] == 'succeeded' and done['finished'] is True
        assert done['scenario']['published'] is False and done['scenario']['error'] == 'test_failed'
        assert published(service) is None and head(service)['draft_version']


# spec: ANYM-973
def test_a_draft_test_stopped_by_the_guard_ends_the_run_there_and_never_publishes(tmp_path):
    config, _ = saving(tmp_path)
    with Service(config) as service:
        service.phone.stop_nth = {'tapOn': 2}
        _, done = run_saving(service)
        assert done['state'] == 'stopped_by_guard' and done['guard']['tool'] == 'flow_test'
        assert done['scenario']['published'] is False and published(service) is None


# spec: ANYM-973
def test_a_log_that_cannot_compile_saves_nothing_and_the_run_still_succeeds(tmp_path):
    steps = [{'call': 'open_app', 'args': {'package': PACKAGE}}, {'call': 'tap', 'args': {'point': '5,5000'}},
             {'call': 'finish', 'args': {'summary': 'ok', 'data': {}}}]
    config, _ = saving(tmp_path, steps)
    with Service(config) as service:
        session = service.lease()
        done = service.finished(service.start(session, save_as='food-total')['request_id'])
        assert done['state'] == 'succeeded' and done['scenario']['published'] is False
        assert done['scenario']['error'] == 'unstable_point_tap (step 2)' and done['scenario']['version'] is None
        assert head(service) is None


# spec: ANYM-973
def test_the_save_fields_and_the_repair_tools_are_refused_where_they_make_no_sense(tmp_path):
    config, _ = saving(tmp_path, [{'sleep': 3}])
    with Service(config) as service:
        session = service.lease()
        for fields in ({'param_names': ['ITEM']}, {'save_as': 'x', 'param_names': ['ITEM']}, {'save_as': 'bad name'},
                       {'save_as': 'x', 'params': {'ITEM': 'a'}, 'param_names': ['ITEM', 'ITEM']}):
            service.start(session, code=422, **fields)
        view = service.start(session)
        bearer = next(iter(service.app.state.agents.by_bearer))
        reply = httpx.post(service.url + '/agent-tool/flow_edit', json={'yaml': FIXED}, headers={'Authorization': 'Bearer ' + bearer},
                           timeout=30)
        assert reply.json()['is_error'] and 'no_scenario_to_repair' in reply.json()['text']
        service.post('agent', {'action': 'stop', 'request_id': view['request_id']})
