"""The merge keeper enqueues only approved, green, correctly numbered ship pull requests (scripts/merge_keeper.py)."""
from hypothesis import given, settings, strategies as st

from scripts import merge_keeper as mk

HEAD = 'a' * 40
OLD = 'b' * 40
GREEN = {'checks / verify': {'status': 'completed', 'conclusion': 'success', 'started_at': '2026-11-09T01:00:00Z'},
         'CI gate': {'status': 'completed', 'conclusion': 'success', 'started_at': '2026-11-09T01:00:00Z'}}
AT = '2026-11-09T00:10:00Z'


WRITER = 'writer'


def comment(body, created=AT, updated=None, user=WRITER):
    return {'body': body, 'user': user, 'created_at': created, 'updated_at': updated or created}


def approval(sha=HEAD, **changes):
    return comment(f'Review: orchestrator diff read {sha}', **changes)


def facts(**changes):
    base: dict[str, object] = dict(number=7, head=HEAD, comments=[approval()], checks=dict(GREEN),
                                   writers=frozenset({WRITER, 'other-writer'}),
                                   head_at='2026-11-09T00:00:00Z', main_at='2026-11-09T00:00:00Z',
                                   now='2026-11-09T05:00:00Z', main_migrations=('072_a.sql', '073_b.sql'))
    base.update(changes)
    return mk.Facts(**base)  # type: ignore[arg-type]  # test overrides by keyword


# spec: ANYM-625
def test_approved_green_pull_request_is_enqueued():
    assert mk.decide(facts()).action == 'enqueue'


# spec: ANYM-625
def test_trusted_approve_review_on_the_head_counts_and_an_outsider_does_not():
    review = {'state': 'APPROVED', 'commit_id': HEAD, 'user': 'other-writer'}
    assert mk.decide(facts(comments=[], reviews=[review])).action == 'enqueue'
    outsider = dict(review, user='read-only-member')
    assert mk.decide(facts(comments=[], reviews=[outsider])).reason == 'no approval recorded for the head'


# spec: ANYM-625
def test_forged_edited_abbreviated_or_pre_head_approval_comments_do_not_approve():
    for forged in (approval(user='read-only-member'), approval(user='outsider'),
                   approval(updated='2026-11-09T00:20:00Z'), approval(sha=HEAD[:12]),
                   approval(created='2026-11-08T23:00:00Z')):
        assert not mk.head_approved(facts(comments=[forged])), forged


# spec: ANYM-625
def test_approval_of_an_older_head_holds_and_comments_once():
    stale = facts(comments=[approval(OLD)])
    decision = mk.decide(stale)
    assert (decision.action, decision.comment_key) == ('hold', f'stale:{HEAD}')
    assert OLD[:12] in decision.comment and HEAD in decision.comment
    stale.comments.append(comment(decision.comment))
    assert mk.act(mk.GitHub(dry_run=True), stale, 'id', decision) == []


# spec: ANYM-625
def test_approval_of_the_previous_head_posted_before_the_push_still_gets_the_stale_comment():
    before_push = facts(comments=[approval(OLD, created='2026-11-08T23:00:00Z')])
    decision = mk.decide(before_push)
    assert (decision.action, decision.comment_key) == ('hold', f'stale:{HEAD}')


# spec: ANYM-625
def test_an_outsider_marker_does_not_mute_the_keeper_comment():
    decision = mk.decide(facts(comments=[approval(OLD)]))
    muted = facts(comments=[approval(OLD), comment(decision.comment, user='read-only-member')])
    assert mk.act(mk.GitHub(dry_run=True), muted, 'id', decision) == [f'comment stale:{HEAD}']
    by_bot = facts(comments=[approval(OLD), comment(decision.comment, user=mk.KEEPER_BOT)])
    assert mk.act(mk.GitHub(dry_run=True), by_bot, 'id', decision) == []


# spec: ANYM-625
def test_skipped_gate_or_all_skipped_or_pending_never_enqueues():
    skipped = {**GREEN['CI gate'], 'conclusion': 'skipped'}
    assert mk.decide(facts(checks={**GREEN, 'CI gate': skipped})).action == 'hold'
    docs_only = {**GREEN, 'checks / verify': {**GREEN['checks / verify'], 'conclusion': 'skipped'}}
    assert mk.decide(facts(checks=docs_only)).action == 'enqueue'
    queued = {**GREEN, 'CI gate': {'status': 'queued', 'conclusion': None, 'started_at': None}}
    assert mk.decide(facts(checks=queued)).reason == 'required checks: CI gate: queued'
    assert mk.decide(facts(checks={'CI gate': GREEN['CI gate']})).reason == 'required checks: checks / verify: no run on the head'


