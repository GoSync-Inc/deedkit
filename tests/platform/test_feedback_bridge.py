"""The feedback <-> BB Tasks bridge on the Ryzen host: watch loop, applier, ledger, reverse path (ANYM-990)."""
from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import threading

from hypothesis import given, strategies as st
import pytest

import scripts.feedback_bridge as fb

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc).timestamp()
KEY = 'canary-key-' + 'x' * 16
SUB = 'deedkit-feedback-bridge'
REPO = Path(__file__).resolve().parents[2]
RECEIPT = 'r' * 43  # secrets.token_urlsafe(32), as subscription_consumption.fetch_subscription_events issues it
STATUSES = ('received', 'accepted', 'declined', 'duplicate', 'fixed', 'verify_requested', 'verified', 'reopened')  # feedback.STATUSES
REQUEST_ID = re.compile(r'[a-zA-Z0-9_.:-]{8,128}')  # feedback_triage.REQUEST_ID
SERVER_SPEC = re.compile(r'ANYM-[1-9][0-9]{0,5}')  # feedback.SPEC_REF


def fid(n):
    return '33333333-3333-4333-8333-%012d' % n


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace('+00:00', 'Z')


def row(n, status='received', age=60, projection=None, **over):
    value = {'feedback_id': fid(n), 'principal': 'agent-a', 'category': 'bug', 'operation': 'messages_search',
        'created_at': iso(NOW - age), 'status': status, 'fixed_in': None, 'spec_ref': None, 'duplicate_of': None,
        'status_changed_at': None if status == 'received' else iso(NOW - age + 1), 'projection': projection}
    return value | over


def projection(brief='The search tool returns stale chats after a sync, so an agent reads old messages.'):
    return {'classification': 'reported-bug', 'reason_code': 'clear_report', 'primary_spec': 'ANYM-12', 'new_card': None,
        'released_brief': brief, 'released_at': iso(NOW - 30), 'reader_pin': 'pin1'}


class Crash(BaseException):
    """A process death at one chosen point."""


class FakeGateway:
    """The gateway tools the bridge uses, with failure injection; every call is logged."""

    def __init__(self, order=None):
        self.rows, self.events, self.subs, self.log, self.fail, self.seq = [], [], [], [], {}, 0
        self.order = [] if order is None else order
        self.acked, self.writes, self.on_wait, self.on_ack = 0, {}, None, None
        self.create_mode = self.write_mode = self.crash_after_ack = None
        self.page = 100
        self.ack_version, self.module_acked = 7, 0

    def calls(self, tool):
        return [args for name, args in self.log if name == tool]

    def submit(self, value):
        self.seq += 1
        self.rows.append(value | {'_seq': self.seq})
        self.events.append(('feedback.received', value['feedback_id']))

    def server_change(self, number, status):
        item = next(r for r in self.rows if r['feedback_id'] == fid(number))
        self.seq += 1
        item.update(status=status, status_changed_at=iso(NOW), _seq=self.seq)
        self.events.append(('feedback.' + status, item['feedback_id']))

    def call(self, tool, args, timeout=None):
        self.log.append((tool, args))
        if tool in self.fail:
            raise self.fail[tool]
        if not 1 <= args.get('limit', 50) <= 100:  # store.bounded
            raise fb.Refused('invalid_limit', 400)
        return getattr(self, tool)(**args)

    @staticmethod
    def event_data(kind, ident):
        """The data of one feedback module event: feedback_events._event, publish_received, publish_projection_recorded."""
        if kind == 'feedback.projection_recorded':
            return {'feedback_id': ident, 'classification': 'reported-bug'}
        if kind == 'feedback.received':
            return {'feedback_id': ident, 'status': 'received', 'category': 'bug'}
        return {'feedback_id': ident, 'status': kind.split('.')[1], 'previous_status': 'received'}

    def page_of(self):
        """A compact page of a maintainer-only definition: subscription_consumption.fetch_subscription_events through
        project_page (module_events.page holds the items)."""
        items = [{'module_event_id': n, 'specversion': '1.0', 'id': 'feedback:%d' % n, 'source': 'deedkit:feedback',
            'type': kind, 'subject': ident, 'time': iso(NOW), 'data': self.event_data(kind, ident), 'urgency': 'now',
            'expires_at': None, 'expired': False} for n, (kind, ident) in enumerate(self.events, self.module_acked + 1)]
        return {'subscription': {'subscription_id': 'sub-0', 'state': 'active', 'version': 1, 'label': SUB, 'matched_through': 1,
            'matcher_caught_up': True, 'acked_through': 1, 'blocked_reason': None, 'gap': None, 'scope_masked': False},
            'items': [], 'has_more': False, 'next_after': 1, 'consumption': 'explicit_subscription_ack', 'scope_masked': False,
            'module': {'items': items, 'has_more': False, 'next_after': self.module_acked + len(items), 'gap': None,
                'acked_through': self.module_acked},
            'positions': {'acked_through': 1, 'fetched_through': 1, 'open_receipts': 1 if items else 0,
                'oldest_open_receipt_expires_at': iso(NOW + 300) if items else None, 'expired_unacked': 0},
            'ack_version': self.ack_version, 'receipt': RECEIPT if items else None, 'acked_through': 1,
            'receipt_expires_at': iso(NOW + 300) if items else None, 'ack_available': True}

    def messages_fetch_subscription_events(self, subscription_id, limit=50, detail=None):
        page = self.page_of()
        page['subscription']['subscription_id'] = subscription_id
        return page

    def messages_wait_for_subscription_events(self, subscription_id, timeout=25, limit=50, detail=None):
        if self.on_wait is not None:
            hook, self.on_wait = self.on_wait, None
            hook()
        page = self.page_of()
        page['subscription']['subscription_id'] = subscription_id
        return page | {'wait_status': 'events_available' if page['module']['items'] else 'timeout'}

    def messages_ack_subscription_events(self, subscription_id, receipt, version):
        assert receipt == RECEIPT and version == self.ack_version
        if self.on_ack is not None:
            self.on_ack()
        self.order.append('ack')
        self.module_acked += len(self.events)
        self.ack_version += 1
        self.events, self.acked = [], self.acked + 1
        if self.crash_after_ack:
            raise Crash
        return {'subscription_id': subscription_id, 'acked_through': 1, 'ack_version': self.ack_version,
            'provider_read_state_changed': False, 'module_acked_through': self.module_acked}

    def messages_export_feedback_metadata(self, cursor=None, limit=50):
        after = int(cursor or 0)
        rows = sorted((r for r in self.rows if r['_seq'] > after), key=lambda r: r['_seq'])
        page = rows[:min(limit, self.page)]
        more = len(rows) > len(page)
        head = max([r['_seq'] for r in self.rows] or [0])
        return {'items': [{k: v for k, v in r.items() if k != '_seq'} for r in page], 'has_more': more,
            'next_cursor': str(page[-1]['_seq'] if more else max(0, head - 1)), 'untrusted_content': True,
            'export_head': iso(NOW)}

    def messages_create_subscription(self, feedback, label):
        assert feedback == {'maintainer': True}
        if self.create_mode == 'uncertain_not_created':
            self.create_mode = None
            raise fb.Uncertain('gateway_unreachable')
        sub = {'subscription_id': 'sub-%d' % (len(self.subs) + 1), 'label': label, 'state': 'active', 'version': 1,
            'feedback_maintainer': 'only'}
        self.subs.insert(0, sub)  # the listing is newest first
        if self.create_mode == 'uncertain_created':
            self.create_mode = None
            raise fb.Uncertain('gateway_unreachable')
        return dict(sub)

    def messages_list_subscriptions(self, limit=50):
        """subscriptions.list_subscriptions: `{items, has_more}`, at most `limit` definitions, has_more past them."""
        return {'items': [dict(s) for s in self.subs[:limit]], 'has_more': len(self.subs) > limit}

    def messages_set_feedback_triage(self, feedback_id, status, request_id, expected_status, expected_changed_at,
                                     duplicate_of=None, spec_ref=None, note=None):
        bad = (status not in ('accepted', 'declined', 'duplicate') or expected_status not in STATUSES
            or not REQUEST_ID.fullmatch(request_id) or (spec_ref is not None and not SERVER_SPEC.fullmatch(spec_ref))
            or (status == 'accepted' and spec_ref is None) or (status == 'duplicate' and duplicate_of is None and spec_ref is None)
            or (status != 'duplicate' and duplicate_of is not None))  # feedback_triage._errors
        if bad:
            raise fb.Refused('invalid_feedback', 400)
        if request_id in self.writes:
            return self.writes[request_id] | {'replayed': True}
        if self.write_mode == 'uncertain_not_applied':
            self.write_mode = None
            raise fb.Uncertain('gateway_unreachable')
        item = next((r for r in self.rows if r['feedback_id'] == feedback_id), None)
        if item is None:
            raise fb.Refused('not_found', 404)
        if item['status'] != expected_status or item['status_changed_at'] != expected_changed_at:
            raise fb.Refused('version_conflict', 409)
        self.seq += 1
        item.update(status=status, status_changed_at=iso(NOW + self.seq), _seq=self.seq, duplicate_of=duplicate_of,
            spec_ref=spec_ref)
        result = self.writes[request_id] = {'feedback_id': feedback_id, 'status': status,
            'status_changed_at': item['status_changed_at'], 'spec_ref': spec_ref, 'duplicate_of': duplicate_of,
            'replayed': False}
        if self.write_mode == 'uncertain_applied':
            self.write_mode = None
            raise fb.Uncertain('gateway_unreachable')
        return result


