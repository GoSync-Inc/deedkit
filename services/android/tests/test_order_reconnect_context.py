"""Current order metadata on the first ordinary turn of a reconnected Codex thread."""

import json
from pathlib import Path

from agent_harness import Service
from test_native_dialogue import messages
from test_order_supervision import host, rows


# spec: ANYM-997
def test_reconnected_codex_receives_existing_orders_once_without_rewriting_them(tmp_path, monkeypatch):
    config, record = host(tmp_path, monkeypatch, delay=120)
    binary = Path(json.loads(config.read_text())['engines']['codex']['binary'])
    source = binary.read_text()
    old = 'state=dict(pending=True)'
    assert source.count(old) == 1
    binary.write_text(source.replace(old, """package='com.grabtaxi.passenger' if count == 1 else 'com.gojek.app',
                provider_order_id='example-' + str(count),
                state=dict(status='delivered' if count == 1 else 'preparing')"""))

    def starts(method):
        return [item['message'] for item in messages(record) if item['message'].get('method') == method]

    def caller_input():
        text = starts('turn/start')[-1]['params']['input'][0]['text']
        return json.loads(text.split('Caller input (nonsecret JSON):\n', 1)[1])

    def orders(service):
        return rows(service, '''SELECT order_id, original_request_id, package, lifecycle, responsible,
            next_check_at, native_thread_id, provider_order_id FROM android_order_obligations WHERE task_only=0 ORDER BY order_id''')

    with Service(config) as service:
        lease = service.lease()
        for _ in range(2):
            order = service.start(lease, goal='native-control')
            assert service.finished(order['request_id'])['state'] == 'succeeded'
        saved = orders(service)
        assert len(saved) == 2
        assert {order['package'] for order in saved} == {'com.grabtaxi.passenger', 'com.gojek.app'}
        assert 'current_orders' not in caller_input()

    with Service(config) as service:
        goal = '  Home, check the existing orders.'
        check = service.start(lease, goal=goal)
        outcome = service.finished(check['request_id'])
        assert outcome['state'] == 'succeeded'
        assert outcome['native_session']['thread_id'] == saved[0]['native_thread_id']
        assert caller_input()['goal'] == goal
        assert caller_input()['current_orders'] == {
            'items': [
                {'obligation_ref': order['order_id'], 'package': order['package'],
                 'provider_order_id': order['provider_order_id'], 'responsible': order['responsible'],
                 'next_check_at': order['next_check_at']} | (
                    {'completion_pending': 'delivered'} if order['package'] == 'com.grabtaxi.passenger' else {})
                for order in saved
            ],
            'more': False,
        }
        assert len(starts('thread/start')) == 1
        assert len(starts('thread/resume')) == 1
        assert len(starts('turn/start')) == 3

        followup = service.start(lease, goal='  Home, another ordinary question.')
        assert service.finished(followup['request_id'])['state'] == 'succeeded'
        assert 'current_orders' not in caller_input()
        assert orders(service) == saved
