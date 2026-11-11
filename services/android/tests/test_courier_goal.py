"""Courier chat autonomy (ANYM-978): a goal per order, bounded replies, quiet handover. Synthetic phone, no provider."""

import asyncio
import base64
import importlib
from types import SimpleNamespace
import uuid

import pytest
from fastapi.testclient import TestClient

from deedkit_android.app import create_app
from deedkit_android.store import Store
from test_courier_chat import CHAT_ENTRY, FIELD, ORDER, PNG, SEND, Phone, element, inside, service_config

GOAL = 'Get the courier to the lobby of unit 12B'
WORDS = 'Handle the courier of Warung Sate for me: say I am in the lobby, unit 12B, and ask reception if he is lost'
HELLO, ETA = 'Saya di lobi unit 12B, ya', 'Boleh kabari kalau sudah dekat?'


def cc():
    return importlib.import_module('deedkit_android.courier_chat')


def cg():
    return importlib.import_module('deedkit_android.courier_goal')


class ChatPhone(Phone):
    """The Phone of test_courier_chat with a chat that keeps every row and shows the order in its title."""

    def __init__(self, state='order', **switches):
        super().__init__(state, **switches)
        self.rows = []

    def elements(self):
        if self.state == 'order':
            return super().elements()
        found = [element('' if self.switches.get('no_title') else f'Driver for {ORDER}', (40, 100, 600, 180))]
        found += [element(text, (40, 300 + 60 * index, 500, 340 + 60 * index)) for index, text in enumerate(self.rows)]
        found.append(element(self.typed or 'Type a message', FIELD, True, 'android.widget.EditText',
                             hint='Type a message', focused=str(self.focused).lower()))
        found.append(element('', SEND, True, 'android.widget.ImageButton', **{'content-desc': 'Send'}))
        if self.state == 'typed' and self.switches.get('pay_after_typed'):
            found.append(element('Bayar Rp45.000', (100, 1500, 400, 1600), True, 'android.widget.Button'))
        return found + list(self.switches.get('extra', ()))

    async def call(self, device, command, **options):
        result = await super().call(device, command, **options)
        if 'inputText' in command and self.switches.get('courier_after_typed'):
            self.rows.append(self.switches['courier_after_typed'])
        return result

    async def adb(self, argv, timeout, *args, **kwargs):
        typed = self.typed
        result = await super().adb(argv, timeout, *args, **kwargs)
        if self.state == 'sent':
            self.rows.append(typed)
            self.state = 'chat'
        return result


@pytest.fixture
def world(tmp_path, monkeypatch):
    from deedkit_android.device import Devices

    def build(state='order', **switches):
        phone, now = ChatPhone(state, **switches), [1_000_000.0]
        store = Store(tmp_path / ('state' + uuid.uuid4().hex))
        devices = Devices(SimpleNamespace(devices={'phone': 'SERIAL'}, adb='/synthetic', uiautomator2_jar='/synthetic',
                                          state_dir=str(tmp_path / 'state')), store)
        devices.ui = phone
        found = SimpleNamespace(phone=phone, store=store, devices=devices, now=now, reads=0, owner='owner',
                                token=store.reserve('owner', 'phone', 'acquire')['session_id'])

        async def hierarchy(device):
            found.reads += 1
            return {'hierarchy_source': 'uiautomator2', 'elements': [dict(item) for item in phone.elements()]}

        async def image(owner, device, mode, crop):
            return {'image': {'mime_type': 'image/png', 'data': base64.b64encode(PNG + b'x').decode()}}

        monkeypatch.setattr(devices, 'capture_hierarchy', hierarchy)
        monkeypatch.setattr(devices, 'capture_image', image)
        monkeypatch.setattr('deedkit_android.courier_chat.run_process', phone.adb)
        monkeypatch.setattr('deedkit_android.courier_chat.POLL_S', 0)
        monkeypatch.setattr('deedkit_android.courier_chat.SEND_WAIT_S', 0.05)
        monkeypatch.setattr('deedkit_android.courier_goal.clock', lambda: now[0])
        return found

    return build


def run(world, scenario):
    async def main():
        try:
            return await scenario
        finally:
            await world.devices.close()
            world.store.close()
    return asyncio.run(main())


async def act(world, action, **overrides):
    args = {'action': action, 'device_id': 'phone', 'session_id': world.token, 'request_id': str(uuid.uuid4()),
            'app': 'gojek', 'order': ORDER, 'timeout_s': 60, 'wait_s': 5, **overrides}
    return await cc().handle(world.devices, world.owner, args)