# spec: ANYM-625
def test_one_queue_failure_retries_once_after_the_worker_window_and_a_second_stops():
    one = [{'at': '2026-11-09T01:30:00Z', 'reason': 'failed_checks', 'group': 'c' * 40}]
    early = mk.decide(facts(removals=one, now='2026-11-09T01:31:00Z'))
    assert early.action == 'hold' and 'unless a worker does' in early.reason
    decision = mk.decide(facts(removals=one, failing_jobs=['CI / checks / verify: failure https://x']))
    assert (decision.action, decision.comment_key) == ('enqueue', f'retry:{HEAD}')
    assert 'checks / verify' in decision.comment
    two = one + [{'at': '2026-11-09T02:30:00Z', 'reason': 'failed_checks', 'group': 'd' * 40}]
    decision = mk.decide(facts(removals=two))
    assert (decision.action, decision.comment_key) == ('hold', f'stop:{HEAD}')
    older_head = [dict(r, at='2026-11-08T00:00:00Z') for r in two]
    assert mk.decide(facts(removals=older_head)).action == 'enqueue'


# spec: ANYM-625
def test_a_head_queued_or_armed_again_after_two_failures_is_taken_out_by_any_actor():
    two = [{'at': '2026-11-09T01:30:00Z', 'reason': 'failed_checks', 'group': None},
           {'at': '2026-11-09T02:30:00Z', 'reason': 'failed_checks', 'group': None}]
    for state in ({'queued': True}, {'auto_merge': True}):
        decision = mk.decide(facts(removals=two, **state))
        assert (decision.action, decision.comment_key) == ('dequeue', f'budget:{HEAD}')
        assert mk.act(mk.GitHub(dry_run=True), facts(removals=two, **state), 'id', decision)[-1] == 'would dequeue'
    assert mk.decide(facts(removals=two[:1], queued=True)).action == 'skip'


# spec: ANYM-625
def test_queue_conflict_waits_for_main_to_move_and_a_second_stops():
    one = [{'at': '2026-11-09T01:30:00Z', 'reason': 'merge_conflict', 'group': None}]
    assert mk.decide(facts(removals=one, main_at='2026-11-09T01:00:00Z')).action == 'hold'
    assert mk.decide(facts(removals=one, main_at='2026-11-09T01:40:00Z')).action == 'enqueue'
    two = one + [{'at': '2026-11-09T02:30:00Z', 'reason': 'merge_conflict', 'group': None}]
    decision = mk.decide(facts(removals=two, main_at='2026-11-09T03:00:00Z'))
    assert (decision.action, decision.comment_key) == ('hold', f'queue-conflict:{HEAD}')


# spec: ANYM-625
def test_manual_removal_holds_until_the_label_is_added_again():
    manual = [{'at': '2026-11-09T01:30:00Z', 'reason': 'manual', 'group': None}]
    assert mk.decide(facts(removals=manual)).action == 'hold'
    assert mk.decide(facts(removals=manual, labeled_at='2026-11-09T01:40:00Z')).action == 'enqueue'


# spec: ANYM-625
def test_queued_armed_draft_fork_and_conflict_are_left_alone():
    for change in ({'queued': True}, {'auto_merge': True}, {'draft': True}, {'same_repository': False},
                   {'labels': ()}, {'open': False}):
        assert mk.decide(facts(**change)).action == 'skip'
    conflict = mk.decide(facts(mergeable=False))
    assert (conflict.action, conflict.comment_key) == ('hold', f'conflict:{HEAD}')
    assert mk.decide(facts(mergeable=None)).action == 'hold'


# spec: ANYM-625
def test_colliding_migration_number_comments_the_exact_rename():
    decision = mk.decide(facts(added_migrations=('073_wake.sql',)))
    assert (decision.action, decision.comment_key) == ('hold', f'migration:{HEAD}')
    assert f'git mv {mk.MIGRATIONS}/073_wake.sql {mk.MIGRATIONS}/074_wake.sql' in decision.comment
    assert 'RUNTIME_SCHEMA=74' in decision.comment
    assert mk.decide(facts(added_migrations=('074_wake.sql',))).action == 'enqueue'


names = st.from_regex(r'[a-z]{1,8}', fullmatch=True)


# spec: ANYM-625
@settings(max_examples=300)
@given(st.sets(st.integers(1, 200), max_size=8), st.lists(st.tuples(st.integers(1, 210), names), max_size=4))
def test_migration_plan_renames_to_the_next_free_numbers(on_main, added):
    """After the plan's renames the added files take top+1..top+k in order; no plan only when they already do."""
    main = tuple(f'{n:03d}_m.sql' for n in on_main)
    mine = tuple(f'{n:03d}_{name}.sql' for n, name in added)
    plan = mk.migration_plan(mine, main)
    top = max(on_main, default=0)
    after = sorted(new for _, new in plan) if plan else sorted(mine)
    assert [int(n[:3]) for n in after] == list(range(top + 1, top + 1 + len(mine)))
    assert (plan is None) == ([int(n[:3]) for n in sorted(mine)] == list(range(top + 1, top + 1 + len(mine))))


