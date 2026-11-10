"""Owner-scoped draft publication and pinned, data-driven Android actions."""

import hashlib
import json
import uuid
from xml.etree import ElementTree

import pytest

from deedkit_android.config import Refused
from deedkit_android.store import Store

MAP = '''package: example.shop
selectors:
  orders:
    - {textExact: Orders}
  input:
    - {resourceId: example.shop:id/search}
screens:
  orders:
    require: [{textExact: Orders, selected: true}]
lists:
  products:
    item: {resourceId: example.shop:id/item}
    fields:
      title: {resourceId: example.shop:id/title}
'''
FLOW = '''map: shop
side_effects: none
steps:
  - tapElement: {selector: orders, expect_screen: orders}
'''


@pytest.fixture
def store(tmp_path):
    state = Store(tmp_path / 'state')
    yield state
    state.close()


def draft(store, kind, name, source, base=None):
    return store.library.manage('one', action='draft', kind=kind, name=name, source=source, base_version=base)


def evidence(store, pins, state='succeeded'):
    request_id = str(uuid.uuid4())
    store.admit('one', request_id, {})
    store.finish('one', request_id, state, {'library': {'pins': pins,
        'root': next((pin for pin in pins if pin['kind'] == 'flow'), None)},
        'steps': [{'map': pin} for pin in pins if pin['kind'] == 'map'] or [{'command': 'back'}]})
    return request_id


# spec: ANYM-954
def test_candidate_heads_conflicts_and_immutable_history(store):
    first = draft(store, 'map', 'shop', MAP)
    with pytest.raises(Refused, match='library_version_conflict'):
        draft(store, 'map', 'shop', MAP + '\n# changed')
    second = draft(store, 'map', 'shop', MAP + '\n# changed', first['version'])
    assert store.library.read('one', 'map', 'shop', first['version'])['yaml'] == MAP
    assert store.library.read('one', 'map', 'shop', stage='draft')['version'] == second['version']
    with pytest.raises(Refused):
        store.library.read('two', 'map', 'shop', first['version'])
    history = store.library.manage('one', action='history', kind='map', name='shop')
    assert len(history['events']) == 2


# spec: ANYM-954
def test_only_successful_current_test_publishes_and_rollback_retains_history(store):
    candidate = draft(store, 'map', 'shop', MAP)
    pin = {'kind': 'map', 'name': 'shop', 'version': candidate['version']}
    for state in ('paused', 'failed', 'unknown'):
        with pytest.raises(Refused, match='successful_test_required'):
            store.library.manage('one', action='publish', kind='map', name='shop',
                                 version=candidate['version'], test_request_id=evidence(store, [pin], state))
    store.library.manage('one', action='publish', kind='map', name='shop',
                         version=candidate['version'], test_request_id=evidence(store, [pin]))
    second = draft(store, 'map', 'shop', MAP + '\n# v2', candidate['version'])
    store.library.manage('one', action='publish', kind='map', name='shop', base_version=candidate['version'],
                         version=second['version'], test_request_id=evidence(store, [{**pin, 'version': second['version']}]))
    store.library.manage('one', action='rollback', kind='map', name='shop',
                         version=candidate['version'], base_version=second['version'])
    assert store.library.read('one', 'map', 'shop')['version'] == candidate['version']
    assert store.library.read('one', 'map', 'shop', second['version'])['yaml'].endswith('# v2')


# spec: ANYM-954
def test_publish_refuses_a_dependency_changed_since_the_test(store):
    app_map = draft(store, 'map', 'shop', MAP)
    flow = draft(store, 'flow', 'orders', FLOW)
    pins = [{'kind': 'map', 'name': 'shop', 'version': app_map['version']},
            {'kind': 'flow', 'name': 'orders', 'version': flow['version']}]
    request_id = evidence(store, pins)
    draft(store, 'map', 'shop', MAP + '\n# new', app_map['version'])
    with pytest.raises(Refused, match='test_dependencies_changed'):
        store.library.manage('one', action='publish', kind='flow', name='orders',
                             version=flow['version'], test_request_id=request_id)


# spec: ANYM-954
def test_legacy_flows_import_and_keep_their_original_interface(store):
    first = store.flow('one', 'put', 'old', '- back', None)
    store.flow('one', 'put', 'old', '- scroll', first['version'])
    assert store.library.read('one', 'flow', 'old', first['version'])['yaml'] == '- back'
    store.flow('one', 'delete', 'old', None, None)
    assert store.flow('one', 'list', None, None, None) == {'flows': []}
    assert store.library.read('one', 'flow', 'old', first['version'])['yaml'] == '- back'