async def goal(world, **overrides):
    values = {'goal': GOAL, 'owner_instruction': WORDS, **overrides}
    return await act(world, 'goal', **values)


async def reply(world, message=HELLO, wait=31, **overrides):
    world.now[0] += wait
    return await act(world, 'reply', message=message, **overrides)


def idle(world):
    assert world.reads == 0 and world.phone.ui_calls == [] and world.phone.adb_taps == []


# spec: ANYM-978
def test_a_goal_records_one_rule_per_order_without_touching_the_phone(world):
    w = world()

    async def scenario():
        request_id = str(uuid.uuid4())
        first = await goal(w, request_id=request_id, max_messages=3)
        again = await goal(w, request_id=request_id, max_messages=3)
        return first, again, await act(w, 'summary')

    first, again, summary = run(w, scenario())
    assert first['state'] == 'active' and first['max_messages'] == 3 and first['sent'] == 0
    assert first['min_interval_s'] == 30 and first['expires_at'] == 1_000_000 + 180 * 60 and first['goal'] == GOAL
    assert again == first and len(summary['log']) == 1 and summary['log'][0]['event'] == 'goal'
    assert summary['log'][0]['owner_instruction'] == WORDS
    idle(w)


# spec: ANYM-978
def test_goal_arguments_are_checked_by_the_route_and_message_is_needed_only_to_type(tmp_path):
    with TestClient(create_app(service_config(tmp_path))) as client:
        base = {'device_id': 'home', 'session_id': str(uuid.uuid4()), 'request_id': str(uuid.uuid4()),
                'action': 'goal', 'app': 'gojek', 'order': ORDER, 'goal': GOAL, 'owner_instruction': WORDS}
        headers = {'Authorization': 'Bearer one'}
        for change in ({'max_messages': 0}, {'max_messages': 13}, {'min_interval_s': 9}, {'min_interval_s': 301},
                       {'ttl_min': 481}, {'goal': ''}, {'goal': 'x' * 301}, {'owner_instruction': 'ok'},
                       {'owner_instruction': None}, {'goal': None}, {'standing': 'maybe'}):
            response = client.post('/v1/courier_chat', json={**base, **change}, headers=headers)
            assert response.status_code == 422 and response.json()['error'] == 'invalid_arguments', change
        for action in ('reply', 'preview', 'send'):
            response = client.post('/v1/courier_chat', json={**base, 'action': action}, headers=headers)
            assert response.status_code == 422, action
        assert client.post('/v1/courier_chat', json={**base, 'standing': True, 'ttl_min': 10080},
                           headers=headers).status_code == 200
        assert client.post('/v1/courier_chat', json={**base, 'action': 'summary', 'request_id': str(uuid.uuid4())},
                           headers={'Authorization': 'Bearer reader'}).status_code == 403


# spec: ANYM-978
def test_issuing_a_goal_again_keeps_the_counters_and_reactivates_a_handed_over_rule(world):
    w = world('chat')

    async def scenario():
        await goal(w, max_messages=2)
        await reply(w)
        await act(w, 'handover', reason='the courier speaks another language')
        refused = await reply(w, ETA)
        again = await goal(w, max_messages=2, owner_instruction='Yes, carry on with him')
        return refused, again, await reply(w, ETA)

    refused, again, second = run(w, scenario())
    assert refused['error'] == 'handed_over' and again['state'] == 'active' and again['sent'] == 1
    assert second['state'] == 'succeeded' and second['message_number'] == 2


# spec: ANYM-978
def test_a_rule_expires_and_an_owner_has_at_most_five_active_orders(world):
    w = world()

    async def scenario():
        await goal(w, ttl_min=10)
        w.now[0] += 11 * 60
        expired = await act(w, 'reply', message=HELLO)
        for number in range(5):
            await goal(w, order=f'Order number {number}')
        return expired

    expired = run(w, scenario())
    assert expired['error'] == 'goal_expired' and expired['state'] == 'failed'
    w2 = world()

    async def sixth():
        for number in range(6):
            await goal(w2, order=f'Order number {number}')

    with pytest.raises(cc().Refused) as refused:
        run(w2, sixth())
    assert refused.value.code == 'too_many_active_orders'
    idle(w)


# spec: ANYM-978
def test_a_reply_needs_a_rule_and_touches_nothing_without_one(world):
    w = world()
    result = run(w, act(w, 'reply', message=HELLO))
    assert result['state'] == 'failed' and result['error'] == 'goal_required' and result['side_effects'] == 'none'
    assert result['sent'] is False and 'observation' not in result
    assert result['next_call']['arguments']['action'] == 'preview'
    idle(w)


