"""W5-7b pin: VoiceService.tool dispatches each voice tool to its own same-file handler, and every tool result and the
saved call record stay byte-equal to the pre-split main (7004d7cf). Each scenario drives one handler through its
success and refusal branches, plus the shared gate (cache, generation, closing, validation); its digest covers the
tool results and the whole call record, with ids and clock values replaced by stable placeholders."""
import hashlib
import inspect
import json
import re

import pytest

from core import FLOW_TOOL_MODELS, TOOL_MODELS, BriefInput, VoiceService
from outbound import ExternalBrief
from test_tasks import BRIEF

MENU = 'Layanan anti-scam XL Smart. Tekan 3 untuk melanjutkan, tekan 4 untuk berhenti.'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
HEX = re.compile(r'\b[0-9a-f]{64}\b')
CLOCK = re.compile(r'\d{4}-\d\d-\d\d(?:[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:[+-]\d\d:?\d\d|Z)?)?')


def scrub(value, ids, root):
    """Ids become their first-seen ordinal; digests (the brief snapshot's covers its clock) and clock values a
    placeholder; the temp directory a fixed name."""
    if isinstance(value, dict):
        return {scrub(k, ids, root): scrub(x, ids, root) for k, x in value.items()}
    if isinstance(value, list):
        return [scrub(x, ids, root) for x in value]
    if isinstance(value, float):
        return '<float>'
    if isinstance(value, str):
        value = value.replace(root, '<tmp>')
        value = HEX.sub('<sha256>', value)
        value = UUID.sub(lambda m: 'id-' + str(ids.setdefault(m.group(), len(ids))), value)
        return CLOCK.sub('<t>', value)
    return value


def digest(tmp_path, store, cid, log):
    ids = {}
    record = scrub({'log': log, 'call': store.get_call(cid)}, ids, str(tmp_path))
    return hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def standard(store, channel=None):
    brief = (store.prepare(ExternalBrief(**BRIEF, channel=channel, account='twilio', destination='+628123456789'), [])
             if channel else store.prepare(BriefInput(**BRIEF), []))
    call = store.start(brief['id']); store.connected(call['id'])
    return call['id']


def flows(store, mode='task'):
    brief = store.prepare(BriefInput(goal='Collect feedback', counterparty='Browser', success_fields=['feedback'],
                                     conversation_mode=mode, dialog_engine='gemini_live_flows'), [])
    call = store.start(brief['id']); store.connected(call['id'])
    return call['id']


def lookup_context(store, run):
    cid = standard(store)
    run(cid, 'lookup_context', {'query': 'TEST-104'}, 'l1')
    run(cid, 'lookup_context', {'query': '  '}, 'l2')
    run(cid, 'lookup_context', {'query': 'absent'}, 'l3')
    run(cid, 'lookup_context', {'query': 'TEST-104'}, 'l1')
    run(cid, 'lookup_context', {}, 'l4')
    return cid


def record_answer(store, run):
    cid = standard(store)
    store.transcript(cid, 'user', 'Привезём завтра с 10 до 12.')
    run(cid, 'record_answer', {'field': 'unknown', 'value': 'x', 'evidence': 'x'}, 'a1')
    run(cid, 'record_answer', {'field': 'delivery_window', 'value': 'завтра 10-12',
                               'evidence': 'Привезём завтра с 10 до 12.'}, 'a2')
    run(cid, 'record_answer', {'field': 'delivery_window', 'value': 'завтра', 'evidence': 'не говорили'}, 'a3')
    return cid


def request_owner_decision(store, run):
    cid = standard(store)
    run(cid, 'request_owner_decision', {'question': 'Accept a fee?', 'options': ['yes', 'x' * 301]}, 'd1')
    run(cid, 'request_owner_decision', {'question': 'Accept a 5 IDR fee?', 'options': ['yes', 'no']}, 'd2')
    store.transcript(cid, 'user', 'Доставка завтра.')
    run(cid, 'record_answer', {'field': 'delivery_window', 'value': 'завтра', 'evidence': 'Доставка завтра.'}, 'd3')
    run(cid, 'finish_task', {'summary': 'Waiting for the owner'}, 'd4')
    return cid


def press_keys_phone(store, run):
    cid = standard(store, 'phone')
    store.transcript(cid, 'user', MENU)
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'p1')
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': 'unheard'}, 'p2')
    run(cid, 'press_keys', {'keys': '4', 'menu_quote': MENU}, 'p3')
    run(cid, 'press_keys', {'keys': '1234', 'menu_quote': MENU}, 'p4')
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'p1')
    return cid