# spec: ANYM-954
def test_scenario_compilation_pins_maps_and_subflows_and_keeps_inputs_literal(store):
    from deedkit_android.app_maps import compile_scenario

    app_map = draft(store, 'map', 'shop', MAP)
    flow = draft(store, 'flow', 'orders', FLOW)
    source = 'map: shop\nsteps:\n  - runFlow: {name: orders}\n  - inputText: ${QUERY}\n'
    commands, metadata = compile_scenario(store.library, 'one', source, {'QUERY': 'чай: [yes]'}, stage='draft')
    assert commands[-1] == {'inputText': 'чай: [yes]'}
    assert {tuple(sorted(pin.items())) for pin in metadata['pins']} == {
        tuple(sorted({'kind': 'map', 'name': 'shop', 'version': app_map['version']}.items())),
        tuple(sorted({'kind': 'flow', 'name': 'orders', 'version': flow['version']}.items()))}
    draft(store, 'map', 'shop', MAP.replace('Orders', 'NewOrders'), app_map['version'])
    pinned = commands[0]['runFlow']['commands'][0]['tapElement']['app_map']
    assert pinned['selectors']['orders'] == [{'textExact': 'Orders'}]


# spec: ANYM-954
def test_map_validation_rejects_unknown_names_and_unchecked_coordinates(store):
    from deedkit_android.app_maps import compile_scenario, validate_map

    with pytest.raises(Refused):
        validate_map(MAP + '\nunknown: value')
    draft(store, 'map', 'shop', MAP)
    with pytest.raises(Refused, match='unknown_map_selector'):
        compile_scenario(store.library, 'one', FLOW.replace('selector: orders', 'selector: missing'), {}, stage='draft')
    point_map = MAP.replace('{textExact: Orders}', '{point: "40%,90%"}', 1)
    draft(store, 'map', 'points', point_map)
    with pytest.raises(Refused, match='coordinate_requires_screen'):
        compile_scenario(store.library, 'one', 'map: points\nsteps: [{tapElement: {selector: orders}}]', {}, stage='draft')


# spec: ANYM-954
def test_map_matching_never_chooses_an_ambiguous_candidate():
    from deedkit_android.app_maps import validate_map
    from deedkit_android.map_runtime import resolve_selector

    app_map = validate_map(MAP)
    root = ElementTree.fromstring('<hierarchy><node text="Orders"/><node text="Orders"/></hierarchy>')
    with pytest.raises(Refused, match='ambiguous_selector'):
        resolve_selector(root, app_map['selectors']['orders'])
    root = ElementTree.fromstring('<hierarchy><node text="Orders" bounds="[0,0][20,40]"/></hierarchy>')
    node, trace = resolve_selector(root, app_map['selectors']['orders'])
    assert node.get('bounds') == '[0,0][20,40]' and trace == [{'candidate': 0, 'matches': 1}]


# spec: ANYM-954
def test_list_extraction_masks_passwords_and_preserves_rows():
    from deedkit_android.app_maps import validate_map
    from deedkit_android.map_runtime import extract_list

    root = ElementTree.fromstring('''<hierarchy>
      <node resource-id="example.shop:id/item"><node resource-id="example.shop:id/title" text="Tea"/></node>
      <node resource-id="example.shop:id/item"><node resource-id="example.shop:id/title" text="secret" password="true"/></node>
    </hierarchy>''')
    rows = extract_list(root, validate_map(MAP)['lists']['products'], ['title'], 10)
    assert rows == [{'title': 'Tea'}, {'title': None}]


# spec: ANYM-954
def test_operation_identity_survives_a_published_head_change(store):
    request_id = str(uuid.uuid4())
    identity = {'name': 'orders', 'parameters': {}, 'device_id': 'home'}
    store.admit('one', request_id, {'flow': [{'back': None}]}, identity=identity,
                metadata={'library': {'pins': []}})
    store.finish('one', request_id, 'succeeded', {'library': {'pins': []}})
    assert store.replay('one', request_id, identity)['state'] == 'succeeded'
    with pytest.raises(Refused, match='request_id_conflict'):
        store.replay('one', request_id, {**identity, 'name': 'other'})
    assert store.replay('two', request_id, identity) is None
    assert store.db.execute('SELECT identity FROM operation_requests').fetchone()[0] == hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


# spec: ANYM-954
def test_declared_defaults_reach_subflows_and_dates_stay_serializable(store):
    from deedkit_android.app_maps import compile_scenario, validate_map

    dated_map = MAP + '\nmeta: {updated_at: 2026-11-11}'
    assert validate_map(dated_map)['meta']['updated_at'] == '2026-11-11'
    store.flow('one', 'put', 'child', '- inputText: ${QUERY}', None)
    source = 'params:\n  QUERY: {type: string, default: tea}\nsteps: [{runFlow: {name: child}}]'
    commands, _ = compile_scenario(store.library, 'one', source, {})
    assert commands[0]['runFlow']['commands'] == [{'inputText': 'tea'}]


# spec: ANYM-954
def test_invalid_map_types_are_refused_before_execution(store):
    from deedkit_android.app_maps import compile_scenario, validate_map

    for source in (MAP + '\ndeep_links: {orders: {url: "example://orders", screen: []}}',
                   MAP + '\npopups: {ad: {detect: [], press: back}}', MAP + '\nmeta: {a: .nan}'):
        with pytest.raises(Refused):
            validate_map(source)
    draft(store, 'map', 'shop', MAP)
    for source in ('map: shop\nsteps: [{tapElement: {selector: []}}]',
                   'params: {A: {max_length: []}}\nsteps: [back]'):
        with pytest.raises(Refused):
            compile_scenario(store.library, 'one', source, {})