# spec: ANYM-978
def test_a_reply_within_the_goal_sends_once_through_the_three_nodes_with_its_receipt(world):
    w = world()

    async def scenario():
        await goal(w)
        return await reply(w)

    result = run(w, scenario())
    assert result['state'] == 'succeeded' and result['sent'] is True and result['side_effects'] == 'sent'
    assert result['autonomous'] is True and result['message_number'] == 1 and result['max_messages'] == 6
    assert result['goal'] == GOAL and result['authorized_by'] == WORDS and result['order_seen'] is True
    assert result['message'] == HELLO and HELLO in result['chat'] and 'owner_instruction' not in result
    assert result['owner_summary'].startswith('Courier chat, Warung Sate') and '1 of 6' in result['owner_summary']
    assert w.phone.rows == [HELLO] and len(w.phone.adb_taps) == 1 and inside(SEND, *w.phone.adb_taps[0])
    points = [tuple(int(p) for p in c['tapOn']['point'].split(',')) for c in w.phone.ui_calls if 'tapOn' in c]
    assert points and all(inside(CHAT_ENTRY, *p) or inside(FIELD, *p) for p in points)


# spec: ANYM-978
def test_the_order_not_being_on_screen_is_recorded_not_blocking(world):
    w = world(no_title=True)

    async def scenario():
        await goal(w)
        return await reply(w)

    result = run(w, scenario())
    assert result['state'] == 'succeeded' and result['order_seen'] is False


# spec: ANYM-978
def test_a_standing_rule_opens_the_rule_of_each_order_on_first_use(world):
    w = world()

    async def scenario():
        await goal(w, order='all my deliveries', standing=True, owner_instruction='Handle my deliveries, logistics only')
        result = await reply(w)
        return result, await act(w, 'summary')

    result, summary = run(w, scenario())
    assert result['state'] == 'succeeded' and result['goal'] == GOAL
    assert result['authorized_by'] == 'Handle my deliveries, logistics only'
    assert summary['sent'] == 1 and summary['max_messages'] == 6 and summary['standing'] is False


# spec: ANYM-978
def test_the_message_cap_counts_attempts_and_hands_over_with_no_device_call(world):
    w = world('chat')

    async def scenario():
        await goal(w, max_messages=2)
        await reply(w)
        await reply(w, ETA)
        w.phone.ui_calls.clear()
        reads = w.reads
        return await reply(w, 'Saya tunggu di lobi'), reads

    third, reads = run(w, scenario())
    assert third['state'] == 'stopped' and third['stop_reason'] == 'handover'
    assert third['handover']['reason'] == 'message_cap_reached' and third['side_effects'] == 'none'
    assert w.reads == reads and w.phone.ui_calls == [] and len(w.phone.adb_taps) == 2


# spec: ANYM-978
def test_the_rate_limit_names_when_to_retry_and_touches_nothing(world):
    w = world('chat')

    async def scenario():
        await goal(w)
        await reply(w)
        reads = w.reads
        w.now[0] += 10
        early = await act(w, 'reply', message=ETA)
        return early, reads, w.reads, await reply(w, ETA, wait=25)

    early, reads, after, later = run(w, scenario())
    assert early['error'] == 'rate_limited' and early['state'] == 'failed' and 19 <= early['retry_after_s'] <= 20
    assert after == reads
    assert later['state'] == 'succeeded' and later['message_number'] == 2


# spec: ANYM-978
@pytest.mark.parametrize('message,topic', [
    ('Boleh kasih tip 10rb?', 'money'), ('I will pay cash on arrival', 'money'), ('Rp 20.000 ya', 'money'),
    ('Оставлю чаевые', 'money'), ('Please cancel the order', 'cancel'), ('Batalkan saja pesanannya', 'cancel'),
    ('I want a refund', 'refund'), ('I will complain to support', 'complaint'), ('Saya mau komplain', 'complaint'),
    ('My PIN is 1234', 'secret'), ('The OTP is 5555', 'secret'), ('Send the card number', 'secret'),
    ('Call 0812345678 please', 'number'), ('Look at https://example.com/x', 'link'), ('Mail me@example.com', 'link'),
    ('Dasar anjing kamu', 'hostile'), ('x' * 301, 'length')])
def test_a_message_outside_the_goal_is_refused_before_the_phone_with_the_preview_as_next_call(world, message, topic):
    w = world()

    async def scenario():
        await goal(w)
        return await reply(w, message)

    result = run(w, scenario())
    assert result['state'] == 'failed' and result['error'] == 'outside_goal' and result['topic'] == topic
    assert result['next_call']['arguments']['action'] == 'preview' and result['side_effects'] == 'none'
    assert w.phone.ui_calls == [] and w.phone.adb_taps == []