def press_keys_browser(store, run):
    cid = standard(store)
    store.transcript(cid, 'user', MENU)
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'p1')
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'p2')
    for i in range(19):
        run(cid, 'press_keys', {'keys': '12'[i % 2], 'menu_quote': MENU}, 'q' + str(i))
    return cid


def press_keys_channel(store, run):
    brief = store.prepare(ExternalBrief(**BRIEF, channel='telegram', account='telegram', destination='123456789'), [])
    call = store.start(brief['id']); store.connected(call['id'])
    run(call['id'], 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'p1')
    return call['id']


def stale_generation(store, run):
    cid = standard(store, 'phone')
    store.transcript(cid, 'user', MENU)
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'p1', 1)
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'p2', 1)
    run(cid, 'lookup_context', {'query': ''}, 'p3', 1)
    store.claim_delivery(cid, 'p2', 1); store.handover_result(cid, 'p2', 'accepted'); store.activate_generation(cid)
    run(cid, 'lookup_context', {'query': ''}, 'p4', 1)
    run(cid, 'press_keys', {'keys': '4', 'menu_quote': MENU}, 'p5', 2)
    return cid


def consult_supervisor(store, run):
    cid = standard(store)
    for i in range(7):
        run(cid, 'consult_supervisor', {'question': 'Is a 5 IDR fee fine? ' + str(i)}, 's' + str(i))
    return cid


def finish_task(store, run):
    cid = standard(store)
    run(cid, 'finish_task', {'summary': 'Nothing collected'}, 'f1')
    store.transcript(cid, 'user', 'Завтра с 10 до 12.')
    run(cid, 'record_answer', {'field': 'delivery_window', 'value': '10-12', 'evidence': 'Завтра с 10 до 12.'}, 'f2')
    run(cid, 'finish_task', {'summary': 'Window 10-12'}, 'f3')
    run(cid, 'finish_task', {'summary': ''}, 'f4')
    return cid


def end_standard(store, run):
    cid = standard(store)
    store.transcript(cid, 'user', 'Мне неудобно, не могу сейчас говорить.')
    run(cid, 'end_conversation', {'reason': 'needs_owner', 'summary': 's', 'evidence': ''}, 'e1')
    run(cid, 'end_conversation', {'reason': 'refused', 'summary': 's', 'evidence': 'никогда не говорил'}, 'e2')
    run(cid, 'end_conversation', {'reason': 'refused', 'summary': 's',
                                  'evidence': 'Мне неудобно, не могу сейчас говорить.'}, 'e3')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 's', 'evidence': ''}, 'e4')
    run(cid, 'end_conversation', {'reason': 'unavailable', 'summary': 'Busy',
                                  'evidence': 'Мне неудобно, не могу сейчас говорить.'}, 'e5')
    run(cid, 'lookup_context', {'query': ''}, 'e6')
    run(cid, 'end_conversation', {'reason': 'unavailable', 'summary': 'Busy',
                                  'evidence': 'Мне неудобно, не могу сейчас говорить.'}, 'e5')
    return cid


def end_standard_pending(store, run):
    cid = standard(store)
    store.transcript(cid, 'user', 'Завтра с 10 до 12. До свидания!')
    run(cid, 'record_answer', {'field': 'delivery_window', 'value': '10-12', 'evidence': 'Завтра с 10 до 12.'}, 'x1')
    run(cid, 'request_owner_decision', {'question': 'Fee?', 'options': ['yes', 'no']}, 'x2')
    run(cid, 'finish_task', {'summary': 'Window 10-12'}, 'x3')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Window', 'evidence': 'До свидания!'}, 'x4')
    run(cid, 'end_conversation', {'reason': 'needs_owner', 'summary': 'Window', 'evidence': 'До свидания!'}, 'x5')
    return cid


def end_social(store, run):
    brief = store.prepare(BriefInput(**{**BRIEF, 'conversation_mode': 'social'}), [])
    call = store.start(brief['id']); store.connected(call['id']); cid = call['id']
    store.transcript(cid, 'user', 'Ну ладно.')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Chat', 'evidence': 'Ну ладно.'}, 'c1')
    store.transcript(cid, 'user', 'Всё, пока!')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Chat', 'evidence': 'Всё, пока!'}, 'c2')
    return cid


