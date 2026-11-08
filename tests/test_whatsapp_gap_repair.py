"""ANYM-814: the opt-in phone repair of unverified WhatsApp spans, against a fake native adapter."""
import asyncio
from copy import deepcopy
import json

import pytest

from messaging_service.envelope import stable_id
from messaging_service.whatsapp_collector import gap_span_id
from test_whatsapp import event
from test_whatsapp_unverified_spans import A, NativeCache, collector, history, progress, scan, start_catchup


class NoRequests(NativeCache):
    """Fails on any adapter request beyond the local cache read: the flag-off contract."""
    async def call(self, method, params):
        raise AssertionError(f'adapter request with the flag off: {method}')


class Phone(NativeCache):
    """The adapter's repair rows as gap_repair_status reports them; a test answers the outstanding one."""
    def __init__(self):
        super().__init__()
        self.rows, self.asked, self.paused_until_ms = [], [], 0

    async def call(self, method, params):
        if method == 'gap_repair_status':
            outstanding = sum(row['state'] in ('reserved', 'sending', 'sent') for row in self.rows)
            return {'enabled': True, 'outstanding': outstanding, 'paused_until_ms': self.paused_until_ms,
                    'requests': deepcopy(self.rows)}
        assert method == 'gap_repair'
        self.asked.append(dict(params))
        request = f'REQ{len(self.rows)+1}'
        self.rows.append({'request_id': request, 'chat': params['chat'], 'span_id': params['span_id'],
                          'state': 'sent', 'count': 50, 'created_ms': len(self.rows), 'got': 0, 'oldest_ts': 0,
                          'skipped': 0})
        return {'request_id': request, 'state': 'sent', 'deadline_ms': 0, 'count': 50}

    def answer(self, got, oldest_ts, state='answered', skipped=0):
        row = self.rows[-1]
        assert row['state'] == 'sent'
        row.update(state=state, got=got, oldest_ts=oldest_ts, skipped=skipped)


def ts(mid):
    return event(mid)['timestamp']


@pytest.fixture
def phone(seeded, tmp_path):
    w, s = collector(seeded, tmp_path, Phone())
    w.gap_repair = True
    w.state.provider_observation('whatsmeow')
    return w, s


def recorded_span(w):
    """The ANYM-813 span [5, 20] of chat A: the head, then a cache holding only newer messages, stalled twice."""
    start_catchup(w, A, 5, [20, 21]); scan(w, 2)
    span = progress(w)['unverified_span']
    assert span['from'] and span['to']
    return span


def repair(w):
    return progress(w)['gap_repair']


# spec: ANYM-814
def test_flag_off_makes_no_repair_request_and_no_reservation(seeded, tmp_path):
    w, s = collector(seeded, tmp_path, NoRequests())
    assert w.gap_repair is False
    start_catchup(w, A, 5, [20, 21]); scan(w, 3)
    stored = progress(w)
    assert stored['unverified_span'] and 'gap_repair' not in stored
    status = history(w, s)
    assert status['gap_repair']['enabled'] is False and status['gap_repair']['waiting'] == 1
    assert status['unverified_spans'][0]['repair'] == {'state': 'waiting', 'request_ids': []}


# spec: ANYM-814
def test_flag_defaults_off_in_the_collector_config(tmp_path):
    from messaging_service.whatsapp_collector import configuration
    cfg = {'enabled': True, 'account': 'wa-one', 'expected_wid': '123456789@c.us', 'permit_file': str(tmp_path/'permit'),
           'session_directory': str(tmp_path/'session'), 'events_directory': str(tmp_path/'events'),
           'pages_directory': str(tmp_path/'pages'), 'provider': 'whatsmeow', 'native_executable': '/usr/bin/true'}
    config, permit = tmp_path/'config', tmp_path/'permit'
    permit.write_text(json.dumps({'account': 'wa-one', 'expected_wid': cfg['expected_wid'], 'prior_clients_stopped': True}))
    permit.chmod(0o600)
    config.write_text(json.dumps(cfg)); config.chmod(0o600)
    assert configuration(config)['whatsapp_gap_repair'] is False
    for bad in ('yes', 1):
        config.write_text(json.dumps({**cfg, 'whatsapp_gap_repair': bad}))
        with pytest.raises(ValueError, match='invalid_whatsapp_gap_repair'):
            configuration(config)
    config.write_text(json.dumps({**cfg, 'whatsapp_gap_repair': True}))
    assert configuration(config)['whatsapp_gap_repair'] is True


# spec: ANYM-814
def test_an_interior_hole_with_cached_messages_on_both_sides_stays_open(phone):
    w, s = phone
    span = recorded_span(w)
    # The cache now bridges to the head; messages 6..19 are still missing inside.
    w.provider.keep(A, 5, 20, 21); scan(w, 3)
    assert progress(w)['unverified_span'] == span
    assert repair(w)['state'] == 'outstanding' and 'verified_by' not in repair(w)
    # An unanswered request (an uncorrelated history sync leaves its row `sent`) closes nothing.
    assert len(w.provider.asked) == 1
    assert history(w, s)['gap_repair']['verified'] == 0


