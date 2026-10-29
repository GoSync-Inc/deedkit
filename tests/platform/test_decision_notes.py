"""Research cards that close on a committed note: the note keeps the parts its card's criteria asked for (ANYM-521,
ANYM-625). Reads committed files only; the quality of the research is its reviews' call, not this test's."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sections(path: str) -> dict[str, str]:
    text = (ROOT / path).read_text(encoding='utf-8')
    return {match.group(1): ' '.join(match.group(2).split())
            for match in re.finditer(r'^#{2,3} ([^\n]+)\n(.*?)(?=^#{2,3} |\Z)', text, re.M | re.S)}


# spec: ANYM-521
def test_the_cross_module_note_compares_the_three_options_on_isolation_egress_schema_compatibility_and_effort():
    note = sections('docs/plans/cross-module-subscriptions.md')
    options = [title for title in note if re.match(r'\([abc]\) ', title)]
    assert [title[:3] for title in options] == ['(a)', '(b)', '(c)']
    assert 'router pulls' in options[0] and 'own subscriptions and dispatcher' in options[1]
    assert 'publish directly into the messaging journal through a narrow write role' in options[2]
    assert ('Criteria: failure isolation (a calendar or calls outage or stall must not stall message hints, and the '
            'reverse: the subscription path must not stall calendar sync or a live call), data egress, schema and role '
            'changes, compatibility with what is deployed (N-1 schema, installed gateway), effort.') in note['3. Options compared']
    chosen = note[options[0]]
    for part in ('**Failure isolation:**', '**Data egress:**', '**Schema/roles:**', '**Compatibility:**', '**Effort:**'):
        assert f'- {part}' in chosen
    assert 'Failure isolation: best' in note[options[1]] and 'Egress: each dispatcher' in note[options[1]]
    assert '- Failure isolation fails both ways' in note[options[2]] and '- Compatibility: same `events`' in note[options[2]]
    table = note['Decision table']
    for row in ('A module outage stalls message hints', 'New secrets', 'Schema changes', 'Effort (story points'):
        assert f'| {row}' in table


# spec: ANYM-521
def test_the_cross_module_note_fixes_calendar_and_call_event_kinds_with_reference_only_payloads():
    kinds = sections('docs/plans/cross-module-subscriptions.md')['4. Event kinds and payload references (criterion 2)']
    for kind in ('calendar.event.created', 'calendar.event.changed', 'calendar.event.cancelled', 'calendar.event.starting',
                 'call.finished', 'call.failed', 'call.not_answered', 'call.owner_request'):
        assert f'`{kind}`' in kinds
    assert 'invite response | **declined for now**' in kinds
    assert '"transcript ready" | **folded into `call.finished`**' in kinds
    assert 'Shared references used, soft-linked to the platform epic: `event_ref` and `calendar_ref` (calendar), `call_id` (calls)' in kinds
    assert 'Errors use the epic\'s shape (S1: `state`, `error`, `next`)' in kinds


# spec: ANYM-521
def test_the_cross_module_note_ends_in_one_recommendation_and_child_cards_of_at_most_eight_points():
    note = sections('docs/plans/cross-module-subscriptions.md')
    assert note['0. Answer'].startswith('Recommendation: **option (a), staged')
    rows = re.findall(r'\| (ANYM-\d+) \| [^|]+ \| (\d+) \| (\d+) \|', note['7. Child cards'])
    assert len(rows) >= 2 and all(1 <= int(effort) <= 8 for _, _, effort in rows)
    assert 'Declined, with reason:' in note['7. Child cards']


# spec: ANYM-625
def test_the_dequeue_note_counts_removals_by_reason_and_keeps_reproduced_conflicts_apart_from_hypotheses():
    path = 'docs/quality/merge-queue-dequeues-2026-10-29.md'
    head = (ROOT / path).read_text(encoding='utf-8').split('\n## ', 1)[0]
    assert '(ANYM-625)' in head and 'Window: 2026-10-15T00:00Z to 2026-10-29T00:00Z.' in ' '.join(head.split())
    note = sections(path)
    counts = dict(re.findall(r'\| `([a-z_]+)` \| (\d+) \|', note['Counts by reason (removals in the window)']))
    assert {'merged', 'failed_checks', 'merge_conflict', 'manual'} <= set(counts)
    conflicts = next(text for title, text in note.items() if title.startswith(f"The {counts['merge_conflict']} conflict removals"))
    # One table row per conflict removal, each with the path `git merge-tree` showed; hypotheses stated apart.
    rows = re.findall(r'\| #\d+ \| 10-\d\d \d\d:\d\d \| ([^|]*) \|', conflicts)
    assert len(rows) == int(counts['merge_conflict']) and all(re.search(r'`[^`]+\.\w+`', path) for path in rows)
    assert 'No shared-file hypotheses are left' in conflicts


# spec: ANYM-625
def test_the_dequeue_note_gives_each_cause_one_fix_with_its_owner_and_writes_its_unknowns():
    path = 'docs/quality/merge-queue-dequeues-2026-10-29.md'
    note = sections(path)
    counts = dict(re.findall(r'\| `([a-z_]+)` \| (\d+) \|', note['Counts by reason (removals in the window)']))
    text = (ROOT / path).read_text(encoding='utf-8')
    table = text[text.index('## Causes and proposed fixes'):text.index('## Unknowns')]
    # One row per cause: | X. cause | removals | fix | owner |, each fix and owner cell filled.
    causes = re.findall(r'^\| ([A-D])\. [^|]+\| (\d+)[^|]*\| ([^|]+)\| ([^|]+)\|$', table, re.M)
    assert [cause for cause, *_ in causes] == ['A', 'B', 'C', 'D']
    assert sum(int(removals) for _, removals, _, _ in causes) == int(counts['merge_conflict'])
    assert all(fix.strip() and owner.strip().startswith(('Orchestrator', 'Repository')) for _, _, fix, owner in causes)
    fixes = note['Causes and proposed fixes']
    assert '| Cause | Removals | Fix | Owner |' in fixes
    assert 'Repository: proposal card ANYM-636' in fixes and 'Orchestrator' in fixes
    assert note['Unknowns'].startswith('- ')