def flows_closing(store, run):
    cid = flows(store)
    store.transcript(cid, 'user', 'Всё хорошо.')
    run(cid, 'record_answer', {'field': 'feedback', 'value': 'хорошо', 'evidence': 'Всё хорошо.'}, 'r1')
    run(cid, 'press_keys', {'keys': '3', 'menu_quote': MENU}, 'r2')
    run(cid, 'propose_closing', {'summary': 'Feedback saved'}, 'r3')
    run(cid, 'propose_closing', {'summary': 'Feedback saved'}, 'r4')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved', 'evidence': 'Всё хорошо.'}, 'r5')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved', 'evidence': 'Всё хорошо.'}, 'r6')
    store.audio_activity(cid, True)
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved', 'evidence': 'Всё хорошо.'}, 'r7')
    store.audio_activity(cid, False)
    store.transcript(cid, 'user', 'Нет, больше ничего. До свидания!')
    run(cid, 'lookup_context', {'query': ''}, 'r8')
    run(cid, 'propose_closing', {'summary': 'Feedback saved'}, 'r9')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved',
                                  'evidence': 'Нет, больше ничего. До свидания!'}, 'r10')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved',
                                  'evidence': 'Нет, больше ничего. До свидания!'}, 'r10')
    store.transcript(cid, 'user', 'Ой, подождите.')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved',
                                  'evidence': 'Нет, больше ничего. До свидания!'}, 'r10')
    return cid


def flows_farewell(store, run):
    cid = flows(store)
    store.transcript(cid, 'user', 'Всё хорошо.')
    run(cid, 'record_answer', {'field': 'feedback', 'value': 'хорошо', 'evidence': 'Всё хорошо.'}, 'w1')
    store.transcript(cid, 'assistant', 'Спасибо, до свидания!')
    store.transcript(cid, 'user', 'До свидания!')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved', 'evidence': 'До свидания!'}, 'w2')
    run(cid, 'lookup_context', {'query': ''}, 'w3')
    return cid


def flows_owner_and_unavailable(store, run):
    cid = flows(store)
    store.transcript(cid, 'user', 'Сейчас занят, перезвоните позже.')
    run(cid, 'end_conversation', {'reason': 'unavailable', 'summary': 'Busy', 'evidence': 'Перезвоните позже.'}, 'u1')
    run(cid, 'end_conversation', {'reason': 'unavailable', 'summary': 'Busy', 'evidence': 'вообще другое'}, 'u2')
    store.transcript(cid, 'user', 'Хорошо, решайте. Пока!')
    run(cid, 'record_answer', {'field': 'feedback', 'value': 'ok', 'evidence': 'Хорошо, решайте.'}, 'u3')
    run(cid, 'request_owner_decision', {'question': 'Fee?', 'options': ['yes', 'no']}, 'u4')
    run(cid, 'consult_supervisor', {'question': 'Fee fine?'}, 'u5')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Owner asked', 'evidence': 'Пока!'}, 'u6')
    run(cid, 'finish_task', {'summary': 'late'}, 'u7')
    return cid


def flows_success(store, run):
    cid = flows(store)
    store.transcript(cid, 'user', 'Всё хорошо.')
    run(cid, 'record_answer', {'field': 'feedback', 'value': 'хорошо', 'evidence': 'Всё хорошо.'}, 'g1')
    run(cid, 'finish_task', {'summary': 'Feedback saved'}, 'g2')
    store.transcript(cid, 'assistant', 'Есть ли что-то ещё, что хотелось бы обсудить?')
    store.transcript(cid, 'user', 'Нет, спасибо, до свидания.')
    run(cid, 'record_answer', {'field': 'feedback', 'value': 'всё хорошо', 'evidence': 'Всё хорошо.'}, 'g3')
    run(cid, 'propose_closing', {'summary': 'Feedback saved'}, 'g4')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved',
                                  'evidence': 'нет спасибо до свидания'}, 'g5')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved',
                                  'evidence': 'нет спасибо до свидания'}, 'g5')
    store.transcript(cid, 'user', 'А нет, подождите, ещё вопрос.')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Feedback saved',
                                  'evidence': 'нет спасибо до свидания'}, 'g5')
    return cid


def flows_needs_owner(store, run):
    brief = store.prepare(BriefInput(goal='Delivery', counterparty='Browser', success_fields=['fee'],
                                     approval_fields=['fee'], dialog_engine='gemini_live_flows'), [])
    cid = store.start(brief['id'])['id']; store.connected(cid)
    store.transcript(cid, 'user', 'Доплата пятьдесят тысяч.')
    run(cid, 'record_answer', {'field': 'fee', 'value': '50000', 'evidence': 'Доплата пятьдесят тысяч.'}, 'n1')
    store.transcript(cid, 'assistant', 'Есть ли что-то ещё, что хотелось бы обсудить?')
    store.transcript(cid, 'user', 'Нет, спасибо.')
    run(cid, 'end_conversation', {'reason': 'completed', 'summary': 'Fee 50000', 'evidence': 'Нет, спасибо.'}, 'n2')
    return cid