# spec: ANYM-978
def test_the_topic_lists_see_case_spacing_and_punctuation_and_leave_logistics_alone():
    topic = cg().topic
    for text, wanted in (('TIP!', 'money'), ('  Tip   please ', 'money'), ('...cAnCeL...', 'cancel'),
                         ('pin', 'secret'), ('Refund?', 'refund'), ('send OTP,now', 'secret')):
        assert topic(text) == wanted, text
        assert topic(text.upper()) == wanted and topic('hello ' + text + ' there') == wanted
    for fine in ('Tolong ke pintu depan, saya di lobi unit 12B', 'Please ask reception, I am in unit 12B',
                 'Can you call me when you arrive?', 'Saya di gerbang kedua, 5 menit lagi', 'Pintu samping, lantai 12'):
        assert topic(fine) is None, fine


# spec: ANYM-978
def test_three_unreported_messages_stop_the_fourth_until_the_summary(world):
    w = world('chat')

    async def scenario():
        await goal(w)
        for text in ('Satu', 'Dua', 'Tiga'):
            await reply(w, text)
        blocked = await reply(w, 'Empat')
        summary = await act(w, 'summary')
        return blocked, summary, await reply(w, 'Empat')

    blocked, summary, fourth = run(w, scenario())
    assert blocked['error'] == 'report_to_owner_first' and summary['unreported'] == 0
    assert fourth['state'] == 'succeeded' and fourth['message_number'] == 4


# spec: ANYM-978
def test_the_summary_lists_every_attempt_with_time_request_and_outcome(world):
    w = world('chat')

    async def scenario():
        await goal(w)
        first = await reply(w, 'Satu')
        w.phone.switches['tap_times_out'] = True
        second = await reply(w, 'Dua')
        return first, second, await act(w, 'summary'), w.store.operation('owner', second['request_id'])

    first, second, summary, stored = run(w, scenario())
    attempts = [row for row in summary['log'] if row.get('message')]
    assert [row['message'] for row in attempts] == ['Satu', 'Dua'] and attempts[0]['request_id'] == first['request_id']
    assert [row['outcome'] for row in attempts] == ['sent', 'unknown'] and attempts[1]['request_id'] == second['request_id']
    assert summary['sent'] == 2 and summary['state'] == 'handed_over' and 'at' in attempts[0]
    assert stored['state'] == 'unknown'


# spec: ANYM-978
def test_a_read_opens_the_chat_and_returns_each_courier_row_once(world):
    w = world()
    w.phone.rows = ['Saya sudah di depan']

    async def scenario():
        await goal(w)
        first = await act(w, 'read')
        w.phone.rows.append('Unitnya yang mana?')
        return first, await act(w, 'read'), await act(w, 'read')

    first, second, third = run(w, scenario())
    assert first['state'] == 'succeeded' and 'Saya sudah di depan' in first['new_from_courier']
    assert second['new_from_courier'] == ['Unitnya yang mana?'] and third['new_from_courier'] == []
    assert first['needs_owner'] == [] and w.phone.adb_taps == [] and not any('inputText' in c for c in w.phone.ui_calls)
    assert len([c for c in w.phone.ui_calls if 'tapOn' in c]) == 1


# spec: ANYM-978
@pytest.mark.parametrize('row,reason', [('Bisa kasih tip ya?', 'money'), ('Pesanan saya batalkan', 'cancel'),
                                        ('Minta kode OTP nya', 'secret'), ('Anjing lu', 'hostile')])
def test_a_courier_row_with_a_trigger_hands_the_chat_over_and_later_replies_do_nothing(world, row, reason):
    w = world('chat')

    async def scenario():
        await goal(w)
        w.phone.rows.append(row)
        read = await act(w, 'read')
        reads = w.reads
        w.phone.ui_calls.clear()
        return read, await reply(w), reads

    read, later, reads = run(w, scenario())
    assert read['state'] == 'succeeded' and read['needs_owner'][0]['row'] == row
    assert read['handover']['reason'] == 'courier_raised_' + reason and read['handover']['courier_row'] == row
    assert row in read['handover']['chat'] and read['handover']['next_action'] == 'tell_the_owner_now'
    assert later['error'] == 'handed_over' and later['handover']['summary']['state'] == 'handed_over'
    assert w.reads == reads and w.phone.ui_calls == [] and w.phone.adb_taps == []


