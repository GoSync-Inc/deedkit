"""Offline tool fixture for an author-driven Halodoc transcript (ANYM-994).

No provider, HTTP, database, phone or model client. Decisions are supplied by the
agent in the transcript, not made by this fixture. This is not a workflow engine.
"""
import argparse
import json
import time
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'docs/experiments/halodoc-order'


class Fixture:
    def __init__(self):
        self.data = json.loads((FIXTURES / 'world.json').read_text())
        self.catalog = {x['name']: x['inputSchema'] for x in json.loads(
            (ROOT / 'services/gateway/src/agent_gateway/catalog.json').read_text())}
        self.sends = {}
        self.calls = []
        self.subscription = None

    def call(self, name, args):
        jsonschema.validate(args, self.catalog[name])
        self.calls.append({'tool': name, 'arguments': args})
        if name == 'android_agent':
            if args['action'] == 'start':
                return {'state': 'running', 'request_id': args['request_id']}
            return self.data['android_results'][args['request_id']]
        if name == 'messages_get_context':
            return {'message': self.data['messages'][args['message_id']]}
        if name == 'messages_create_subscription':
            self.subscription = args
            return {'subscription_id': 'sub_fixture', 'version': 1}
        if name == 'messages_get_subscription_events':
            assert self.subscription is not None
            return {'items': [{'message_id': mid} for mid, message in self.data['messages'].items()
                              if message['chat'] in self.subscription['chat_ids']
                              and message['sender'] in self.subscription['sender_ids']],
                    'next_after': 4}
        if name == 'messages_send_message':
            request = args['request_id']
            if request in self.sends:
                assert self.sends[request]['arguments'] == args, 'idempotency_conflict'
                return self.sends[request]['receipt']
            # Check transport target/reply like outgoing.target, not order authority.
            message = self.data['messages'][args['reply_to']]
            assert (args['account'], args['chat_id']) == (message['account'], message['chat'])
            receipt = {'send_id': 'send_fixture_' + str(len(self.sends) + 1),
                       'status': 'queued', 'request_id': request}
            self.sends[request] = {'arguments': args, 'receipt': receipt}
            return receipt
        if name == 'messages_get_send_status':
            matches = [s for r, s in self.sends.items() if r == args.get('request_id')
                       or s['receipt']['send_id'] == args.get('send_id')]
            assert len(matches) == 1, 'not_found'
            send = matches[0]
            uncertain = send['arguments']['request_id'] == 'fixture-entrance'
            return {**send['receipt'], 'status': 'unknown' if uncertain else 'sent',
                    'receipt_state': 'unknown' if uncertain else 'pending',
                    'recipient_delivery_confirmed': False}
        raise ValueError('Fixture has no implementation for ' + name)


def exercise(transcript):
    fixture, log, context = Fixture(), [], None
    started = time.monotonic()
    for step in transcript:
        if 'tool' in step:
            result = fixture.call(step['tool'], step['arguments'])
            log.append({**step, 'result': result})
        else:
            if 'context' in step:
                context = step['context']
            log.append(step)
    assert context is not None
    assert context['goal_id'] == fixture.data['owner_context']['goal_id']
    assert context['prescription']['ref'] == 'RX-FIXTURE-DOCTOR-1'
    assert context['prescription']['issuer'] == 'fixture-physician'
    assert context['order_ref'] == 'HD-FIXTURE-1'
    assert context['cancelled'] is True
    sends = list(fixture.sends.values())
    assert len(sends) == 2
    assert all(s['arguments']['chat_id'] == 'chat_bound' for s in sends)
    assert [s['arguments']['reply_to'] for s in sends] == ['msg_eta', 'msg_entrance']
    assert 'reception' in sends[1]['arguments']['text'].lower()
    assert not any(s['arguments'].get('allow_duplicate') for s in sends)
    questions = [s for s in log if s.get('decision') == 'ask_owner']
    assert any(s.get('field') == 'allergies' for s in questions)
    assert any(s.get('decision') == 'exclude' and s.get('message') == 'msg_stranger' for s in log)
    assert any(s.get('decision') == 'exclude' and s.get('message') == 'msg_other_order' for s in log)
    assert any(s.get('decision') == 'hold_unknown' for s in log)
    unknown_at = next(i for i, s in enumerate(log) if s.get('decision') == 'hold_unknown')
    assert not any(s.get('tool') == 'messages_send_message' for s in log[unknown_at:]), 'resend_after_unknown'
    cancelled_at = next(i for i, s in enumerate(log) if s.get('decision') == 'cancel_goal')
    assert not any(s.get('tool') == 'messages_send_message' for s in log[cancelled_at:])
    # Check the agent's factual disclosure, not prose in the skill or goal string.
    disclosures = [s for s in log if s.get('decision') == 'tell_doctor']
    assert disclosures
    for disclosure in disclosures:
        for fact in disclosure['facts']:
            assert fact in fixture.data['owner_context']['clinical_facts'], 'invented_clinical_fact'
        assert all(field in fixture.data['owner_context']['unknown'] for field in disclosure['unknown'])
    return {'kind': 'author-driven synthetic tool exercise', 'checks': 'passed',
            'tool_calls': len(fixture.calls), 'sends': len(sends),
            'owner_questions': len(questions),
            'script_wall_s': round(time.monotonic() - started, 4),
            'model_time_s': None, 'model_turns': None, 'live_verified': False,
            'trace': log}


def negative_controls(transcript):
    """Corrupt actual choices to ensure the exercise detects harmful outcomes."""
    cases = {}
    invented = json.loads(json.dumps(transcript))
    next(s for s in invented if s.get('decision') == 'tell_doctor')['facts'].append(
        {'field': 'allergies', 'value': 'none', 'source': 'agent_guess'})
    cases['invented_clinical_fact'] = invented
    foreign = json.loads(json.dumps(transcript))
    send = next(s for s in foreign if s.get('tool') == 'messages_send_message')
    send['arguments'].update(chat_id='chat_stranger', reply_to='msg_stranger')
    cases['unrelated_sender'] = foreign
    for name, decision in [('resend_after_unknown', 'hold_unknown'), ('send_after_cancel', 'cancel_goal')]:
        replay = json.loads(json.dumps(transcript))
        original = next(s for s in replay if s.get('tool') == 'messages_send_message')
        index = next(i for i, s in enumerate(replay) if s.get('decision') == decision)
        replay.insert(index + 1, original)
        cases[name] = replay
    refused = []
    for name, steps in cases.items():
        try:
            exercise(steps)
        except AssertionError:
            refused.append(name)
        else:
            raise AssertionError('Negative control unexpectedly passed: ' + name)
    return refused


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('transcript', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--negative-controls', action='store_true')
    args = parser.parse_args()
    transcript = json.loads(args.transcript.read_text())
    report = exercise(transcript)
    if args.negative_controls:
        report['rejected_negative_controls'] = negative_controls(transcript)
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'trace'}))


if __name__ == '__main__':
    main()