def flows_unavailable(store, run):
    cid = flows(store, 'social')
    store.transcript(cid, 'user', 'Сейчас занят, перезвоните позже.')
    run(cid, 'end_conversation', {'reason': 'unavailable', 'summary': 'Busy',
                                  'evidence': 'Сейчас занят, перезвоните позже.'}, 'v1')
    return cid


def stopped(store, run):
    cid = standard(store)
    store.stop(cid)
    run(cid, 'lookup_context', {'query': ''}, 'z1')
    run(cid, 'no_such_tool', {}, 'z2')
    return cid


# Case: (scenario, sha256 of its tool results and call record at 7004d7cf). Digest first: gitleaks reads a hex string
# right after a tool name as an API key.
CASES = {
    'lookup_context': ('bbb468e71e25de7934f39bca2e14072b5747a92f0519ecf145e9da2199054976', lookup_context),
    'record_answer': ('580cff148503a4f7105d6766977e94d05080469ec5382d89f1e1f630bb523b9c', record_answer),
    'request_owner_decision': ('4bbf4e09446faf89bddb0fad55c573860f1dd5ed0effa0612f1f5519a0f12104', request_owner_decision),
    'press_keys_phone': ('55db5b6e2a1c47cfe15d2b47502a1845e21051e008f2d3645cbc7604ee71c999', press_keys_phone),
    'press_keys_browser': ('434f5e659c3340ce808dab4c3756d962a78eeec00e2018aed555265f4e4d01c5', press_keys_browser),
    'press_keys_channel': ('83aea40b976f9cec1e8cd1609cf9f2e66e1a0b0cf8276caee5c2dfa1a3269647', press_keys_channel),
    'stale_generation': ('9674d49d56e6df15db903080bd24a415dc37211947ec2c939dab69df3ffb890e', stale_generation),
    'consult_supervisor': ('bd8352f76ab478c337ee9e5a5589ce341808bea2b1c5cb448a91096357e2d2fa', consult_supervisor),
    'finish_task': ('69ecffc7eea03994ca11c352cde27f7e87a04d6cf8d2b4298313d31621b4aba1', finish_task),
    'end_standard': ('157e048cfeffdfdabd38471113230be35f6b738ff6007fa3df1c51e771ce67e9', end_standard),
    'end_standard_pending': ('545999917f0e5540825041dd03dfc6185fb14c67d4d44c6fc54fa5c337d27211', end_standard_pending),
    'end_social': ('176bd505230dd816e62ed866fca741997941a6e6a35bdeececa09dd9fde9f335', end_social),
    'flows_closing': ('69df25f1ad99e83902793a17a9031823d7ec4bdb09d751867b6e31a603ff47b4', flows_closing),
    'flows_farewell': ('e3adbfefabb6e9e4f9b6562886d311988b29adb31b0f425e50b27f525c73e6b6', flows_farewell),
    'flows_owner_and_unavailable': ('4a50884713b9589406ee54ed0aa40eaa7671ff51ec7b2429af84f25d78ab5527', flows_owner_and_unavailable),
    'flows_success': ('616c0a7a4daca8ae6a65c7d66117aeaa57110e9c5a638ecbcf7767a98048f4ae', flows_success),
    'flows_needs_owner': ('47ff3080f184a490838e7ae9ad97312ff15d0c5dab8bfa7e7298e820b19d4d17', flows_needs_owner),
    'flows_unavailable': ('54a5c44d5d537933dc805815e813562cea8bc65b1ffa0d6d9acbdeb504ed6ba2', flows_unavailable),
    'stopped': ('9af4a83d6cb2b730c6f51afd3edffca01be6aa4967a7cc5ec694f1622b14b3c1', stopped),
}


# spec: ANYM-239
@pytest.mark.parametrize('case', CASES)
def test_each_tool_handler_keeps_its_results_and_call_record(tmp_path, case):
    expected, scenario = CASES[case]
    store, log = VoiceService(tmp_path), []

    def run(cid, name, arguments, tool_call_id, generation=None):
        log.append([name, tool_call_id, store.tool(cid, name, arguments, tool_call_id, generation)])
    cid = scenario(store, run)
    assert digest(tmp_path, store, cid, log) == expected
    store.close()


# spec: ANYM-239
def test_every_voice_tool_has_its_own_handler_and_tool_keeps_one_lock():
    names = set(TOOL_MODELS) | set(FLOW_TOOL_MODELS)
    assert {n for n in names if not callable(getattr(VoiceService, '_tool_' + n, None))} == set()
    source = inspect.getsource(VoiceService.tool)
    assert source.count('with self.lock') == 1 and len(source.splitlines()) <= 60
    for n in names:
        assert 'with self.lock' not in inspect.getsource(getattr(VoiceService, '_tool_' + n))