# spec: ANYM-625
@settings(max_examples=300)
@given(st.sets(st.integers(1, 200), max_size=8), st.sets(st.integers(1, 210), min_size=1, max_size=5))
def test_migration_moves_never_target_an_existing_file(on_main, numbers):
    """Replaying the `git mv` lines on the added files never moves onto a present file and ends at the plan."""
    mine = sorted(f'{n:03d}_x.sql' for n in numbers)
    plan = mk.migration_plan(mine, tuple(f'{n:03d}_m.sql' for n in on_main))
    if not plan:
        return
    files = {f'{mk.MIGRATIONS}/{name}' for name in mine}
    for line in mk.migration_moves(plan):
        _, _, source, target = line.split()
        assert source in files and target not in files, line
        files = (files - {source}) | {target}
    assert files == {f'{mk.MIGRATIONS}/{new}' for _, new in plan}


check = st.fixed_dictionaries({'status': st.sampled_from(['completed', 'in_progress', 'queued']),
                               'conclusion': st.sampled_from(['success', 'skipped', 'failure', None]),
                               'started_at': st.sampled_from(['2026-11-09T01:00:00Z', None])})
removal = st.fixed_dictionaries({'at': st.sampled_from(['2026-11-08T00:00:00Z', '2026-11-09T01:30:00Z',
                                                        '2026-11-09T04:59:00Z']),
                                 'reason': st.sampled_from(['failed_checks', 'manual', 'merge_conflict', 'merged']),
                                 'group': st.none()})
approvals = st.sampled_from([approval(), approval(HEAD[:9]), approval(OLD), approval(user='read-only-member'),
                             approval(updated='2026-11-09T00:30:00Z'), approval(created='2026-11-08T00:00:00Z')])


# spec: ANYM-625
@settings(max_examples=500)
@given(comments=st.lists(approvals, max_size=2),
       checks=st.dictionaries(st.sampled_from(['checks / verify', 'CI gate']), check),
       removals=st.lists(removal, max_size=3), mergeable=st.sampled_from([True, False, None]),
       queued=st.booleans(), draft=st.booleans(), main_at=st.sampled_from(['2026-11-09T00:00:00Z', '2026-11-09T02:00:00Z']),
       added=st.sampled_from([(), ('073_x.sql',), ('074_x.sql',), ('075_x.sql',)]))
def test_enqueue_only_when_every_gate_holds(comments, checks, removals, mergeable, queued, draft, main_at, added):
    f = facts(comments=comments, checks=checks, removals=removals, mergeable=mergeable, queued=queued, draft=draft,
              main_at=main_at, added_migrations=added)
    if mk.decide(f).action != 'enqueue':
        return
    since = [r for r in removals if r['at'] >= f.head_at]
    assert approval() in comments and not queued and not draft and mergeable is True
    assert checks.get('CI gate', {}).get('conclusion') == 'success'
    assert all(c['status'] == 'completed' and c['conclusion'] in ('success', 'skipped') for c in checks.values())
    assert set(checks) == set(f.required) and added in ((), ('074_x.sql',))
    failures = [r for r in since if r['reason'] == 'failed_checks']
    assert len(failures) <= 1 and all(r['at'] <= '2026-11-09T04:57:00Z' for r in failures)
    conflicts = [r for r in since if r['reason'] == 'merge_conflict']
    assert len(conflicts) <= 1 and all(r['at'] <= main_at for r in conflicts)
    assert not any(r['reason'] == 'manual' for r in since)


# spec: ANYM-625
def test_heartbeat_is_stale_after_the_limit_or_when_missing():
    at = mk.dt.datetime(2026, 11, 9, 1, 0, tzinfo=mk.dt.timezone.utc)
    assert mk.stale('{"ok_at":"2026-11-09T00:50:00Z","host":"mini"}', at) == ''
    assert 'min ago on mini' in mk.stale('{"ok_at":"2026-11-09T00:30:00Z","host":"mini"}', at)
    assert mk.stale('', at) == 'no heartbeat recorded'
    assert mk.stale('nope', at).startswith('unreadable')


# spec: ANYM-625
def test_watchdog_has_its_own_concurrency_group():
    text = (mk.Path(__file__).resolve().parents[1] / '.github/workflows/merge-keeper.yml').read_text()
    assert "group: merge-keeper-${{ github.event.schedule == '17 * * * *' && 'watchdog' || 'report' }}" in text
    assert "github.event.schedule == '17 * * * *'" in text.split('heartbeat-check')[0].rsplit('- name:', 1)[1]