# spec: ANYM-978
def test_a_courier_row_that_appears_while_typing_stops_the_reply_before_the_tap_and_erases_the_draft(world):
    w = world('chat', courier_after_typed='Bisa tambah tip?')

    async def scenario():
        await goal(w)
        return await reply(w)

    result = run(w, scenario())
    assert result['state'] == 'stopped' and result['stop_reason'] == 'handover' and result['sent'] is False
    assert result['handover']['reason'] == 'courier_raised_money' and w.phone.adb_taps == []
    assert {'eraseText': {}} in w.phone.ui_calls and w.phone.typed == ''


# spec: ANYM-978
@pytest.mark.parametrize('action,switches', [('reply', {'pay_after_typed': True}),
                                             ('reply', {'extra': [element('Bayar Rp45.000', (100, 1500, 400, 1600), True)]}),
                                             ('read', {'extra': [element('Bayar Rp45.000', (100, 1500, 400, 1600), True)]}),
                                             ('read', {'extra': [element('Enter PIN', (100, 1500, 400, 1600))]})])
def test_a_payment_or_pin_screen_stops_input_and_hands_the_chat_to_the_owner(world, action, switches):
    w = world('chat', **switches)

    async def scenario():
        await goal(w)
        return await (reply(w) if action == 'reply' else act(w, 'read'))

    result = run(w, scenario())
    assert result['state'] == 'stopped' and result['stop_reason'] == 'payment_or_pin_screen'
    assert result['handover']['reason'] == 'guard_stop' and result['side_effects'] == 'none'
    assert w.phone.adb_taps == [] and w.phone.typed in ('', HELLO)
    if not switches.get('pay_after_typed'):
        assert not any('inputText' in c for c in w.phone.ui_calls)


# spec: ANYM-978
def test_three_failed_turns_in_a_row_hand_over_and_a_success_resets_the_count(world):
    w = world('order', no_chat_entry=True)

    async def scenario():
        await goal(w)
        seen = [(await reply(w))['error'], (await reply(w))['error']]
        w.phone.switches['no_chat_entry'] = False
        ok = await reply(w, ETA)
        w.phone.state = 'order'
        w.phone.switches['no_chat_entry'] = True
        again = [(await reply(w))['state'], (await reply(w))['state']]
        w.phone.state = 'order'
        return seen, ok, again, await reply(w)

    seen, ok, again, last = run(w, scenario())
    assert seen == ['chat_entry_not_found'] * 2 and ok['state'] == 'succeeded' and again == ['failed', 'failed']
    assert last['state'] == 'stopped' and last['handover']['reason'] == 'too_many_failed_turns'


# spec: ANYM-978
def test_an_uncertain_send_is_unknown_hands_over_counts_and_never_replays(world):
    w = world('chat', tap_times_out=True)

    async def scenario():
        await goal(w)
        first = await reply(w)
        taps = len(w.phone.adb_taps)
        same = await act(w, 'reply', message=HELLO, request_id=first['request_id'])
        return first, taps, same, await reply(w, ETA), await act(w, 'summary'), w.store.operation('owner', first['request_id'])

    first, taps, same, later, summary, stored = run(w, scenario())
    assert first['state'] == 'unknown' and first['sent'] is None and first['automatic_retry'] is False
    assert first['handover']['reason'] == 'unknown_send_outcome' and same == stored
    assert len(w.phone.adb_taps) == taps == 1 and later['error'] == 'handed_over' and summary['sent'] == 1


# spec: ANYM-978
def test_the_agent_hands_over_with_a_reason_and_the_owner_path_of_the_first_tool_still_works(world):
    w = world()

    async def scenario():
        await goal(w)
        handed = await act(w, 'handover', reason='The courier writes in a language I cannot read')
        refused = await reply(w)
        preview = await act(w, 'preview', message=HELLO)
        sent = await act(w, 'send', message=HELLO, preview_digest=preview['preview_digest'],
                         owner_instruction=f'Yes, send "{HELLO}" to the courier of {ORDER}')
        return handed, refused, preview, sent

    handed, refused, preview, sent = run(w, scenario())
    assert handed['state'] == 'handed_over' and handed['reason'].startswith('agent: The courier writes')
    assert handed['handover']['next_action'] == 'tell_the_owner_now' and refused['error'] == 'handed_over'
    assert preview['previewed'] is True and sent['sent'] is True and len(w.phone.adb_taps) == 1


# spec: ANYM-978
def test_a_rule_gives_the_first_tool_no_shortcut(world):
    w = world()

    async def scenario():
        await goal(w)
        return await act(w, 'send', message=HELLO, owner_instruction='Yes, send it please')

    with pytest.raises(cc().Refused) as stopped:
        run(w, scenario())
    assert stopped.value.code == 'preview_required' and w.phone.adb_taps == []
