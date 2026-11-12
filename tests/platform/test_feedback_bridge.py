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
        return getattr(self, tool)(**args)

    def page_of(self):
        items = [{'module_event_id': n, 'id': 'feedback:%d' % n, 'type': kind, 'subject': ident,
            'data': {'feedback_id': ident, 'status': kind.split('.')[1]}} for n, (kind, ident) in enumerate(self.events, 1)]
        return {'module': {'items': items, 'has_more': False}, 'receipt': 'r' * 32 if items else None,
            'ack_version': 7 if items else None}

    def messages_fetch_subscription_events(self, subscription_id, limit=50, detail=None):
        return self.page_of()

    def messages_wait_for_subscription_events(self, subscription_id, timeout=25, limit=50, detail=None):
        if self.on_wait is not None:
            hook, self.on_wait = self.on_wait, None
            hook()
        return self.page_of()

    def messages_ack_subscription_events(self, subscription_id, receipt, version):
        assert receipt == 'r' * 32 and version == 7
        if self.on_ack is not None:
            self.on_ack()
        self.order.append('ack')
        self.events, self.acked = [], self.acked + 1
        if self.crash_after_ack:
            raise Crash
        return {'acked': True}

    def messages_export_feedback_metadata(self, cursor=None, limit=50):
        after = int(cursor or 0)
        rows = sorted((r for r in self.rows if r['_seq'] > after), key=lambda r: r['_seq'])
        page = rows[:min(limit, self.page)]
        more = len(rows) > len(page)
        head = max([r['_seq'] for r in self.rows] or [0])
        return {'items': [{k: v for k, v in r.items() if k != '_seq'} for r in page], 'has_more': more,
            'next_cursor': str(page[-1]['_seq'] if more else max(0, head - 1)), 'untrusted_content': True}

    def messages_create_subscription(self, feedback, label):
        assert feedback == {'maintainer': True}
        if self.create_mode == 'uncertain_not_created':
            self.create_mode = None
            raise fb.Uncertain('gateway_unreachable')
        sub = {'subscription_id': 'sub-%d' % (len(self.subs) + 1), 'label': label, 'state': 'active'}
        self.subs.append(sub)
        if self.create_mode == 'uncertain_created':
            self.create_mode = None
            raise fb.Uncertain('gateway_unreachable')
        return dict(sub)

    def messages_list_subscriptions(self, limit=50):
        return {'subscriptions': [dict(s) for s in self.subs]}

    def messages_set_feedback_triage(self, feedback_id, status, request_id, expected_status, expected_changed_at,
                                     duplicate_of=None):
        if request_id in self.writes:
            return self.writes[request_id]
        if self.write_mode == 'uncertain_not_applied':
            self.write_mode = None
            raise fb.Uncertain('gateway_unreachable')
        item = next(r for r in self.rows if r['feedback_id'] == feedback_id)
        if item['status'] != expected_status or item['status_changed_at'] != expected_changed_at:
            raise fb.Refused('version_conflict', 409)
        self.seq += 1
        item.update(status=status, status_changed_at=iso(NOW + self.seq), _seq=self.seq, duplicate_of=duplicate_of)
        result = self.writes[request_id] = {'feedback_id': feedback_id, 'status': status,
            'status_changed_at': item['status_changed_at']}
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
    gw.submit(row(5, 'fixed', age=5 * day, fixed_in='a' * 40))
    bridge.cycle()
    assert sorted(t['title'] for t in tasks.tasks.values()) == ['bug feedback: messages_search (metadata only)'] * 2
    assert {n: bridge.ledger.link(fid(n))['skipped'] for n in (1, 2, 3, 4, 5)} == {1: 1, 2: 1, 3: 0, 4: 1, 5: 0}
    assert gw.acked == 1


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
    gw.rows[0]['fixed_in'] = 'a' * 40 if status in ('fixed', 'verify_requested') else None
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
def test_an_event_for_an_item_nobody_returns_is_acked_after_three_cycles(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.events.append(('feedback.received', fid(7)))
    for _ in range(2):
        bridge.cycle()
        assert gw.acked == 0
    bridge.cycle()
    assert gw.acked == 1 and not tasks.tasks and bridge.ledger.unresolved(fid(7)) >= 3


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
    bridge.cycle()
    assert len(tasks.tasks) == 1 and tasks.count('create') == 1 and gw.acked == 1


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
    tasks.human(key, status='todo')
    version = tasks.tasks[key]['updatedAt']
    assert bridge.reverse()['written'] == 1
    write = gw.calls('messages_set_feedback_triage')[0]
    assert write == {'feedback_id': fid(1), 'status': 'accepted', 'expected_status': 'received', 'expected_changed_at': None,
        'request_id': fb.request_id(fid(1), 'T001', version)}
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
    gw.rows[0]['fixed_in'] = 'a' * 40
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
    tasks.human(task_of(tasks, 1)['key'], status='todo')
    gw.write_mode = 'uncertain_applied'
    assert bridge.reverse()['written'] == 1
    assert len(gw.calls('messages_set_feedback_triage')) == 1 and bridge.ledger.link(fid(1))['fb_status'] == 'accepted'
    assert bridge.reverse()['written'] == 0


# spec: ANYM-990
def test_an_uncertain_status_write_that_did_not_apply_is_replayed_with_the_same_request_id(tmp_path):
    bridge, gw, tasks = make(tmp_path)
    gw.submit(row(1))
    bridge.cycle()
    tasks.human(task_of(tasks, 1)['key'], status='todo')
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
    gw.rows[0]['fixed_in'] = 'a' * 40
    key = task_of(tasks, 1)['key']
    tasks.human(key, status='todo')  # the human acts on a status the server has already left
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
    tasks.human(task_of(tasks, 1)['key'], status='todo')
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
        gw.subs = [{'subscription_id': 'other-%d' % n, 'label': 'x', 'state': 'active'} for n in range(100)]
    bridge.watch(stopper(3), sleep=lambda s: None)
    assert len(gw.calls('messages_create_subscription')) == creates and bridge.ledger.meta('subscription_id') is None


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
    gw.fail['messages_wait_for_subscription_events'] = fb.Refused('subscription_closed', 410)
    bridge.watch(stopper(1), sleep=lambda s: None)
    gw.fail = {}
    gw.subs[0]['state'] = 'closed'
    bridge.watch(stopper(2), sleep=lambda s: None)
    assert [s['subscription_id'] for s in gw.subs] == ['sub-1', 'sub-2'] and bridge.ledger.meta('subscription_id') == 'sub-2'


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