# spec: ANYM-954
def test_map_tap_verifies_the_screen_and_ambiguity_never_clicks():
    from deedkit_android.app_maps import validate_map
    from deedkit_android.map_runtime import execute_map

    class Device:
        selected = False
        ambiguous = False
        clicks = []

        def jsonrpc_call(self, method, args):
            node = '<node text="Orders" bounds="[0,0][20,40]" selected="' + str(self.selected).lower() + '"/>'
            return '<hierarchy>' + node * (2 if self.ambiguous else 1) + '</hierarchy>'

        def click(self, x, y):
            self.clicks.append((x, y))
            self.selected = True

    device = Device()
    options = {'app_map': validate_map(MAP), 'selector': 'orders', 'expect_screen': 'orders', 'timeout': 0}
    result = execute_map(device, 'tapElement', options, None)
    assert result['screen'] == 'orders' and device.clicks == [(10, 20)]
    device.ambiguous = True
    with pytest.raises(Refused, match='ambiguous_selector'):
        execute_map(device, 'tapElement', options, None)
    assert device.clicks == [(10, 20)]


# spec: ANYM-954
def test_publishing_requires_executed_root_and_published_dependencies(store):
    app_map = draft(store, 'map', 'shop', MAP)
    flow = draft(store, 'flow', 'orders', FLOW)
    map_pin = {'kind': 'map', 'name': 'shop', 'version': app_map['version']}
    flow_pin = {'kind': 'flow', 'name': 'orders', 'version': flow['version']}
    skipped = str(uuid.uuid4())
    store.admit('one', skipped, {})
    store.finish('one', skipped, 'succeeded', {'library': {'pins': [map_pin, flow_pin]}, 'steps': []})
    for pin in (map_pin, flow_pin):
        with pytest.raises(Refused, match='successful_test_required'):
            store.library.manage('one', action='publish', **pin, test_request_id=skipped)
    tested = evidence(store, [map_pin, flow_pin])
    with pytest.raises(Refused, match='test_dependency_not_published'):
        store.library.manage('one', action='publish', **flow_pin, test_request_id=tested)
    store.library.manage('one', action='publish', **map_pin, test_request_id=tested)
    store.library.manage('one', action='publish', **flow_pin, test_request_id=tested)


# spec: ANYM-954
def test_map_checkpoint_source_recompiles_with_immutable_map_versions(store):
    import yaml
    from deedkit_android.app_maps import compile_scenario, continuation

    version = draft(store, 'map', 'shop', MAP)['version']
    commands, _ = compile_scenario(store.library, 'one', FLOW, {}, stage='draft')
    source = yaml.safe_dump(continuation(commands))
    assert 'app_map' not in source and 'map_ref' not in source
    draft(store, 'map', 'shop', MAP.replace('Orders', 'NewOrders'), version)
    resumed, pins = compile_scenario(store.library, 'one', source, {})
    assert resumed == commands
    assert pins['pins'] == [{'kind': 'map', 'name': 'shop', 'version': version}]


# spec: ANYM-954
def test_control_flow_selectors_are_validated_before_admission(store):
    from deedkit_android.app_maps import compile_scenario

    for source in ('- back\n- runFlow: {when: {visible: {file: x}}, commands: [back]}',
                   '- back\n- repeat: {while: {visible: {selected: bad}}, commands: [back]}'):
        with pytest.raises(Refused):
            compile_scenario(store.library, 'one', source, {})


# spec: ANYM-954
def test_reference_expansion_stops_at_the_shared_budget(store, monkeypatch):
    from deedkit_android.app_maps import compile_scenario

    store.flow('one', 'put', 'leaf', '- back', None)
    for index in range(6):
        child = 'leaf' if index == 0 else 'branch' + str(index - 1)
        store.flow('one', 'put', 'branch' + str(index), ('- runFlow: {name: ' + child + '}\n') * 20, None)
    count = [0]
    original = store.library.read

    def read(*args, **kwargs):
        count[0] += 1
        assert count[0] <= 201, 'reference expansion exceeded the bounded read budget'
        return original(*args, **kwargs)

    monkeypatch.setattr(store.library, 'read', read)
    with pytest.raises(Refused, match='too_many_flow_commands'):
        compile_scenario(store.library, 'one', '- runFlow: {name: branch5}', {})
    assert count[0] <= 201


# spec: ANYM-954
def test_visible_list_keeps_distinct_identical_rows():
    from deedkit_android.app_maps import validate_map
    from deedkit_android.map_runtime import execute_map

    class Device:
        def jsonrpc_call(self, method, args):
            item = '<node resource-id="example.shop:id/item"><node resource-id="example.shop:id/title" text="Tea"/></node>'
            return '<hierarchy>' + item * 2 + '</hierarchy>'

    result = execute_map(Device(), 'readList', {'app_map': validate_map(MAP), 'list': 'products'}, None)
    assert result['rows'] == [{'title': 'Tea'}, {'title': 'Tea'}]