# spec: ANYM-814
def test_more_than_fifty_missing_messages_close_after_several_correlated_answers(phone):
    w, s = phone
    span = recorded_span(w)
    first = w.provider.asked[0]
    assert first == {'chat': A, 'span_id': gap_span_id(A, span), 'before_ts': ts(20), 'after_request': ''}
    w.provider.answer(50, ts(15)); scan(w)
    # The cursor moves to the answer's oldest message; the span stays.
    assert w.provider.asked[1]['after_request'] == 'REQ1' and progress(w)['unverified_span'] == span
    w.provider.answer(50, ts(10)); scan(w)
    assert w.provider.asked[2]['after_request'] == 'REQ2'
    w.provider.answer(50, ts(4)); scan(w)
    done = repair(w)
    assert done['state'] == 'verified' and done['verified_by'] == 'phone_history'
    assert done['request_ids'] == ['REQ1', 'REQ2', 'REQ3'] and len(w.provider.asked) == 3
    status = history(w, s)
    assert status['gap_repair']['verified'] == 1 and status['gap_repair']['enabled'] is True
    listed = status['unverified_spans'][0]['repair']
    assert listed == {'state': 'verified', 'request_ids': ['REQ1', 'REQ2', 'REQ3'], 'verified_by': 'phone_history'}


# spec: ANYM-814
def test_a_short_answer_ends_phone_has_no_older(phone):
    w, s = phone
    recorded_span(w)
    w.provider.answer(12, ts(15)); scan(w, 2)
    assert repair(w)['state'] == 'phone_has_no_older' and len(w.provider.asked) == 1
    assert history(w, s)['gap_repair']['phone_has_no_older'] == 1


# spec: ANYM-814
def test_an_uncertain_request_ends_the_span_and_is_never_asked_again(phone):
    w, s = phone
    recorded_span(w)
    w.provider.answer(0, 0, state='uncertain'); scan(w, 2)
    assert repair(w)['state'] == 'request_uncertain' and len(w.provider.asked) == 1
    assert history(w, s)['gap_repair']['request_uncertain'] == 1


# spec: ANYM-814
def test_ten_answers_without_reaching_from_end_budget_exhausted_with_the_remaining_span(phone):
    w, s = phone
    span = recorded_span(w)
    for _ in range(10):
        w.provider.answer(50, ts(15)); scan(w)
    done = repair(w)
    assert done['state'] == 'budget_exhausted' and done['answered'] == 10 and len(w.provider.asked) == 10
    assert done['remaining']['from'] == span['from']
    assert history(w, s)['gap_repair']['budget_exhausted'] == 1


# spec: ANYM-814
def test_a_paused_account_asks_nothing(phone):
    w, _ = phone
    w.provider.paused_until_ms = 10**13
    start_catchup(w, A, 5, [20, 21]); scan(w, 3)
    assert w.provider.asked == []


# spec: ANYM-814
def test_a_verified_flagged_gap_keeps_its_flag(phone):
    w, s = phone
    recorded_span(w)
    with s.pool.connection() as c:
        c.execute("""UPDATE whatsapp_checkpoints SET progress=progress||'{"gap":true}'::jsonb
            WHERE account=%s AND provider_chat=%s""", (w.account, A))
    w.provider.answer(50, ts(4)); scan(w)
    stored = progress(w)
    assert stored['gap'] is True and stored['gap_repair']['state'] == 'verified'
    chat_id = stable_id('chat', w.account, A)
    entry = history(w, s)['gap_chat_ids'][0]
    assert entry['chat_id'] == chat_id and entry['span'] == 'recorded'
    assert entry['repair']['verified_by'] == 'phone_history' and entry['repair']['request_ids'] == ['REQ1']


# spec: ANYM-814
def test_a_refused_repair_step_leaves_the_scan_committed(phone):
    w, s = phone
    from messaging_service.whatsapp_collector import ProviderOperationError

    async def refuse(method, params):
        raise ProviderOperationError('provider_operation_failed')
    start_catchup(w, A, 5, [20, 21])
    w.provider.call = refuse
    scan(w, 3)
    assert progress(w)['unverified_span'] and history(w, s)['unverified_span_chats'] == 1
    asyncio.run(w.repair_gaps())


# spec: ANYM-814
def test_an_answer_with_a_message_capture_did_not_keep_never_verifies(phone):
    w, s = phone
    span = recorded_span(w)
    w.provider.answer(49, ts(4), skipped=1); scan(w, 2)
    done = repair(w)
    assert done['state'] == 'incomplete_answer' and 'verified_by' not in done and len(w.provider.asked) == 1
    assert done['remaining'] == {'from': span['from'], 'to': span['to']}
    assert history(w, s)['gap_repair']['incomplete_answer'] == 1


# spec: ANYM-814
@pytest.mark.parametrize('code', ['history_rate_limited', 'history_anchor_unavailable'])
def test_an_adapter_refusal_ends_only_the_repair_step(phone, code):
    """The real reply mapping: these codes arrive as CommandRejected (error_codes.json), not as an operation error."""
    w, s = phone
    from messaging_service import whatsapp_collector as wc
    reply = {'id': 1, 'error': code}

    class Reader:
        async def readline(self): return (wc.json.dumps(reply)+'\n').encode()
    provider = w.provider
    provider.process = type('P', (), {'stdout': Reader(), 'returncode': None})()
    with pytest.raises(Exception) as raised:
        asyncio.run(wc.NodeProvider.reply(provider, 1))
    rejected = type(raised.value)

    async def refuse(method, params):
        if method == 'gap_repair_status':
            return {'enabled': True, 'outstanding': 0, 'paused_until_ms': 0, 'requests': []}
        raise rejected(code)
    start_catchup(w, A, 5, [20, 21])
    provider.call = refuse
    scan(w, 3)
    assert rejected.__name__ == 'CommandRejected'
    assert progress(w)['unverified_span'] and progress(w)['gap_repair']['state'] == 'waiting'


# spec: ANYM-814
def test_repair_calls_keep_the_native_reconnect_grace():
    from messaging_service.whatsapp_collector import HISTORY_READS
    assert {'gap_repair_status', 'gap_repair'} <= set(HISTORY_READS)