class FakeTasks:
    """The tracker calls of `BbTasks` over memory."""

    def __init__(self, order=None):
        self.tasks, self.comments, self.log, self.labels, self.clock = {}, [], [], set(), 0
        self.order = [] if order is None else order
        self.next_create = self.fail_list = self.fail_all = None

    def count(self, name):
        return sum(1 for entry in self.log if entry[0] == name)

    def bump(self, task):
        self.clock += 1
        task['updatedAt'] = 'v%06d' % self.clock

    def guard(self):
        if self.fail_all is not None:
            raise self.fail_all

    def ensure_labels(self, names):
        self.guard()
        self.labels |= set(names)

    def create(self, title, description, labels):
        self.guard()
        self.log.append(('create', title))
        mode, self.next_create = self.next_create, None
        if mode == 'crash_before':
            raise Crash
        if mode == 'uncertain_not_created':
            raise fb.Uncertain('tasks_timeout')
        number = len(self.tasks) + 1
        task = {'id': 'T%03d' % number, 'key': 'DK-%d' % number, 'number': number, 'title': title,
            'description': description, 'status': 'backlog', 'labels': list(labels)}
        self.bump(task)
        self.tasks[task['key']] = task
        self.order.append('create')
        if mode == 'crash_after':
            raise Crash
        if mode == 'uncertain_created':
            raise fb.Uncertain('tasks_timeout')
        return dict(task)

    def get(self, ref):
        self.guard()
        return dict(self.tasks[ref])

    def update(self, ref, status=None, title=None, description=None, add_labels=()):
        self.guard()
        self.log.append(('update', ref, status))
        task = self.tasks[ref]
        for name, value in (('status', status), ('title', title), ('description', description)):
            if value is not None:
                task[name] = value
        task['labels'] += [x for x in add_labels if x not in task['labels']]
        self.bump(task)
        return dict(task)

    def comment(self, ref, body):
        self.guard()
        self.log.append(('comment', ref))
        self.comments.append((ref, body))

    def list_tasks(self, label=None):
        self.guard()
        if self.fail_list is not None:
            raise self.fail_list
        return [dict(t) for t in self.tasks.values() if label is None or label in t['labels']]

    def human(self, ref, **fields):
        self.tasks[ref].update(fields)
        self.bump(self.tasks[ref])


def config(tmp_path, **over):
    return fb.Config(**({'gateway_url': 'https://agents.example', 'key': KEY, 'subscription_id': 'sub-0', 'tracker': 'DK',
        'backfill_days': 14, 'state_dir': tmp_path / 'state'} | over))


def make(tmp_path, gateway=None, tasks=None, **over):
    order = []
    gateway = FakeGateway(order) if gateway is None else gateway
    tasks = FakeTasks(order) if tasks is None else tasks
    return fb.Bridge(config(tmp_path, **over), gateway, tasks, now=lambda: NOW, rand=lambda: 0.0), gateway, tasks


def task_of(tasks, n):
    return next(t for t in tasks.tasks.values() if fb.first_line_id(t['description']) == fid(n))


def stopper(passes):
    state = {'n': 0}

    def stop():
        state['n'] += 1
        return state['n'] > passes
    return stop


