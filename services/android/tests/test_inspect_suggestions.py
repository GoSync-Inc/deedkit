"""Map inspection with a synthetic hierarchy: compact nodes and suggested selectors."""

import pytest

from deedkit_android.app_maps import validate_action, validate_map
from deedkit_android.config import Refused

MAP = '''package: example.shop
selectors:
  search: [{resourceId: 'example.shop:id/search'}]
screens:
  orders:
    require: [search]
'''
ROW = ('<node class="android.view.ViewGroup" resource-id="example.shop:id/order" bounds="[0,{top}][100,{bottom}]" clickable="true">'
       '<node class="android.widget.TextView" resource-id="example.shop:id/status" text="Selesai {n}" bounds="[0,{top}][50,{bottom}]" />'
       '<node class="android.widget.TextView" resource-id="example.shop:id/price" text="Rp {n}" bounds="[50,{top}][100,{bottom}]" />'
       '</node>')
CANVAS = '<node class="android.view.View" bounds="[0,{top}][100,{bottom}]" />'
XML = ('<hierarchy><node class="android.widget.FrameLayout" bounds="[0,0][100,900]">'
       '<node class="android.widget.EditText" resource-id="example.shop:id/search" text="Cari transaksi" bounds="[0,0][100,40]" />'
       '<node class="android.widget.EditText" resource-id="example.shop:id/pin" text="1234" password="true" bounds="[0,40][100,60]" />'
       '<node class="android.widget.ImageView" content-desc="shopping cart" bounds="[0,60][20,80]" clickable="true" />'
       '<node class="androidx.recyclerview.widget.RecyclerView" resource-id="example.shop:id/list" bounds="[0,100][100,500]" scrollable="true">'
       + ''.join(ROW.format(top=100 + i * 100, bottom=200 + i * 100, n=i) for i in range(3)) +
       '</node><node class="android.widget.ScrollView" bounds="[0,500][100,900]">'
       + ''.join(CANVAS.format(top=500 + i * 100, bottom=600 + i * 100) for i in range(4)) +
       '</node></node></hierarchy>')


class Device:
    def jsonrpc_call(self, method, args, timeout=10):
        return XML


# spec: ANYM-957
def test_inspect_map_returns_compact_nodes_with_masked_passwords():
    from deedkit_android.map_runtime import execute_map

    app_map = validate_map(MAP)
    plain = execute_map(Device(), 'inspectMap', {'app_map': app_map}, None)
    assert set(plain) == {'selectors', 'screens'}
    result = execute_map(Device(), 'inspectMap', {'app_map': app_map, 'hierarchy': True, 'max_nodes': 5}, None)
    nodes = result['hierarchy']['nodes']
    assert len(nodes) == 5 and result['hierarchy']['total'] == 19 and result['hierarchy']['truncated'] is True
    assert nodes[1] == {'index': 1, 'depth': 1, 'class': 'android.widget.EditText', 'resource-id': 'example.shop:id/search',
                        'text': 'Cari transaksi', 'bounds': '[0,0][100,40]'}
    assert nodes[2]['password'] is True and 'text' not in nodes[2]
    assert nodes[3]['clickable'] is True and nodes[3]['content-desc'] == 'shopping cart'
    assert result['selectors']['search']['found'] is True


# spec: ANYM-957
def test_inspect_map_suggests_unique_selectors_and_list_candidates():
    from deedkit_android.map_runtime import execute_map

    result = execute_map(Device(), 'inspectMap', {'app_map': validate_map(MAP), 'suggest': True}, None)
    suggestions = result['suggestions']
    selectors = [item['selector'] for item in suggestions['selectors']]
    assert {'resourceId': 'example.shop:id/search'} in selectors
    assert {'description': 'shopping cart'} in selectors and {'resourceId': 'example.shop:id/list'} in selectors
    assert all('1234' not in str(item) for item in suggestions['selectors'])
    assert {'resourceId': 'example.shop:id/order'} not in selectors
    lists = suggestions['lists']
    assert lists[0]['item'] == {'resourceId': 'example.shop:id/order'} and lists[0]['count'] == 3
    assert lists[0]['fields'] == {'status': {'resourceId': 'example.shop:id/status'},
                                  'price': {'resourceId': 'example.shop:id/price'}}
    assert lists[0]['has_text'] is True
    canvas = next(item for item in lists if item['item'] == {'className': 'android.view.View'})
    assert canvas['count'] == 4 and canvas['fields'] == {} and canvas['has_text'] is False
    assert 'readScreenText' in canvas['hint']
    assert len(suggestions['selectors']) <= 50 and len(lists) <= 50


# spec: ANYM-957
def test_inspect_options_are_validated_before_admission():
    app_map = validate_map(MAP)
    for options in ({'hierarchy': 'yes'}, {'suggest': 1}, {'max_nodes': 0}, {'max_nodes': 501}, {'nodes': True}):
        with pytest.raises(Refused):
            validate_action('inspectMap', dict(options), app_map)
    validate_action('inspectMap', {'hierarchy': 'true', 'suggest': True, 'max_nodes': '300'}, app_map)
