"""The merge keeper enqueues only approved, green, correctly numbered ship pull requests (scripts/merge_keeper.py)."""
from hypothesis import given, settings, strategies as st

from scripts import merge_keeper as mk

HEAD = 'a' * 40
OLD = 'b' * 40
GREEN = {'checks / verify': {'status': 'completed', 'conclusion': 'success', 'started_at': '2026-11-09T01:00:00Z'},
         'CI gate': {'status': 'completed', 'conclusion': 'success', 'started_at': '2026-11-09T01:00:00Z'}}


def facts(**changes):
    base: dict[str, object] = dict(number=7, head=HEAD, body=f'Review: orchestrator diff read {HEAD[:12]}', checks=dict(GREEN),
                head_at='2026-11-09T00:00:00Z', main_migrations=('072_a.sql', '073_b.sql'))
    base.update(changes)
    return mk.Facts(**base)  # type: ignore[arg-type]  # test overrides by keyword


# spec: ANYM-625
def test_approved_green_pull_request_is_enqueued():
    assert mk.decide(facts()).action == 'enqueue'


# spec: ANYM-625
def test_trusted_approve_review_on_the_head_counts_and_an_outsider_does_not():
    review = {'state': 'APPROVED', 'commit_id': HEAD, 'author_association': 'COLLABORATOR'}
    assert mk.decide(facts(body='', reviews=[review])).action == 'enqueue'
    outsider = dict(review, author_association='NONE')
    assert mk.decide(facts(body='', reviews=[outsider])).reason == 'no approval recorded for the head'


# spec: ANYM-625
def test_approval_of_an_older_head_holds_and_comments_once():
    stale = facts(body=f'Review: orchestrator diff read {OLD[:12]}')
    decision = mk.decide(stale)
    assert (decision.action, decision.comment_key) == ('hold', f'stale:{HEAD}')
    assert OLD[:12] in decision.comment and HEAD[:12] in decision.comment
    stale.comments = [decision.comment]
    assert mk.act(mk.GitHub(dry_run=True), stale, 'id', decision) == []


# spec: ANYM-625
def test_skipped_gate_or_all_skipped_or_pre_ready_run_never_enqueues():
    skipped = {**GREEN['CI gate'], 'conclusion': 'skipped'}
    assert mk.decide(facts(checks={**GREEN, 'CI gate': skipped})).action == 'hold'
    docs_only = {**GREEN, 'checks / verify': {**GREEN['checks / verify'], 'conclusion': 'skipped'}}
    assert mk.decide(facts(checks=docs_only)).action == 'enqueue'
    assert mk.decide(facts(ready_at='2026-11-09T02:00:00Z')).reason.endswith('started before ready-for-review')
    assert mk.decide(facts(checks={'CI gate': GREEN['CI gate']})).reason == 'required checks: checks / verify: no run on the head'


# spec: ANYM-625
def test_one_queue_failure_retries_once_and_a_second_stops():
    one = [{'at': '2026-11-09T01:30:00Z', 'reason': 'failed_checks', 'group': 'c' * 40}]
    decision = mk.decide(facts(removals=one, failing_jobs=['CI / checks / verify: failure https://x']))
    assert (decision.action, decision.comment_key) == ('enqueue', f'retry:{HEAD}')
    assert 'checks / verify' in decision.comment
    two = one + [{'at': '2026-11-09T02:30:00Z', 'reason': 'failed_checks', 'group': 'd' * 40}]
    decision = mk.decide(facts(removals=two))
    assert (decision.action, decision.comment_key) == ('hold', f'stop:{HEAD}')
    older_head = [dict(r, at='2026-11-08T00:00:00Z') for r in two]
    assert mk.decide(facts(removals=older_head)).action == 'enqueue'


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


check = st.fixed_dictionaries({'status': st.sampled_from(['completed', 'in_progress']),
                               'conclusion': st.sampled_from(['success', 'skipped', 'failure', None]),
                               'started_at': st.sampled_from(['2026-11-09T01:00:00Z', '2026-11-09T03:00:00Z'])})
removal = st.fixed_dictionaries({'at': st.sampled_from(['2026-11-08T00:00:00Z', '2026-11-09T01:30:00Z']),
                                 'reason': st.sampled_from(['failed_checks', 'manual', 'merge_conflict', 'merged']),
                                 'group': st.none()})


# spec: ANYM-625
@settings(max_examples=500)
@given(body=st.sampled_from(['', f'Review: orchestrator diff read {HEAD[:9]}', f'Review: orchestrator diff read {OLD}']),
       checks=st.dictionaries(st.sampled_from(['checks / verify', 'CI gate']), check),
       removals=st.lists(removal, max_size=3), mergeable=st.sampled_from([True, False, None]),
       queued=st.booleans(), draft=st.booleans(), ready_at=st.sampled_from(['', '2026-11-09T02:00:00Z']),
       added=st.sampled_from([(), ('073_x.sql',), ('074_x.sql',), ('075_x.sql',)]))
def test_enqueue_only_when_every_gate_holds(body, checks, removals, mergeable, queued, draft, ready_at, added):
    f = facts(body=body, checks=checks, removals=removals, mergeable=mergeable, queued=queued, draft=draft,
              ready_at=ready_at, added_migrations=added)
    if mk.decide(f).action != 'enqueue':
        return
    since = [r for r in removals if r['at'] >= f.head_at]
    assert mk.head_approved(f) and not queued and not draft and mergeable is True
    assert checks.get('CI gate', {}).get('conclusion') == 'success'
    assert all(c['status'] == 'completed' and c['conclusion'] in ('success', 'skipped') and c['started_at'] >= ready_at
               for c in checks.values()) and set(checks) == set(f.required)
    assert added in ((), ('074_x.sql',))
    assert sum(r['reason'] == 'failed_checks' for r in since) <= 1
    assert not any(r['reason'] == 'manual' for r in since)


# spec: ANYM-625
def test_heartbeat_is_stale_after_the_limit_or_when_missing():
    at = mk.dt.datetime(2026, 11, 9, 1, 0, tzinfo=mk.dt.timezone.utc)
    assert mk.stale('{"ok_at":"2026-11-09T00:50:00Z","host":"mini"}', at) == ''
    assert 'min ago on mini' in mk.stale('{"ok_at":"2026-11-09T00:30:00Z","host":"mini"}', at)
    assert mk.stale('', at) == 'no heartbeat recorded'
    assert mk.stale('nope', at).startswith('unreadable')
