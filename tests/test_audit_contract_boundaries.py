"""Audit boundaries of ANYM-15 that other tests leave open, on a disposable database with synthetic, content-free values.

Covered elsewhere and not repeated: the code allow-list itself (test_send_guard, ANYM-762), a denial that names no
target and the operator's issue_key actor (test_access), the gateway role's insert-only grant and actor (test_access),
the atomic audit rollback (test_access) and the collector's messenger_dispatch actor (test_coverage_gateway).
Here: an unlisted failure detail reaching the real authorization transaction, invalid records, the 100-target bound,
the send worker's own collector attribution, and the operator's `audit-tail` command."""
import asyncio
import json
import os
import sys

import pytest

from messaging_service import __main__ as operator
from messaging_service.envelope import stable_id
from messaging_service.outgoing import OutgoingWorker
from messaging_service.policy import audit, targets
from messaging_service.store import APIError
from test_sending import sender as sender, submit

SENTINEL = 'private_detail_sentinel'


class CodedFailure(Exception):
    """A library error carrying its own code attribute, as a provider or driver error may."""
    code = SENTINEL


def since(s, mark):
    with s.pool.connection() as c:
        return c.execute('SELECT * FROM audit_log WHERE occurred_at>%s ORDER BY occurred_at,id', (mark,)).fetchall()


def now(s):
    with s.pool.connection() as c:
        return c.execute('SELECT clock_timestamp() AS t').fetchone()['t']


def key_id(s, principal):
    with s.pool.connection() as c:
        return c.execute('SELECT id FROM api_keys WHERE principal=%s', (principal,)).fetchone()['id']


# spec: ANYM-15
@pytest.mark.parametrize('failure,outcome,code', [
    (APIError('invalid_query'), 'denied', 'invalid_query'),
    (APIError(SENTINEL), 'denied', 'operation_failed'),
    (ValueError('PRIVATE body text ' + SENTINEL), 'error', 'operation_failed'),
    (CodedFailure('PRIVATE body text'), 'error', 'operation_failed'),
], ids=['listed-code', 'unlisted-code', 'exception-text', 'exception-code'])
def test_a_failed_operation_keeps_only_a_listed_code_and_no_target(seeded, failure, outcome, code):
    s, _, b = seeded
    mark = now(s)
    with pytest.raises(type(failure)):
        with s.authorized(b, 'search') as (_, p):
            targets(p, ['dk80-target-a', 'dk80-target-b'])
            raise failure
    [row] = since(s, mark)
    assert (row['principal'], row['key_id'], row['operation'], row['outcome'], row['error_code'], row['resource_ids']) == (
        'agent-b', key_id(s, 'agent-b'), 'search', outcome, code, [])
    assert SENTINEL not in json.dumps(row, default=str) and 'PRIVATE' not in json.dumps(row, default=str)


# spec: ANYM-15
@pytest.mark.parametrize('operation,outcome', [
    ('made_up_operation', 'ok'), ('resolve_target', 'ok'), ('messages_send', 'ok'),
    ('search', 'partial'), ('search', 'OK'), ('search', None),
], ids=['unknown-operation', 'resolve-target-alias', 'messages-send-alias', 'partial', 'upper-case', 'none'])
def test_an_unlisted_operation_or_outcome_is_refused_before_any_row(seeded, operation, outcome):
    """Direct boundary check of policy.audit: resolve_target and messages_send are audited under their own steps."""
    s, _, _ = seeded
    mark = now(s)
    with s.pool.connection() as c:
        with pytest.raises(ValueError, match='^invalid_audit_record$'):
            audit(c, {'id': 'agent-b', 'key_id': key_id(s, 'agent-b')}, operation, outcome)
    assert since(s, mark) == []