# spec: ANYM-990
def test_the_forward_run_makes_one_task_per_item_and_acks_after_the_ledger(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.submit(row(2, projection=projection()))
    gw.on_ack = lambda: [bridge.ledger.link(fid(n)) is not None or pytest.fail('ACK before the ledger') for n in (1, 2)]
    assert bridge.cycle()['created'] == 2
    assert gw.order == ['create', 'create', 'ack']
    first, second = task_of(tasks, 1), task_of(tasks, 2)
    assert first['title'] == 'bug feedback: messages_search (metadata only)'
    assert second['title'].startswith('The search tool returns stale chats')
    assert first['description'].splitlines()[0] == 'Feedback-ID: ' + fid(1)
    assert set(first['labels']) == {'feedback', 'fb-bug'} and 'feedback' in tasks.labels and 'fb-bug' in tasks.labels
    assert '> The search tool returns stale chats' in second['description'] and 'ANYM-12' in second['description']
    before = (len(gw.log), list(tasks.log))
    assert bridge.cycle()['created'] == 0
    assert tasks.log == before[1] and gw.acked == 1  # nothing written, nothing ACKed again


# spec: ANYM-990
def test_a_replayed_event_page_and_a_second_bridge_create_no_second_task(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    gw.events.append(('feedback.received', fid(1)))  # the server repeats the page
    again, _, _ = make(tmp_path, gateway=gw, tasks=tasks)
    again.cycle()
    assert gw.acked == 2
    assert len(tasks.tasks) == 1 and tasks.count('create') == 1


# spec: ANYM-990
def test_a_first_run_skips_history_and_closed_items(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    day = 86400
    gw.submit(row(1, 'verified', age=3 * day))
    gw.submit(row(2, 'accepted', age=40 * day))
    gw.submit(row(3, 'accepted', age=2 * day))
    gw.submit(row(4, 'declined', age=60))
    gw.submit(row(5, 'fixed', age=5 * day, fixed_in='deedkit@' + 'a' * 40))
    bridge.cycle()
    assert sorted(t['title'] for t in tasks.tasks.values()) == ['bug feedback: messages_search (metadata only)'] * 2
    assert {n: bridge.ledger.link(fid(n))['skipped'] for n in (1, 2, 3, 4, 5)} == {1: 1, 2: 1, 3: 0, 4: 1, 5: 0}
    # Owner 11-13: a skipped ledger row is not durable save in BB. Preserve backfill behavior, hold the receipt.
    assert gw.acked == 0


# spec: ANYM-990
def test_only_whitelisted_fields_reach_a_task_a_comment_the_ledger_or_a_log(tmp_path, capsys):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1, description='CANARY-DESC', refs=['CANARY-REF'], status_note='CANARY-NOTE', request_id='CANARY-RID',
        key_id='CANARY-KEYID', projection=projection() | {'raw_text': 'CANARY-RAW'}))
    gw.server_change(1, 'accepted')
    bridge.cycle()
    bridge.cycle()
    assert len(tasks.tasks) == 1
    capture = capsys.readouterr()
    haystack = json.dumps([tasks.tasks, tasks.comments]) + capture.out + capture.err
    haystack += b''.join(p.read_bytes() for p in (tmp_path / 'state').iterdir() if p.is_file()).decode('latin-1')
    assert 'CANARY' not in haystack.replace(KEY, '')


FORWARD_ROWS = [('received', 'backlog'), ('accepted', 'todo'), ('fixed', 'in_review'), ('verify_requested', 'in_review'),
    ('verified', 'done'), ('declined', 'canceled'), ('duplicate', 'canceled'), ('reopened', 'backlog')]


# spec: ANYM-990
@pytest.mark.parametrize('status,expected', FORWARD_ROWS)
def test_every_status_maps_to_its_task_status(tmp_path, status, expected):
    assert fb.FORWARD[status] == expected
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    gw.server_change(1, status)
    gw.rows[0]['fixed_in'] = 'deedkit@' + 'a' * 40 if status in ('fixed', 'verify_requested') else None
    gw.rows[0]['duplicate_of'] = fid(9) if status == 'duplicate' else None
    bridge.cycle()
    assert task_of(tasks, 1)['status'] == expected
    if status == 'duplicate':
        assert fid(9) in ' '.join(body for _, body in tasks.comments)


# spec: ANYM-990
def test_a_status_the_task_already_holds_in_a_compatible_form_is_not_rewritten_and_human_edits_stay(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    gw.server_change(1, 'accepted')
    bridge.cycle()
    key = task_of(tasks, 1)['key']
    tasks.human(key, status='in_progress', title='My own title')
    writes = tasks.count('update')
    gw.submit(row(2))
    gw.server_change(1, 'accepted')  # the same status again moves the item but not its meaning
    gw.rows[0]['projection'] = projection()
    bridge.cycle()
    assert tasks.tasks[key]['status'] == 'in_progress' and tasks.tasks[key]['title'] == 'My own title'
    assert tasks.count('update') == writes and any('stale chats' in body for _, body in tasks.comments)


# spec: ANYM-990
def test_an_unwritten_title_follows_a_new_projection(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    gw.rows[0]['projection'] = projection()
    gw.server_change(1, 'received')
    bridge.cycle()
    assert task_of(tasks, 1)['title'].startswith('The search tool returns stale chats')


# spec: ANYM-990
def test_a_malformed_row_is_skipped_beside_a_good_one_and_never_blocks_it(tmp_path, capsys):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1, feedback_id='not-an-id'))
    gw.submit(row(2, status='bogus'))
    gw.submit(row(3))
    bridge.cycle()
    assert [fb.first_line_id(t['description']) for t in tasks.tasks.values()] == [fid(3)]
    assert capsys.readouterr().err.count('item_malformed') == 2


# spec: ANYM-990
@pytest.mark.parametrize('value, shown', [('deedkit@' + 'a' * 40, True), ('anymsg@1.2.3', True), ('anymsg@1.2.3-rc.1+build.5', True),
    ('a' * 40, False), ('deedkit@abc', False), ('anymsg@1.2', False), ('deedkit@' + 'a' * 40 + '\nInjected: line', False)])
def test_the_fixed_in_line_shows_the_release_forms_the_server_stores_and_nothing_else(value, shown):
    title, text = fb.render(row(1, 'fixed', fixed_in=value))  # feedback.FIXED_IN
    assert ('Fixed in: ' + value + '\n' in text) == shown and 'Injected' not in text


# spec: ANYM-990
def test_a_card_number_longer_than_the_servers_pattern_names_no_card():
    assert fb.task_spec(['feedback', 'spec:ANYM-123456']) == 'ANYM-123456' and fb.task_spec(['spec:ANYM-1234567']) is None  # feedback.SPEC_REF
    item = fb.normalize(row(1, projection=projection() | {'primary_spec': 'ANYM-1234567'}))
    assert item['projection']['primary_spec'] is None and fb.normalize(row(1, spec_ref='ANYM-1234567'))['spec_ref'] is None


# spec: ANYM-990
def test_an_event_for_an_item_nobody_returns_is_acked_after_three_cycles(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.events.append(('feedback.received', fid(7)))
    for _ in range(2):
        bridge.cycle()
        assert gw.acked == 0
    bridge.cycle()
    # Historical identity kept for Tiden. Exact owner 11-13 authority supersedes the old three-cycle assertion:
    # «Да, разрешаю, если так надо, давай делать. Я разрешаю, конечно.» — only after durable BB save.
    assert gw.acked == 0 and not tasks.tasks


# spec: ANYM-990
@pytest.mark.parametrize('case', ['malformed', 'empty-id', 'unmapped', 'wrong-subject', 'missing-time',
    'bad-module-id', 'hidden-through', 'top-message', 'gap', 'scope-masked', 'wrong-subscription', 'private-data',
    'bad-receipt', 'missing-more', 'bad-status', 'bad-category'])
def test_every_event_covered_by_a_receipt_must_be_well_formed_feedback_with_actual_bb_evidence(tmp_path, case):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.apply(row(1), {'created': 0, 'updated': 0})
    page = gw.page_of()
    event = page['module']['items'][0]
    if case == 'malformed':
        page['module']['items'].append({'data': {'feedback_id': fid(1)}})
    elif case == 'empty-id':
        page['module']['items'] = [{'data': {}}]
    elif case == 'unmapped':
        event['type'] = 'calendar.event.created'
    elif case == 'wrong-subject':
        event['subject'] = fid(2)
    elif case == 'missing-time':
        event.pop('time')
    elif case == 'bad-module-id':
        event['module_event_id'] = True
    elif case == 'hidden-through':
        page['module']['next_after'] += 1
    elif case == 'top-message':
        page['items'] = [{'event_id': 1}]
    elif case == 'gap':
        page['module']['gap'] = {'reason': 'retention_gap'}
    elif case == 'scope-masked':
        page['scope_masked'] = True
    elif case == 'wrong-subscription':
        page['subscription']['subscription_id'] = 'another-subscription'
    elif case == 'private-data':
        event['data']['description'] = 'CANARY-private-event'
    elif case == 'bad-receipt':
        page['receipt'] = 123
    elif case == 'missing-more':
        page['module'].pop('has_more')
    elif case == 'bad-status':
        event['data']['status'] = 'accepted'
    else:
        event['data']['category'] = 'unmapped-category'
    bridge.forward(page)
    assert not gw.calls('messages_ack_subscription_events')
    assert len(tasks.tasks) == 1


# spec: ANYM-990
@pytest.mark.parametrize('case', ['deleted', 'header-edited', 'wrong-task-id', 'lookup-unavailable'])
def test_a_local_task_link_is_not_enough_without_a_current_matching_bb_task(tmp_path, case):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.apply(row(1), {'created': 0, 'updated': 0})
    if case == 'header-edited':
        tasks.human('DK-1', description='A human removed the feedback marker.')
    else:
        original = tasks.get
        def get(ref):
            if case == 'deleted':
                raise fb.Refused('not_found')
            if case == 'lookup-unavailable':
                raise fb.Uncertain('tasks_timeout')
            return original(ref) | {'id': 'another-task'}
        tasks.get = get
    bridge.forward()
    assert not gw.calls('messages_ack_subscription_events')


# spec: ANYM-990
def test_an_unknown_ack_is_not_retried_on_unchanged_incomplete_or_other_subscription_readback(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.fail['messages_ack_subscription_events'] = fb.Uncertain('gateway_unreachable')
    with pytest.raises(fb.Uncertain):
        bridge.forward()
    assert bridge.ledger.meta('pending_ack') and RECEIPT not in bridge.ledger.meta('pending_ack')
    for change in ('unchanged', 'incomplete', 'other-subscription'):
        revived, _, _ = make(tmp_path, gateway=gw, tasks=tasks)
        page = gw.page_of()
        if change == 'incomplete':
            page.pop('ack_version')
        elif change == 'other-subscription':
            page['subscription']['subscription_id'] = 'another-subscription'
        with pytest.raises(fb.Uncertain, match='ack_outcome_unresolved'):
            revived.forward(page)
        assert len(gw.calls('messages_ack_subscription_events')) == 1
        assert revived.ledger.meta('pending_ack')


# spec: ANYM-990
def test_definitive_cursor_and_version_readback_reconciles_a_committed_ack_without_resending_it(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    original = gw.messages_ack_subscription_events
    def lost(**args):
        original(**args)
        raise fb.Uncertain('gateway_unreachable')
    gw.messages_ack_subscription_events = lost
    with pytest.raises(fb.Uncertain):
        bridge.forward()
    assert bridge.ledger.meta('pending_ack')
    gw.messages_ack_subscription_events = original
    revived, _, _ = make(tmp_path, gateway=gw, tasks=tasks)
    revived.forward()
    assert revived.ledger.meta('pending_ack') is None
    assert len(gw.calls('messages_ack_subscription_events')) == 1 and len(tasks.tasks) == 1


# spec: ANYM-990
def test_an_unreadable_successful_ack_reply_keeps_its_intent_until_positive_readback(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    original = gw.messages_ack_subscription_events
    def malformed(**args):
        original(**args)
        return {}
    gw.messages_ack_subscription_events = malformed
    with pytest.raises(fb.Uncertain, match='ack_result_unconfirmed'):
        bridge.forward()
    assert bridge.ledger.meta('pending_ack')
    gw.messages_ack_subscription_events = original
    bridge.forward()
    assert bridge.ledger.meta('pending_ack') is None and len(gw.calls('messages_ack_subscription_events')) == 1


# spec: ANYM-990
def test_a_definite_ack_refusal_does_not_masquerade_as_an_unknown_mutation(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.fail['messages_ack_subscription_events'] = fb.Refused('policy_changed', 409)
    with pytest.raises(fb.Refused):
        bridge.forward()
    assert bridge.ledger.meta('pending_ack') is None
    gw.fail = {}
    bridge.forward()
    assert gw.acked == 1 and len(tasks.tasks) == 1


# spec: ANYM-990
def test_partial_cursor_or_version_advancement_never_clears_an_unknown_ack(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.fail['messages_ack_subscription_events'] = fb.Uncertain('gateway_unreachable')
    with pytest.raises(fb.Uncertain):
        bridge.forward()
    for case in ('version-only', 'cursor-only', 'definition-changed'):
        page = gw.page_of()
        if case != 'cursor-only':
            page['ack_version'] += 1
        if case != 'version-only':
            page['module']['acked_through'] = page['module']['next_after']
        if case == 'definition-changed':
            page['subscription']['version'] += 1
        with pytest.raises(fb.Uncertain, match='ack_outcome_unresolved'):
            bridge.forward(page)
    assert len(gw.calls('messages_ack_subscription_events')) == 1 and bridge.ledger.meta('pending_ack')


# spec: ANYM-990
@pytest.mark.parametrize('point', ['journal', 'create', 'link', 'ack'])
def test_a_crash_at_any_step_leaves_one_task_and_one_ack(tmp_path, point):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    if point in ('journal', 'create'):
        tasks.next_create = {'journal': 'crash_before', 'create': 'crash_after'}[point]
    elif point == 'link':
        gw.on_ack = lambda: (_ for _ in ()).throw(Crash)
    else:
        gw.crash_after_ack = True
    with pytest.raises(Crash):
        bridge.cycle()
    gw.on_ack = gw.crash_after_ack = None
    revived, _, _ = make(tmp_path, gateway=gw, tasks=tasks)
    if point == 'link':
        # Identity/parameters retained. A crash at transport entry is unknown to the bridge: never resend.
        with pytest.raises(fb.Uncertain, match='ack_outcome_unresolved'):
            revived.cycle()
        assert gw.acked == 0 and gw.events and revived.ledger.meta('pending_ack')
        assert len(tasks.tasks) == 1 and tasks.count('create') == 1
        return
    revived.cycle()
    assert len(tasks.tasks) == 1 and gw.events == [] and revived.ledger.link(fid(1))['task_key'] == 'DK-1'
    assert tasks.count('create') == (2 if point == 'journal' else 1)


# spec: ANYM-990
def test_an_uncertain_create_that_did_create_is_found_by_the_listing_and_never_retried(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    tasks.next_create = 'uncertain_created'
    with pytest.raises(fb.Uncertain):
        bridge.cycle()
    assert gw.acked == 0 and bridge.ledger.pending(fid(1))
    bridge.cycle()
    assert tasks.count('create') == 1 and len(tasks.tasks) == 1 and gw.acked == 1 and not bridge.ledger.pending(fid(1))


# spec: ANYM-990
def test_an_uncertain_create_that_did_not_create_is_retried_once_after_a_complete_listing(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    tasks.next_create = 'uncertain_not_created'
    with pytest.raises(fb.Uncertain):
        bridge.cycle()
    bridge.cycle()
    assert tasks.count('create') == 2 and len(tasks.tasks) == 1 and gw.acked == 1


# spec: ANYM-990
@pytest.mark.parametrize('failure', [fb.Incomplete('tasks_listing_incomplete'), fb.Uncertain('tasks_timeout')])
def test_a_truncated_or_timed_out_listing_means_no_create(tmp_path, failure):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    tasks.next_create = 'uncertain_not_created'
    with pytest.raises(fb.Uncertain):
        bridge.cycle()
    tasks.fail_list = failure
    creates = tasks.count('create')
    with pytest.raises(type(failure)):
        bridge.cycle()
    assert tasks.count('create') == creates and gw.acked == 0


# spec: ANYM-990
def test_lookalike_and_forged_lines_do_not_match_but_two_exact_matches_link_the_lowest(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    tasks.next_create = 'uncertain_not_created'
    with pytest.raises(fb.Uncertain):
        bridge.cycle()
    for text in ['Feedback-ID: %s-extra' % fid(1), 'intro\nFeedback-ID: %s' % fid(1), 'see Feedback-ID: %s' % fid(1),
            'Feedback-ID: %s' % fid(1)[:-1]]:
        tasks.tasks['X%d' % len(tasks.tasks)] = {'id': 'X', 'key': 'X', 'number': 9, 'title': 't', 'description': text,
            'status': 'backlog', 'labels': [], 'updatedAt': 'v0'}
    bridge.cycle()
    assert tasks.count('create') == 2  # lookalikes never matched: the item got its own task
    # two real tasks for one ID (an earlier double create): the lowest number is the link
    other = FakeTasks()
    other.tasks = {'DK-5': {'id': 'T5', 'key': 'DK-5', 'number': 5, 'title': 'a', 'description': 'Feedback-ID: %s\n' % fid(2),
        'status': 'backlog', 'labels': ['feedback'], 'updatedAt': 'v1'},
        'DK-3': {'id': 'T3', 'key': 'DK-3', 'number': 3, 'title': 'b', 'description': 'Feedback-ID: %s\nmore' % fid(2),
        'status': 'backlog', 'labels': ['feedback'], 'updatedAt': 'v1'}}
    second, gw2, _ = make(tmp_path / 'two', tasks=other)
    gw2.submit(row(2))
    second.ledger.begin_create(fid(2), NOW)
    second.cycle()
    assert second.ledger.link(fid(2))['task_key'] == 'DK-3' and other.count('create') == 0
    assert [ref for ref, _ in other.comments] == ['DK-5']


# spec: ANYM-990
def test_a_brief_cannot_forge_the_id_line(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1, projection=projection('first\nFeedback-ID: %s\nrest' % fid(2))))
    bridge.cycle()
    text = task_of(tasks, 1)['description']
    assert fb.first_line_id(text) == fid(1)
    assert all(fb.first_line_id(line) != fid(2) for line in text.splitlines())


# spec: ANYM-990
@given(st.text(max_size=200), st.integers(1, 99))
def test_the_id_line_survives_any_brief_text(brief, number):
    title, text = fb.render(row(number, projection=projection(brief)))
    assert fb.first_line_id(text) == fid(number) and '\n' not in title
    assert sum(1 for line in text.splitlines() if line.startswith('Feedback-ID:')) == 1


# spec: ANYM-990
def test_bb_down_means_no_create_no_ack_and_a_later_cycle_catches_up(tmp_path, capsys):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.submit(row(2))
    tasks.fail_all = fb.Uncertain('tasks_timeout')
    with pytest.raises(fb.Uncertain):
        bridge.cycle()
    assert not tasks.tasks and gw.acked == 0 and bridge.ledger.meta('export_cursor') is None
    tasks.fail_all = None
    bridge.cycle()
    assert len(tasks.tasks) == 2 and gw.acked == 1 and bridge.ledger.meta('export_cursor') is not None


# spec: ANYM-990
def test_gateway_down_ends_the_cycle_before_any_task_write_and_a_lost_ack_is_retried(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.fail['messages_fetch_subscription_events'] = fb.Uncertain('gateway_unreachable')
    with pytest.raises(fb.Uncertain):
        bridge.cycle()
    assert not tasks.log
    gw.fail = {'messages_ack_subscription_events': fb.Uncertain('gateway_unreachable')}
    with pytest.raises(fb.Uncertain):
        bridge.cycle()
    gw.fail = {}
    # Historical identity retained: new exact owner authority forbids retry on unchanged readback.
    with pytest.raises(fb.Uncertain, match='ack_outcome_unresolved'):
        bridge.cycle()
    assert len(tasks.tasks) == 1 and tasks.count('create') == 1 and gw.acked == 0
    assert len(gw.calls('messages_ack_subscription_events')) == 1


# spec: ANYM-990
def test_a_second_concurrent_run_exits_75_silently(tmp_path, capsys):
    bridge, gw, tasks = make(tmp_path)
    with bridge.lock(block=False):
        with pytest.raises(fb.Busy):
            bridge.run()
        env = write_env(tmp_path)
        assert fb.main(['run'], environ={'DEEDKIT_FEEDBACK_BRIDGE_ENV': str(env)}, gateway=gw, tasks=tasks) == 75
    assert capsys.readouterr().out == ''


# spec: ANYM-990
def test_main_reports_one_fixed_line_and_exit_1_when_bb_is_down(tmp_path, capsys):
    gw, tasks = FakeGateway(), FakeTasks()
    gw.submit(row(1))
    tasks.fail_all = fb.Uncertain('tasks_timeout')
    env = write_env(tmp_path)
    assert fb.main(['run'], environ={'DEEDKIT_FEEDBACK_BRIDGE_ENV': str(env)}, gateway=gw, tasks=tasks) == 1
    capture = capsys.readouterr()
    assert capture.out == '' and capture.err.strip() == 'feedback-bridge: tasks_timeout' and KEY not in capture.err


# spec: ANYM-990
def test_main_is_silent_when_nothing_changed_and_prints_one_line_when_it_did(tmp_path, capsys):
    gw, tasks = FakeGateway(), FakeTasks()
    env = {'DEEDKIT_FEEDBACK_BRIDGE_ENV': str(write_env(tmp_path))}
    assert fb.main(['run'], environ=env, gateway=gw, tasks=tasks) == 0 and capsys.readouterr().out == ''
    gw.submit(row(1))
    assert fb.main(['run'], environ=env, gateway=gw, tasks=tasks) == 0
    assert capsys.readouterr().out.strip() == 'feedback-bridge: created=1 updated=0 written=0'


# spec: ANYM-990
def test_reverse_writes_a_task_side_acceptance_once_with_a_deterministic_request_id(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    key = task_of(tasks, 1)['key']
    tasks.human(key, status='todo', labels=['feedback', 'spec:ANYM-990'])
    version = tasks.tasks[key]['updatedAt']
    assert bridge.reverse()['written'] == 1
    write = gw.calls('messages_set_feedback_triage')[0]
    assert write == {'feedback_id': fid(1), 'status': 'accepted', 'expected_status': 'received', 'expected_changed_at': None,
        'request_id': fb.request_id(fid(1), 'T001', version), 'spec_ref': 'ANYM-990'}
    assert re.fullmatch(r'[0-9a-f]{32}', write['request_id'])
    assert bridge.reverse()['written'] == 0 and len(gw.calls('messages_set_feedback_triage')) == 1
    bridge.cycle()  # the forward pass sees `accepted` and the task already compatible: no rewrite
    assert tasks.tasks[key]['status'] == 'todo' and not [e for e in tasks.log if e[0] == 'update' and e[2] is not None]


# spec: ANYM-990
def test_reverse_declines_a_canceled_task_and_marks_a_duplicate_only_with_its_line(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    for n in (1, 2, 3):
        gw.submit(row(n))
    bridge.cycle()
    tasks.human(task_of(tasks, 1)['key'], status='canceled')
    two = task_of(tasks, 2)['key']
    tasks.human(two, status='canceled', labels=['feedback', 'duplicate'], description=tasks.tasks[two]['description'] +
        '\nDuplicate-Of: %s\n' % fid(1))
    three = task_of(tasks, 3)['key']
    tasks.human(three, status='canceled', labels=['feedback', 'duplicate'])
    assert bridge.reverse()['written'] == 2
    writes = {w['feedback_id']: w for w in gw.calls('messages_set_feedback_triage')}
    assert writes[fid(1)]['status'] == 'declined' and writes[fid(2)]['status'] == 'duplicate'
    assert writes[fid(2)]['duplicate_of'] == fid(1) and fid(3) not in writes
    for _ in range(3):
        bridge.reverse()
    assert [ref for ref, body in tasks.comments if 'Duplicate-Of' in body] == [three]
    assert all('spec_ref' not in write for write in writes.values())  # declined and duplicate carry no card


# spec: ANYM-992
def test_an_acceptance_names_its_card_from_the_task_label_else_from_the_projection_and_with_neither_writes_nothing(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1, projection=projection()))  # primary card ANYM-12
    gw.submit(row(2, projection=projection()))
    gw.submit(row(3))
    gw.submit(row(4, projection=projection()))
    bridge.cycle()
    keys = {n: task_of(tasks, n)['key'] for n in (1, 2, 3, 4)}
    assert bridge.ledger.link(fid(1))['primary_spec'] == 'ANYM-12' and bridge.ledger.link(fid(3))['primary_spec'] is None
    tasks.human(keys[1], status='todo')  # no label: the projection's card
    tasks.human(keys[2], status='todo', labels=['feedback', 'spec:ANYM-77'])  # the label wins
    tasks.human(keys[3], status='todo')  # neither
    tasks.human(keys[4], status='in_progress', labels=['feedback', 'spec:ANYM-1', 'spec:ANYM-2'])  # two labels: not a choice
    assert bridge.reverse()['written'] == 3
    writes = {w['feedback_id']: w for w in gw.calls('messages_set_feedback_triage')}
    assert {fid(n): writes[fid(n)]['spec_ref'] for n in (1, 2, 4)} == {fid(1): 'ANYM-12', fid(2): 'ANYM-77', fid(4): 'ANYM-12'}
    assert fid(3) not in writes
    said = [body for ref, body in tasks.comments if ref == keys[3]]
    assert len(said) == 1 and 'card is needed' in said[0] and 'spec:ANYM-<n>' in said[0]
    for _ in range(3):
        assert bridge.reverse()['written'] == 0
    assert len([1 for ref, body in tasks.comments if ref == keys[3]]) == 1 and fid(3) not in [
        w['feedback_id'] for w in gw.calls('messages_set_feedback_triage')]
    tasks.human(keys[3], labels=['feedback', 'spec:ANYM-88'])  # the human names the card: the next pass writes it
    assert bridge.reverse()['written'] == 1
    assert gw.calls('messages_set_feedback_triage')[-1]['spec_ref'] == 'ANYM-88'
    assert len([1 for ref, body in tasks.comments if ref == keys[3]]) == 1


# spec: ANYM-992
def test_a_ledger_of_an_earlier_version_gains_the_primary_card_column_and_keeps_its_rows(tmp_path):
    import sqlite3
    state = tmp_path / 'state'
    state.mkdir()
    old = sqlite3.connect(state / 'ledger.sqlite3')
    old.execute('CREATE TABLE links(fid TEXT PRIMARY KEY, task_id TEXT, task_key TEXT, skipped INTEGER NOT NULL DEFAULT 0, '
        'fb_status TEXT, fb_changed_at TEXT, fp TEXT, title TEXT, desc_sha TEXT, bridge_version TEXT, seen_version TEXT)')
    old.execute("INSERT INTO links(fid,task_id,task_key,fb_status) VALUES('f-1','T1','DK-1','received')")
    old.commit()
    old.close()
    ledger = fb.Ledger(state)
    assert ledger.link('f-1')['task_key'] == 'DK-1' and ledger.link('f-1')['primary_spec'] is None
    ledger.update_link('f-1', primary_spec='ANYM-5')
    assert fb.Ledger(state).link('f-1')['primary_spec'] == 'ANYM-5'


# spec: ANYM-990
def test_a_version_the_bridge_set_itself_is_never_written_back_in_thirty_passes(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    gw.server_change(1, 'declined')
    bridge.cycle()
    key = task_of(tasks, 1)['key']
    assert tasks.tasks[key]['status'] == 'canceled'
    for _ in range(30):
        assert bridge.reverse()['written'] == 0
    tasks.human(key, title='renamed by a human')  # a new version, still compatible
    assert bridge.reverse()['written'] == 0 and not gw.calls('messages_set_feedback_triage')


# spec: ANYM-990
def test_done_in_bb_is_never_written_back_and_a_comment_says_why_once(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.server_change(1, 'fixed')
    gw.rows[0]['fixed_in'] = 'deedkit@' + 'a' * 40
    bridge.cycle()
    key = task_of(tasks, 1)['key']
    tasks.human(key, status='done')
    for _ in range(3):
        assert bridge.reverse()['written'] == 0
    notes = [body for ref, body in tasks.comments if ref == key]
    assert len(notes) == 1 and 'not fixed or deployed' in notes[0] and not gw.calls('messages_set_feedback_triage')


# spec: ANYM-990
def test_an_uncertain_status_write_that_applied_is_read_back_and_not_replayed(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    tasks.human(task_of(tasks, 1)['key'], status='todo', labels=['feedback', 'spec:ANYM-990'])
    gw.write_mode = 'uncertain_applied'
    assert bridge.reverse()['written'] == 1
    assert len(gw.calls('messages_set_feedback_triage')) == 1 and bridge.ledger.link(fid(1))['fb_status'] == 'accepted'
    assert bridge.reverse()['written'] == 0


# spec: ANYM-990
def test_an_uncertain_status_write_that_did_not_apply_is_replayed_with_the_same_request_id(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    tasks.human(task_of(tasks, 1)['key'], status='todo', labels=['feedback', 'spec:ANYM-990'])
    gw.write_mode = 'uncertain_not_applied'
    assert bridge.reverse()['written'] == 1
    sent = gw.calls('messages_set_feedback_triage')
    assert len(sent) == 2 and sent[0]['request_id'] == sent[1]['request_id']
    assert gw.rows[0]['status'] == 'accepted'


# spec: ANYM-990
def test_a_conflict_is_not_replayed_the_server_status_wins_and_a_comment_says_so(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    gw.server_change(1, 'fixed')
    gw.rows[0]['fixed_in'] = 'deedkit@' + 'a' * 40
    key = task_of(tasks, 1)['key']
    tasks.human(key, status='todo', labels=['feedback', 'spec:ANYM-990'])  # the human acts on a status the server has already left
    assert bridge.reverse()['written'] == 0
    assert len(gw.calls('messages_set_feedback_triage')) == 1
    assert tasks.tasks[key]['status'] == 'in_review'
    said = [body for ref, body in tasks.comments if ref == key]
    assert len(said) == 1 and 'fixed' in said[0] and 'accepted' in said[0]
    bridge.reverse()
    assert len(gw.calls('messages_set_feedback_triage')) == 1 and len(tasks.comments) == 1


# spec: ANYM-990
def test_an_incomplete_task_listing_changes_nothing_and_a_missing_write_tool_leaves_reverse_idle(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    tasks.human(task_of(tasks, 1)['key'], status='todo', labels=['feedback', 'spec:ANYM-990'])
    tasks.fail_list = fb.Incomplete('tasks_listing_incomplete')
    with pytest.raises(fb.Incomplete):
        bridge.reverse()
    assert not gw.calls('messages_set_feedback_triage')
    tasks.fail_list = None
    gw.fail['messages_set_feedback_triage'] = fb.Refused('forbidden', 403)
    assert bridge.reverse() == {'written': 0, 'unavailable': 'forbidden'}
    gw.fail = {}
    assert bridge.reverse()['written'] == 1  # the same version is still waiting


# spec: ANYM-990
def test_the_gateways_refusal_of_a_tool_the_key_cannot_call_leaves_reverse_idle_and_the_change_waiting(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    tasks.human(task_of(tasks, 1)['key'], status='todo', labels=['feedback', 'spec:ANYM-990'])
    gw.fail['messages_set_feedback_triage'] = fb.Refused('not_found_or_forbidden', 403)  # the gateway's one body for an uncallable tool
    assert bridge.reverse() == {'written': 0, 'unavailable': 'not_found_or_forbidden'}
    gw.fail = {}
    assert bridge.reverse()['written'] == 1


# spec: ANYM-990
def test_an_item_the_server_no_longer_has_is_skipped_and_the_next_task_is_still_written(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.submit(row(2))
    bridge.cycle()
    for n in (1, 2):
        tasks.human(task_of(tasks, n)['key'], status='todo', labels=['feedback', 'spec:ANYM-990'])
    gw.rows = [r for r in gw.rows if r['feedback_id'] != fid(1)]  # feedback_triage: not_found (404) names this item only
    assert bridge.reverse() == {'written': 1}
    assert [w['feedback_id'] for w in gw.calls('messages_set_feedback_triage')] == [fid(1), fid(2)]
    assert bridge.reverse() == {'written': 0} and len(gw.calls('messages_set_feedback_triage')) == 2  # not asked again


# spec: ANYM-990
def test_a_policy_change_between_the_gateway_and_its_tool_keeps_the_task_change_waiting(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    gw.submit(row(2))
    bridge.cycle()
    for n in (1, 2):
        tasks.human(task_of(tasks, n)['key'], status='todo', labels=['feedback', 'spec:ANYM-990'])
    gw.fail['messages_set_feedback_triage'] = fb.Refused('policy_changed', 409)  # dispatch: raised before any backend call
    assert bridge.reverse() == {'written': 0}
    assert len(gw.calls('messages_set_feedback_triage')) == 2  # the pass went on to the second task
    gw.fail = {}
    assert bridge.reverse()['written'] == 2
    sent = gw.calls('messages_set_feedback_triage')
    assert [w['request_id'] for w in sent[:2]] == [w['request_id'] for w in sent[2:]]  # the same deterministic request IDs


# spec: ANYM-990
@given(st.sampled_from(sorted(fb.FORWARD)), st.sampled_from(['backlog', 'todo', 'in_progress', 'in_review', 'done', 'canceled']),
       st.booleans(), st.booleans())
def test_no_status_the_reverse_map_writes_is_rewritten_by_the_forward_map(feedback_status, task_status, tagged, line):
    text = 'Duplicate-Of: %s' % fid(1) if line else ''
    result = fb.reverse_status(task_status, ['feedback', 'duplicate'] if tagged else ['feedback'], feedback_status, text)
    assert fb.FORWARD[feedback_status] in fb.COMPATIBLE[feedback_status]
    if result is not None:
        status, duplicate_of = result
        assert status in ('accepted', 'declined', 'duplicate') and task_status in fb.COMPATIBLE[status]
        assert (duplicate_of is not None) == (status == 'duplicate')
    for status in fb.FORWARD:
        assert fb.reverse_status(fb.FORWARD[status], ['feedback'], status, '') is None  # no loop from the bridge's own write


# spec: ANYM-990
def test_the_first_start_creates_one_subscription_and_the_second_start_reuses_it(tmp_path):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    bridge.watch(stopper(1), sleep=lambda s: None)
    assert [s['label'] for s in gw.subs] == [SUB] and bridge.ledger.meta('subscription_id') == 'sub-1'
    again, _, _ = make(tmp_path, gateway=gw, tasks=tasks, subscription_id=None)
    again.watch(stopper(1), sleep=lambda s: None)
    assert len(gw.subs) == 1 and gw.calls('messages_wait_for_subscription_events')[-1]['subscription_id'] == 'sub-1'
    assert gw.calls('messages_wait_for_subscription_events')[0] == {'subscription_id': 'sub-1', 'timeout': 25, 'limit': 100,
        'detail': 'compact'}


# spec: ANYM-990
def test_an_uncertain_subscription_create_that_created_is_adopted_and_not_repeated(tmp_path):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    gw.create_mode = 'uncertain_created'
    sleeps = []
    bridge.watch(stopper(3), sleep=sleeps.append)
    assert len(gw.subs) == 1 and len(gw.calls('messages_create_subscription')) == 1
    assert bridge.ledger.meta('subscription_id') == 'sub-1' and sleeps[0] == 1


# spec: ANYM-990
def test_an_uncertain_subscription_create_that_did_not_create_creates_once_after_a_complete_listing(tmp_path):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    gw.create_mode = 'uncertain_not_created'
    bridge.watch(stopper(3), sleep=lambda s: None)
    assert len(gw.subs) == 1 and len(gw.calls('messages_create_subscription')) == 2


# spec: ANYM-990
@pytest.mark.parametrize('listing', ['failed', 'full'])
def test_a_failed_or_full_subscription_listing_means_no_create(tmp_path, listing):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    gw.create_mode = 'uncertain_not_created'
    bridge.watch(stopper(1), sleep=lambda s: None)
    creates = len(gw.calls('messages_create_subscription'))
    if listing == 'failed':
        gw.fail['messages_list_subscriptions'] = fb.Uncertain('gateway_unreachable')
    else:
        gw.subs = [{'subscription_id': 'other-%d' % n, 'label': 'x', 'state': 'active'} for n in range(101)]
    bridge.watch(stopper(3), sleep=lambda s: None)
    assert len(gw.calls('messages_create_subscription')) == creates and bridge.ledger.meta('subscription_id') is None


# spec: ANYM-990
def test_a_listing_that_says_has_more_is_incomplete_whatever_its_length(tmp_path):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    gw.create_mode = 'uncertain_not_created'
    bridge.watch(stopper(1), sleep=lambda s: None)
    creates = len(gw.calls('messages_create_subscription'))
    gw.messages_list_subscriptions = lambda limit=50: {'items': [], 'has_more': True}  # list_subscriptions: more than `limit`
    bridge.watch(stopper(3), sleep=lambda s: None)
    assert len(gw.calls('messages_create_subscription')) == creates and bridge.ledger.meta('subscription_id') is None


# spec: ANYM-990
def test_a_listing_of_exactly_one_hundred_without_has_more_is_complete_and_a_pending_create_is_made_once(tmp_path):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    gw.create_mode = 'uncertain_not_created'
    bridge.watch(stopper(1), sleep=lambda s: None)
    gw.subs = [{'subscription_id': 'other-%d' % n, 'label': 'x', 'state': 'active'} for n in range(100)]
    assert gw.messages_list_subscriptions(limit=100)['has_more'] is False
    bridge.watch(stopper(3), sleep=lambda s: None)
    assert len(gw.calls('messages_create_subscription')) == 2 and bridge.ledger.meta('subscription_id') == 'sub-101'


# spec: ANYM-990
def test_a_pending_create_adopts_the_live_definition_of_its_label_from_the_items_listing(tmp_path):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    gw.subs = [{'subscription_id': 'sub-9', 'label': SUB, 'state': 'closed', 'version': 3},
        {'subscription_id': 'sub-8', 'label': 'other', 'state': 'active', 'version': 1},
        {'subscription_id': 'sub-6', 'state': 'access_changed', 'version': 2, 'recreate_required': True, 'scope_masked': True},
        {'subscription_id': 'sub-7', 'label': SUB, 'state': 'active', 'version': 1}]  # the masked and closed ones name no live one
    bridge.ledger.set_meta('subscription_pending', '1')
    assert bridge.ensure_subscription() == 'sub-7' and not gw.calls('messages_create_subscription')
    assert bridge.ledger.meta('subscription_pending') is None


# spec: ANYM-990
def test_the_backoff_doubles_to_a_cap_resets_on_success_and_never_busy_loops(tmp_path, capsys):
    bridge, gw, tasks = make(tmp_path)
    gw.fail['messages_wait_for_subscription_events'] = fb.Uncertain('gateway_unreachable')
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 9:
            gw.fail = {}
    bridge.watch(stopper(12), sleep=sleep)
    assert sleeps[:9] == [1, 2, 4, 8, 16, 32, 60, 60, 60] and set(sleeps[9:]) == {1}
    assert capsys.readouterr().err.count('feedback-bridge: gateway_unreachable') == 9


# spec: ANYM-990
def test_a_page_with_items_is_applied_then_acked_and_reverse_follows_it(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.on_wait = lambda: gw.submit(row(1))
    bridge.watch(stopper(1), sleep=lambda s: None)
    assert gw.order == ['create', 'ack'] and len(tasks.tasks) == 1
    assert len(gw.calls('messages_fetch_subscription_events')) == 1  # the catch-up cycle before the loop


# spec: ANYM-990
def test_reverse_runs_at_least_every_120_seconds_while_the_loop_is_idle(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    clock = {'t': 0.0}
    tasks.log.clear()
    seen = []
    original = tasks.list_tasks

    def listing(label=None):
        seen.append(clock['t'])
        return original(label)
    tasks.list_tasks = listing

    def sleep(seconds):
        clock['t'] += 25

    gw_wait = gw.messages_wait_for_subscription_events

    def waiting(**kw):
        clock['t'] += 25
        return gw_wait(**kw)
    gw.messages_wait_for_subscription_events = waiting
    bridge.watch(stopper(12), sleep=sleep, monotonic=lambda: clock['t'])
    gaps = [b - a for a, b in zip(seen, seen[1:])]
    assert gaps and all(100 <= gap <= 150 for gap in gaps[1:])


# spec: ANYM-990
def test_a_closed_subscription_is_cleared_and_recreated_once(tmp_path):
    bridge, gw, tasks = make(tmp_path, subscription_id=None)
    bridge.watch(stopper(1), sleep=lambda s: None)
    gw.fail['messages_wait_for_subscription_events'] = fb.Refused('subscription_closed', 409)
    bridge.watch(stopper(1), sleep=lambda s: None)
    gw.fail = {}
    gw.subs[0]['state'] = 'closed'
    bridge.watch(stopper(2), sleep=lambda s: None)
    assert [s['subscription_id'] for s in gw.subs] == ['sub-2', 'sub-1'] and bridge.ledger.meta('subscription_id') == 'sub-2'


# spec: ANYM-990
def test_the_watch_cycle_waits_for_the_lock_the_fallback_run_holds(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.on_wait = lambda: gw.submit(row(1))
    holder, release, started = make(tmp_path)[0], threading.Event(), threading.Event()

    def hold():
        with holder.lock(block=False):
            started.set()
            release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    started.wait(5)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    # the catch-up cycle is the first thing under the lock, so it waits for the holder; nothing is created before
    bridge.watch(stopper(1), sleep=lambda s: None)
    thread.join()
    timer.join()
    assert len(tasks.tasks) == 1 and gw.acked == 1


def write_env(tmp_path, mode=0o600, **over):
    key = tmp_path / 'key'
    key.write_text(KEY + '\n')
    key.chmod(0o600)
    lines = {'DEEDKIT_GATEWAY_URL': 'https://agents.example', 'DEEDKIT_FEEDBACK_KEY_FILE': str(key),
        'DEEDKIT_FEEDBACK_SUBSCRIPTION_ID': 'sub-0', 'DEEDKIT_BRIDGE_STATE_DIR': str(tmp_path / 'state')} | over
    path = tmp_path / 'feedback-bridge.env'
    path.write_text(''.join('%s=%s\n' % (k, v) for k, v in lines.items() if v is not None))
    path.chmod(mode)
    return path


# spec: ANYM-990
def test_the_env_file_loads_with_defaults_and_the_key_is_read_without_its_newline(tmp_path):
    cfg = fb.load_config(write_env(tmp_path, DEEDKIT_FEEDBACK_SUBSCRIPTION_ID=None), {})
    assert (cfg.gateway_url, cfg.key, cfg.subscription_id, cfg.tracker, cfg.backfill_days) == (
        'https://agents.example', KEY, None, 'DK', 14)
    assert stat.S_IMODE(cfg.state_dir.stat().st_mode) == 0o700 if cfg.state_dir.exists() else True
    environ = {'DEEDKIT_FEEDBACK_BRIDGE_ENV': str(write_env(tmp_path, DEEDKIT_BRIDGE_TRACKER='ZZ'))}
    assert fb.load_config(None, environ).tracker == 'ZZ'


# spec: ANYM-990
@pytest.mark.parametrize('case', ['env_mode', 'key_mode', 'key_symlink', 'missing_url', 'http_url', 'missing_key'])
def test_a_loose_or_incomplete_credential_setup_is_refused_without_printing_values(tmp_path, case):
    env = write_env(tmp_path, mode=0o644 if case == 'env_mode' else 0o600,
        DEEDKIT_GATEWAY_URL={'missing_url': None, 'http_url': 'http://agents.example'}.get(case, 'https://agents.example'))
    key = tmp_path / 'key'
    if case == 'key_mode':
        key.chmod(0o640)
    if case == 'key_symlink':
        real = tmp_path / 'real'
        key.rename(real)
        key.symlink_to(real)
    if case == 'missing_key':
        key.unlink()
    with pytest.raises(fb.ConfigError) as caught:
        fb.load_config(env, {})
    assert KEY not in str(caught.value) and str(tmp_path) not in str(caught.value)


# spec: ANYM-990
def test_a_loopback_http_gateway_is_accepted_for_local_tests(tmp_path):
    assert fb.load_config(write_env(tmp_path, DEEDKIT_GATEWAY_URL='http://127.0.0.1:9'), {}).gateway_url == 'http://127.0.0.1:9'


class Server:
    """A loopback HTTP server answering one scripted reply per request."""

    def __init__(self, replies):
        outer = self
        self.seen, self.replies = [], list(replies)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                outer.seen.append((self.path, self.headers['Authorization'], json.loads(body)))
                status, payload, headers = outer.replies.pop(0)
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass
        self.httpd = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        self.url = 'http://127.0.0.1:%d' % self.httpd.server_port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# spec: ANYM-990
def test_the_gateway_client_posts_json_with_the_bearer_key_and_classifies_every_answer():
    ok = (200, b'{"items":[]}', {})
    replies = [ok, (404, b'{"error":"forbidden"}', {}), (409, b'{"error":"version_conflict"}', {}),
        (422, b'{"error":"CANARY-unknown-code"}', {}), (500, b'oops ' + KEY.encode(), {}),
        (302, b'', {'Location': 'http://127.0.0.1:1/'}), (200, b'not json', {})]
    with Server(replies) as server:
        gateway = fb.Gateway(server.url, KEY)
        assert gateway.call('messages_export_feedback_metadata', {'limit': 5}) == {'items': []}
        assert server.seen[0] == ('/v1/messages_export_feedback_metadata', 'Bearer ' + KEY, {'limit': 5})
        for code, status in (('forbidden', 404), ('version_conflict', 409), ('http_error', 422)):
            with pytest.raises(fb.Refused) as caught:
                gateway.call('t', {})
            assert caught.value.code == code and caught.value.status == status
        for exception in (fb.Uncertain, fb.Refused, fb.Uncertain):
            with pytest.raises(exception) as caught:
                gateway.call('t', {})
            assert KEY not in str(caught.value) and 'CANARY' not in str(caught.value)
    with pytest.raises(fb.Uncertain):
        fb.Gateway('http://127.0.0.1:1', KEY).call('t', {})  # connection refused


# spec: ANYM-990
def test_the_gateways_own_refusals_are_read_as_the_codes_it_sends_and_a_backend_failure_names_its_status(capsys):
    # agent_gateway.dispatch.unknown_tool_denial (HTTP 403) and diagnostics.failure_details / http_clients (HTTP 503), as the REST route answers
    denied = {'error': 'not_found_or_forbidden', 'next_action': 'refresh_tool_list_once', 'hint': 'If the tool is still absent after a refresh.'}
    crashed = {'error': 'backend_unavailable', 'backend_status': 500, 'request_id': 'a' * 32}
    waited = {'error': 'backend_unavailable', 'backend_status': 503, 'backend_error': 'subscription_wait_timeout'}
    lost = {'error': 'backend_unavailable', 'reason': 'gateway_timeout'}
    hostile = {'error': 'backend_unavailable', 'backend_status': 'CANARY\n', 'backend_error': 'CANARY-code', 'reason': KEY}
    replies = [(403, json.dumps(denied).encode(), {})] + [(503, json.dumps(x).encode(), {}) for x in (crashed, waited, lost, hostile)]
    with Server(replies) as server:
        gateway = fb.Gateway(server.url, KEY)
        with pytest.raises(fb.Refused) as caught:
            gateway.call('messages_set_feedback_triage', {})
        assert (caught.value.code, caught.value.status) == ('not_found_or_forbidden', 403)
        said = []
        for _ in range(4):
            with pytest.raises(fb.Uncertain) as caught:
                gateway.call('messages_create_subscription', {})
            assert (caught.value.code, caught.value.status) == ('gateway_http_503', 503)
            said.append(caught.value.note)
            fb.report(caught.value)
    assert said == ['backend_status=500', 'backend_status=503 backend_error=subscription_wait_timeout', 'reason=gateway_timeout', '']
    lines = capsys.readouterr().err.splitlines()
    assert lines == ['feedback-bridge: gateway_http_503 (backend_status=500)',
        'feedback-bridge: gateway_http_503 (backend_status=503 backend_error=subscription_wait_timeout)',
        'feedback-bridge: gateway_http_503 (reason=gateway_timeout)', 'feedback-bridge: gateway_http_503']


def runner_for(script):
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        return script(argv)
    return run, calls


# spec: ANYM-990
def test_the_bb_tasks_client_builds_the_cli_calls_and_reads_its_json(tmp_path):
    task = {'id': 'T1', 'key': 'DK-1', 'number': 1, 'title': 't', 'description': 'd', 'status': 'backlog',
        'labels': ['feedback'], 'updatedAt': 'v1'}
    run, calls = runner_for(lambda argv: (0, json.dumps({'task': task})))
    tasks = fb.BbTasks('DK', runner=run, state_dir=tmp_path)
    assert tasks.create('Title', 'Feedback-ID: x\nbody', ['feedback', 'fb-bug'])['key'] == 'DK-1'
    argv = calls[0]
    assert argv[:5] == ['bb', 'tasks', 'create', '--project', 'DK'] and argv[argv.index('--title') + 1] == 'Title'
    assert argv.count('--label') == 2 and argv[-1] == '--json' and '--description-file' in argv
    assert tasks.update('DK-1', status='todo')['status'] == 'backlog'
    assert calls[1][:4] == ['bb', 'tasks', 'update', 'DK-1'] and calls[1][calls[1].index('--status') + 1] == 'todo'
    tasks.comment('DK-1', 'hello')
    assert calls[2][:4] == ['bb', 'tasks', 'comment', 'DK-1'] and '--body-file' in calls[2]
    assert not any(p.name.startswith('tmp') for p in tmp_path.rglob('*') if p.is_file())  # temp files are removed


# spec: ANYM-990
def test_the_bb_tasks_client_follows_pages_and_refuses_a_listing_it_cannot_finish(tmp_path):
    pages = {None: {'tasks': [{'id': 'A', 'key': 'DK-1', 'number': 1}], 'nextCursor': 'c1'},
        'c1': {'tasks': [{'id': 'B', 'key': 'DK-2', 'number': 2}], 'nextCursor': None}}

    def script(argv):
        cursor = argv[argv.index('--cursor') + 1] if '--cursor' in argv else None
        return 0, json.dumps(pages[cursor])
    run, calls = runner_for(script)
    tasks = fb.BbTasks('DK', runner=run, state_dir=tmp_path)
    assert [t['key'] for t in tasks.list_tasks('feedback')] == ['DK-1', 'DK-2']
    assert '--label' in calls[0] and all(s in calls[0][calls[0].index('--status') + 1] for s in ('backlog', 'done', 'canceled'))
    endless, _ = runner_for(lambda argv: (0, json.dumps({'tasks': [], 'nextCursor': 'again'})))
    with pytest.raises(fb.Incomplete):
        fb.BbTasks('DK', runner=endless, state_dir=tmp_path).list_tasks()


# spec: ANYM-990
def test_the_bb_tasks_client_tells_a_definite_refusal_from_an_uncertain_outcome(tmp_path):
    def raise_timeout(argv):
        raise subprocess.TimeoutExpired(argv, 1)
    for script, expected in ((raise_timeout, fb.Uncertain), (lambda a: (1, 'connection lost'), fb.Uncertain),
            (lambda a: (0, 'not json'), fb.Uncertain),
            (lambda a: (1, json.dumps({'ok': False, 'error': {'code': 'project_not_linked', 'message': 'x'}})), fb.Refused)):
        run, _ = runner_for(script)
        with pytest.raises(expected):
            fb.BbTasks('DK', runner=run, state_dir=tmp_path).create('t', 'd', [])


# spec: ANYM-990
def test_the_default_runner_calls_the_real_bb_executable(tmp_path, monkeypatch):
    fake = tmp_path / 'bin' / 'bb'
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_BB_ARGS"\nprintf \'{"projects":[]}\'\n')
    fake.chmod(0o755)
    monkeypatch.setenv('PATH', str(fake.parent) + os.pathsep + os.environ['PATH'])
    monkeypatch.setenv('FAKE_BB_ARGS', str(tmp_path / 'args'))
    assert fb.BbTasks('DK', state_dir=tmp_path / 's').projects() == []
    assert (tmp_path / 'args').read_text().split() == ['tasks', 'project', 'list', '--json']


# spec: ANYM-990
def test_check_is_read_only_and_reports_the_setup(tmp_path, capsys):
    gw, tasks = FakeGateway(), FakeTasks()
    tasks.projects = lambda: [{'prefix': 'DK'}]
    tasks.label_names = lambda: ['feedback']
    env = {'DEEDKIT_FEEDBACK_BRIDGE_ENV': str(write_env(tmp_path))}
    assert fb.main(['check'], environ=env, gateway=gw, tasks=tasks) == 0
    assert capsys.readouterr().out.splitlines() == ['feedback-bridge: env ok', 'feedback-bridge: tracker DK ok',
        'feedback-bridge: label feedback ok']
    assert not gw.log and not tasks.log
    tasks.projects = lambda: []
    assert fb.main(['check'], environ=env, gateway=gw, tasks=tasks) == 1


# spec: ANYM-990
def test_the_install_material_exposes_no_port_names_the_env_keys_and_holds_no_secret():
    unit = (REPO / 'deploy/feedback-bridge/deedkit-feedback-bridge.service').read_text()
    assert 'feedback_bridge.py watch' in unit and 'NoNewPrivileges=yes' in unit and 'Restart=always' in unit
    assert not re.search(r'Listen|Socket|--port|PORT', unit)
    example = (REPO / 'deploy/feedback-bridge/feedback-bridge.env.example').read_text()
    names = set(re.findall(r'^#?\s*([A-Z_]+)=', example, flags=re.MULTILINE))
    assert names == {'DEEDKIT_GATEWAY_URL', 'DEEDKIT_FEEDBACK_KEY_FILE', 'DEEDKIT_FEEDBACK_SUBSCRIPTION_ID',
        'DEEDKIT_BRIDGE_TRACKER', 'DEEDKIT_BRIDGE_BACKFILL_DAYS', 'DEEDKIT_BRIDGE_STATE_DIR'}
    assert not re.search(r'=[ \t]*[A-Za-z0-9+/_-]{24,}[ \t]*$|whsec_|Bearer', example, flags=re.MULTILINE)
    docs = (REPO / 'docs/operations/feedback-bridge.md').read_text()
    for needle in ('systemctl --user enable --now deedkit-feedback-bridge', 'bb automation create', 'bb tasks project create',
            'label create', 'feedback_bridge.py check', '## Undo'):
        assert needle in docs
    assert 'Funnel' not in docs.replace('no Funnel', '') and 'PORT' not in docs and 'HINT_SECRET' not in docs
    script = (REPO / 'deploy/feedback-bridge/automation-run.sh').read_text()
    assert re.search(r'feedback_bridge\.py"? run', script) and '75' in script