# spec: ANYM-15
def test_a_success_records_at_most_100_unique_targets_in_first_seen_order(seeded):
    """policy.targets inside the real authorization transaction: 130 identifiers out of sorted order, some repeated
    before the 100th."""
    s, _, b = seeded
    order = [f'dk80-{i * 37 % 130:03d}' for i in range(130)]  # every identifier once, not in sorted order
    seen = [x for i, x in enumerate(order) for x in ([x, order[i // 2]] if i % 3 == 0 else [x])]
    expected = order[:100]
    assert len(seen) > 150 and list(dict.fromkeys(seen))[:100] == expected and expected != sorted(expected)
    mark = now(s)
    with s.authorized(b, 'search') as (_, p):
        targets(p, seen)
    [row] = since(s, mark)
    assert (row['outcome'], row['error_code'], row['resource_ids']) == ('ok', None, expected)


# spec: ANYM-15
def test_the_send_worker_audits_its_dispatch_as_the_collector(sender):
    """The real OutgoingWorker with a synthetic provider: the agent's send keeps the gateway actor, the worker's
    dispatch is the collector's and names the send and its chat; a dispatch cancelled by disabled sending names none."""
    s, archive, key, _, state = sender
    mark = now(s)
    sent = []
    async def provider(job):
        sent.append(job['id'])
        return {'provider_message_id': '4242'}
    first = submit(s, key)['send_id']
    asyncio.run(OutgoingWorker(state, provider).once())
    second = s.send_message(key, account='send-fixture', chat_id=stable_id('chat', 'send-fixture', '100'),
                            text='Second synthetic text', request_id='synthetic-request-0002')['send_id']
    archive.set_sending('send-fixture', False)
    asyncio.run(OutgoingWorker(state, provider).once())
    assert sent == [first]
    agent = key_id(s, 'send-agent')
    rows = [(r['operation'], r['actor'], r['principal'], r['key_id'], r['outcome'], r['error_code'], r['resource_ids'])
            for r in since(s, mark) if r['operation'] in ('send_message', 'send_dispatch')]
    chat = stable_id('chat', 'send-fixture', '100')
    assert rows == [
        ('send_message', 'gateway', 'send-agent', agent, 'ok', None, rows[0][6]),
        ('send_dispatch', 'collector', 'send-agent', agent, 'ok', None, [first, chat]),
        ('send_message', 'gateway', 'send-agent', agent, 'ok', None, rows[2][6]),
        ('send_dispatch', 'collector', 'send-agent', agent, 'denied', 'sending_disabled', []),
    ]
    assert second not in rows[3][6]


@pytest.fixture
def cli(seeded, monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL_FILE', os.environ['PROBE_TEST_DSN_FILE'])

    def invoke(*arguments):
        monkeypatch.setattr(sys, 'argv', ['anymsg', 'audit-tail', *arguments])
        try:
            operator.main()
        finally:
            out = capsys.readouterr()
        return json.loads(out.out)
    return invoke


# spec: ANYM-15
def test_the_operator_tails_the_newest_audit_entries_by_principal_and_limit(seeded, cli):
    s, _, _ = seeded
    with s.pool.connection() as c:
        # agent-b's rows come first, so only the principal filter can return them as its newest.
        for principal, count in (('agent-b', 3), ('agent-a', 51)):
            for i in range(count):
                audit(c, {'id': principal, 'key_id': key_id(s, principal), '_audit_targets': [f'{principal}-{i}']},
                      'search', 'ok')
    with s.pool.connection() as c:
        newest = [str(r['id']) for r in c.execute('SELECT id FROM audit_log ORDER BY occurred_at DESC,id DESC LIMIT 50')]
    rows = cli('--principal', 'agent-b', '--limit', '2')
    assert [(r['principal'], r['operation'], r['outcome'], r['resource_ids']) for r in rows] == [
        ('agent-b', 'search', 'ok', ['agent-b-2']), ('agent-b', 'search', 'ok', ['agent-b-1'])]
    rows = cli()
    assert [r['id'] for r in rows] == newest and len(rows) == 50
    assert [r['resource_ids'] for r in rows] == [[f'agent-a-{i}'] for i in range(50, 0, -1)]
    for arguments, refusal in ((('--limit', '101'), 'invalid_limit'), (('--limit', '0'), 'invalid_limit'),
                               (('--principal', 'agent b'), 'invalid_principal')):
        with pytest.raises((APIError, ValueError), match='^' + refusal + '$'):
            cli(*arguments)
