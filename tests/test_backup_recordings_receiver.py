"""Recordings receiver, verifier, restore helper and follow support (ANYM-728).

Synthetic SQLite archives and fake engines only: no provider, no Docker, no real age.
"""
import hashlib
import json
import os
from pathlib import Path

import pytest

from messaging_service import backup_r2_receiver as r2
from messaging_service import backup_recordings_continuity as cont
from messaging_service.backup_recordings import create_recordings_snapshot
from test_backup_r2_verifier import HEAD, HostEngine, NEW_IMAGE, follow_status, follower, instances, module, operator, pins  # noqa: F401  (fixtures)

TEXT = 'synthetic words about a synthetic meeting'
CAPTURED = '2026-11-03T21:30:00+00:00'


def rec(i, **changes):
    return {'ref': f'r{i:04d}', 'provider_id': f'p{i}', 'revision': 1, 'transcript': f'{TEXT} {i}', 'summary': f'summary {i}', **changes}


def archive(tmp_path, records, name='a', **kwargs):
    return cont.synthetic_archive(tmp_path / (name + '.sqlite'), records, **kwargs)


def facts_of(tmp_path, records, name='a', **kwargs):
    kwargs.setdefault('events', 50)  # the journal only grows: a deletion does not lower the highest sequence
    return cont.archive_facts(archive(tmp_path, records, name, **kwargs))


def refused(baseline, candidate):
    try:
        cont.judge(baseline, candidate)
    except cont.ContinuityRefused as error:
        return error.code
    return None


def tomb(i, namespace='omi:synthetic'):
    return cont.provider_hash(namespace, f'p{i}')


# spec: ANYM-728
def test_facts_hold_hashes_never_text(tmp_path):
    facts = facts_of(tmp_path, [rec(1), rec(2)], tombstones=[tomb(9)])
    raw = json.dumps(facts)
    assert 'meeting' not in raw and 'words' not in raw
    assert sorted(item[:2] for item in facts['parts']) == [['r0001', 'summary'], ['r0001', 'transcript'],
                                                           ['r0002', 'summary'], ['r0002', 'transcript']]
    assert facts['tombstones'] == [tomb(9)] and facts['archive']['archive_id'] == cont.SYNTHETIC_ARCHIVE_ID
    assert cont.validate_facts(json.loads(raw)) == facts


# spec: ANYM-728
@pytest.mark.parametrize('case', ['emptied', 'truncated', 'part_gone', 'revision_lower', 'ref_gone'])
def test_a_candidate_that_lost_content_is_refused_with_unchanged_count_ref_and_identity(tmp_path, case):
    base = facts_of(tmp_path, [rec(1, revision=3), rec(2)], 'base')
    changed = {'emptied': rec(1, revision=3, transcript=''), 'truncated': rec(1, revision=3, transcript=TEXT[:5]),
               'part_gone': rec(1, revision=3, summary=''), 'revision_lower': rec(1, revision=2)}
    records = [rec(2)] if case == 'ref_gone' else [changed[case], rec(2)]
    if case == 'ref_gone':
        records = [rec(2), rec(5)]  # the count stays 2
    assert refused(base, facts_of(tmp_path, records, 'cand')) == 'archive_content_lost'


# spec: ANYM-728
def test_growth_updates_and_the_same_archive_pass_and_a_changed_digest_needs_a_higher_revision(tmp_path):
    base = facts_of(tmp_path, [rec(1), rec(2)], 'base')
    assert refused(base, base) is None
    assert refused(base, facts_of(tmp_path, [rec(1), rec(2), rec(3)], 'grown')) is None
    assert refused(base, facts_of(tmp_path, [rec(1, revision=2, transcript='updated words'), rec(2)], 'updated')) is None
    assert refused(base, facts_of(tmp_path, [rec(1, transcript='updated words'), rec(2)], 'same_revision')) == 'archive_content_lost'


# spec: ANYM-728
def test_gradual_loss_has_no_cumulative_tolerance(tmp_path):
    records = [rec(i) for i in range(1, 6)]
    baseline = facts_of(tmp_path, records, 'b0')
    for step in range(1, 4):
        candidate = facts_of(tmp_path, [r for r in records if r['ref'] != f'r{step:04d}'] + [rec(20 + step)], f'c{step}')
        assert refused(baseline, candidate) == 'archive_content_lost'  # the refused candidate never became the baseline


# spec: ANYM-728
def test_identity_incarnation_and_sequence_rules(tmp_path):
    base = facts_of(tmp_path, [rec(1)], 'base', incarnation=2, events=10)
    other = facts_of(tmp_path, [rec(1)], 'other', archive_id='arch-other-0002', incarnation=2, events=10)
    assert refused(base, other) == 'archive_identity_changed'
    assert refused(base, facts_of(tmp_path, [rec(1)], 'lowinc', incarnation=1, events=10)) == 'archive_incarnation_lower'
    assert refused(base, facts_of(tmp_path, [rec(1)], 'lowseq', incarnation=2, events=9)) == 'archive_sequence_lower'
    # A restore onto a volume (a higher incarnation) may carry a lower sequence.
    assert refused(base, facts_of(tmp_path, [rec(1), rec(2)], 'restored', incarnation=3, events=4)) is None


# spec: ANYM-728
def test_restoring_an_older_snapshot_that_lost_parts_is_refused_but_a_restore_that_kept_them_grows(tmp_path):
    full = facts_of(tmp_path, [rec(1), rec(2), rec(3)], 'full', events=6)
    older = facts_of(tmp_path, [rec(1), rec(2)], 'older', incarnation=2, events=4)
    assert refused(full, older) == 'archive_content_lost'
    restored_then_growth = facts_of(tmp_path, [rec(1), rec(2), rec(3), rec(4)], 'grown', incarnation=2, events=7)
    assert refused(full, restored_then_growth) is None


# spec: ANYM-728
def test_deletion_is_authorized_only_by_a_new_tombstone_for_that_provider_in_the_candidate(tmp_path):
    base = facts_of(tmp_path, [rec(1), rec(2), rec(3)], 'base')
    deleted = facts_of(tmp_path, [rec(2), rec(3)], 'deleted', tombstones=[tomb(1)])
    assert refused(base, deleted) is None
    # Normal growth right after a deletion passes; a second, untombstoned loss does not.
    assert refused(base, facts_of(tmp_path, [rec(2), rec(3), rec(4)], 'after', tombstones=[tomb(1)])) is None
    assert refused(base, facts_of(tmp_path, [rec(2)], 'two', tombstones=[tomb(1)])) == 'archive_content_lost'
    assert refused(base, facts_of(tmp_path, [rec(2), rec(3)], 'wrong', tombstones=[tomb(7)])) == 'archive_content_lost'
    assert refused(base, facts_of(tmp_path, [rec(2), rec(3)], 'other_namespace', tombstones=[tomb(1, 'omi:other')])) == 'archive_content_lost'
    # Tombstones are append-only, and one already in the baseline authorizes nothing new.
    after = facts_of(tmp_path, [rec(2), rec(3)], 'accepted', tombstones=[tomb(1)])
    assert refused(after, facts_of(tmp_path, [rec(2), rec(3)], 'dropped')) == 'archive_tombstone_dropped'
    assert refused(facts_of(tmp_path, [rec(1), rec(2)], 'pre', tombstones=[tomb(1)]), facts_of(tmp_path, [rec(2)], 'lost', tombstones=[tomb(1)])) == 'archive_content_lost'


# spec: ANYM-728
@pytest.mark.parametrize('bad', ['version', 'extra_key', 'digest', 'ref', 'revision', 'archive_id', 'kind', 'tombstone'])
def test_facts_from_the_helper_are_validated_before_any_decision(tmp_path, bad):
    facts = facts_of(tmp_path, [rec(1)], tombstones=[tomb(1)])
    broken = {'version': {**facts, 'v': 2}, 'extra_key': {**facts, 'text': 'x'},
              'digest': {**facts, 'parts': [['r0001', 'transcript', 1, 'zz']]},
              'ref': {**facts, 'parts': [['bad ref', 'transcript', 1, 'a' * 64]]},
              'revision': {**facts, 'parts': [['r0001', 'transcript', -1, 'a' * 64]]},
              'archive_id': {**facts, 'archive': {**facts['archive'], 'archive_id': 'bad id'}},
              'kind': {**facts, 'parts': [['r0001', 'title', 1, 'a' * 64]]},
              'tombstone': {**facts, 'tombstones': ['nothex']}}[bad]
    with pytest.raises(ValueError, match='invalid_continuity'):
        cont.validate_facts(broken)


# spec: ANYM-728
def test_readback_searches_the_index_and_reads_the_context_of_a_synthetic_archive(tmp_path):
    cont.readback(archive(tmp_path, [rec(1)]))
    cont.readback(archive(tmp_path, [], 'empty'))
    with pytest.raises(ValueError, match='readback_failed'):
        broken = archive(tmp_path, [rec(1)], 'broken')
        import sqlite3
        db = sqlite3.connect(broken)
        db.execute('DROP TABLE texts4')
        db.commit()
        db.close()
        cont.readback(broken)


# spec: ANYM-728
def test_a_readback_fails_when_the_text_survived_but_its_index_was_emptied(tmp_path):
    import sqlite3
    emptied = archive(tmp_path, [rec(1), rec(2)], 'emptied')
    db = sqlite3.connect(emptied)
    db.execute('DELETE FROM texts4')
    db.commit()
    db.close()
    with pytest.raises(ValueError, match='readback_failed'):
        cont.readback(emptied)
    # A record the indexer skipped or has not reached yet is not a failure.
    db = sqlite3.connect(emptied)
    db.executemany('INSERT INTO index_skips VALUES(?)', [('r0001',), ('r0002',)])
    db.commit()
    db.close()
    cont.readback(emptied)


# spec: ANYM-728
@pytest.mark.parametrize('case', ['empty_row', 'unrelated_title', 'metadata_only_records_newer'])
def test_a_readback_cannot_be_satisfied_by_an_empty_row_a_title_or_newer_records_without_text(tmp_path, case):
    import sqlite3
    records = [rec(1)] + ([rec(i, transcript='', summary='') for i in range(2, 6)] if case == 'metadata_only_records_newer' else [])
    path = archive(tmp_path, records, case)
    db = sqlite3.connect(path)
    db.execute('DELETE FROM texts4')
    if case == 'empty_row':
        db.execute("INSERT INTO texts4(ref,namespace,title,transcript,summary) VALUES('r0001','omi:synthetic','','','')")
    if case == 'unrelated_title':
        db.execute("INSERT INTO texts4(ref,namespace,title,transcript,summary) VALUES('r0001','omi:synthetic','synthetic words','','')")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match='readback_failed'):
        cont.readback(path)


# spec: ANYM-728
def test_a_readback_finds_the_words_of_a_real_shaped_transcript_and_ignores_short_words(tmp_path):
    lines = '\n'.join(json.dumps({'speaker': 'A', 'text': text}) for text in ('ok hi', 'planning the quarterly review'))
    path = archive(tmp_path, [rec(1, transcript=lines), rec(2, transcript='ok hi', summary='')])
    cont.readback(path)


# spec: ANYM-728
def test_record_identities_must_be_unambiguous_so_a_tombstone_authorizes_one_record(tmp_path):
    facts = facts_of(tmp_path, [rec(1), rec(2)])
    other = tomb(2)
    ambiguous = {**facts, 'providers': [[facts['providers'][0][0], facts['providers'][0][1]], [facts['providers'][0][0], other],
                                        facts['providers'][1]]}
    for broken in (ambiguous,
                   {**facts, 'providers': [facts['providers'][0], [facts['providers'][1][0], facts['providers'][0][1]]]},
                   {**facts, 'providers': facts['providers'][:1]},
                   {**facts, 'parts': [*facts['parts'], ['r9999', 'transcript', 1, 'a' * 64]]}):
        with pytest.raises(ValueError, match='invalid_continuity'):
            cont.validate_facts(broken)


# --- the restore helper -------------------------------------------------------------------------------------------

def sealed(tmp_path, records, name='bundle', **kwargs):
    staged = archive(tmp_path, records, name + '-staged', **kwargs)
    create_recordings_snapshot(staged, tmp_path / name)
    return tmp_path / name


# spec: ANYM-728
def test_the_restore_helper_gives_the_recordings_checks_and_the_continuity_file(tmp_path):
    from messaging_service import backup_restore_worker as worker
    bundle = sealed(tmp_path, [rec(1), rec(2)], events=5)
    work = tmp_path / 'work'
    work.mkdir()
    sha = hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()
    result = worker.restore_recordings(bundle, sha, work)
    captured = json.loads((bundle / 'manifest.json').read_text())['captured_at']
    assert result == {'v': 1, 'status': 'verified', 'profile': 'recordings', 'schema_version': 4, 'manifest_sha256': sha,
                      'checks': {name: True for name in r2.RECORDINGS_CHECKS}, 'captured_at': captured,
                      'archive': {'archive_id': cont.SYNTHETIC_ARCHIVE_ID, 'incarnation': 1, 'sequence': 5, 'records': 2}}
    written = json.loads((work / 'continuity.json').read_text())
    assert cont.validate_facts(written) == written and len(written['parts']) == 4
    assert 'synthetic words' not in (work / 'continuity.json').read_text()


# spec: ANYM-728
@pytest.mark.parametrize('case,code', [('corrupt', 'restore_archive_failed'), ('schema', 'restore_schema_failed'),
                                       ('extra_member', 'restore_bundle_failed'), ('no_index', 'restore_readback_failed')])
def test_every_recordings_helper_failure_has_its_own_fixed_stage_code(tmp_path, case, code):
    from messaging_service import backup_restore_worker as worker
    bundle = sealed(tmp_path, [rec(1)])
    state = bundle / 'state' / 'archive.sqlite'
    state.chmod(0o600)
    work = tmp_path / 'work'
    work.mkdir()
    if case == 'corrupt':
        data = bytearray(state.read_bytes())
        data[:16] = b'NOT A SQLITE FILE'[:16]
        state.write_bytes(bytes(data))
        manifest = json.loads((bundle / 'manifest.json').read_text())
        manifest['sha256'] = hashlib.sha256(bytes(data)).hexdigest()
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
    elif case == 'schema':
        manifest = json.loads((bundle / 'manifest.json').read_text())
        manifest['user_version'] = 9
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
    elif case == 'extra_member':
        (bundle / 'extra').write_text('x')
    else:
        import sqlite3
        db = sqlite3.connect(state)
        db.execute('DROP TABLE texts4')
        db.commit()
        db.close()
        manifest = json.loads((bundle / 'manifest.json').read_text())
        manifest['sha256'] = hashlib.sha256(state.read_bytes()).hexdigest()
        manifest['bytes'] = state.stat().st_size
        (bundle / 'manifest.json').write_text(json.dumps(manifest))
    sha = hashlib.sha256((bundle / 'manifest.json').read_bytes()).hexdigest()
    with pytest.raises(worker.RestoreStageError, match=code):
        worker.restore_recordings(bundle, sha, work)
    assert not (work / 'result.json').exists()


# spec: ANYM-728
def test_the_worker_config_accepts_the_recordings_profile_and_still_refuses_unknown_ones(tmp_path):
    from messaging_service import backup_restore_worker as worker
    config = {'v': 1, 'profile': 'notes', 'identity_file': '/run/restore/identity', 'work_directory': '/work',
              'database_host': 'restore-postgres', 'database_password': 'a' * 64, 'snapshot_sha256': 'b' * 64,
              'max_plaintext_bytes': 10}
    with pytest.raises(ValueError, match='invalid_restore_worker_config'):
        worker.run(config, None)


# --- the verifier and the receiver ----------------------------------------------------------------------------------

@pytest.fixture
def recordings_operator(operator, tmp_path):  # noqa: F811  (the imported fixture)
    """The recordings instance: a receiver with `profile: recordings` and a helper whose archive the test chooses."""
    v, config, receiver, cloud, engine, write_json = operator
    receiver = {**receiver, 'profile': 'recordings'}
    write_json(Path(config['receiver_config_file']), receiver)
    config = {**config, 'profile': 'recordings'}
    restore = engine.restore
    engine.records = [rec(1), rec(2)]
    engine.options = {}
    engine.workers = []
    engine.plaintext = None

    def recordings_restore(config, receiver, client, receipt, run, payload, secret_dir):
        engine.workers.append(json.loads((secret_dir / 'worker.json').read_text()))
        restore(config, receiver, client, receipt, run, payload, secret_dir)
        if engine.plaintext is not None:
            (payload / 'archive.sqlite').write_bytes(engine.plaintext)
        path = cont.synthetic_archive(tmp_path / f'helper-{run}.sqlite', engine.records, **engine.options)
        facts = cont.archive_facts(path)
        fd = os.open(payload / 'continuity.json', os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'w') as out:
            out.write(json.dumps(facts, separators=(',', ':')))
        return {'v': 1, 'status': 'verified', 'profile': 'recordings', 'schema_version': 4,
                'manifest_sha256': receipt['wire']['snapshot_sha256'], 'checks': {k: True for k in r2.RECORDINGS_CHECKS},
                'archive': facts['archive'], 'captured_at': CAPTURED}
    engine.restore = recordings_restore
    return v, config, receiver, cloud, engine, write_json


def status(config, name='status.json'):
    return json.loads((Path(config['directory']) / name).read_text())


# spec: ANYM-728
def test_the_recordings_verifier_protects_its_own_proof_and_rotates_two_points(recordings_operator):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, _ = recordings_operator
    assert v.run(config, client=cloud, engine=engine)['status'] == 'verified'
    assert engine.workers[0]['profile'] == 'recordings'
    proof = json.loads(next(Path(config['directory']).glob('proof-*.json')).read_text())
    assert proof['verification']['profile'] == 'recordings' and set(proof['verification']['checks']) == r2.RECORDINGS_CHECKS
    assert proof['verification']['archive']['records'] == 2
    old = Path(config['protected_file']).read_bytes()
    receive(receiver, cloud, bid=THIRD)
    engine.records = [rec(1), rec(2), rec(3)]
    assert v.run(config, client=cloud, engine=engine)['backup_id'] == THIRD
    assert Path(config['protected_file']).read_bytes() != old
    objects = [key for key in cloud.objects if '/objects/' in key]
    assert len(objects) == 1 and objects[0].startswith(receiver['prefix'] + '/objects/' + THIRD + '-')


# spec: ANYM-728
def test_good_then_absent_then_corrupt_never_replaces_the_point_across_rotation_and_restart(recordings_operator):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, _ = recordings_operator
    v.run(config, client=cloud, engine=engine)
    marker = Path(config['protected_file']).read_bytes()
    objects = dict(cloud.objects)
    receive(receiver, cloud, bid=THIRD)
    for failure in ('restore_archive_failed', 'restore_bundle_failed', 'restore_readback_failed'):
        engine.failure = v.HelperFailure(failure)
        with pytest.raises(RuntimeError, match=failure):
            v.run(config, client=cloud, engine=engine)
        assert status(config)['error'] == failure and Path(config['protected_file']).read_bytes() == marker
        assert all(cloud.objects.get(key) == value for key, value in objects.items())
    # A restart (a new engine, the same directory) with a good candidate rotates.
    engine.failure = None
    engine.records = [rec(1), rec(2), rec(3)]
    assert v.run(config, client=cloud, engine=engine)['backup_id'] == THIRD
    assert Path(config['protected_file']).read_bytes() != marker


# spec: ANYM-728
def test_an_emptied_archive_never_replaces_the_protected_point_and_the_baseline_stays(recordings_operator):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, _ = recordings_operator
    v.run(config, client=cloud, engine=engine)
    marker = Path(config['protected_file']).read_bytes()
    baseline = (Path(config['directory']) / 'continuity.json').read_bytes()
    receive(receiver, cloud, bid=THIRD)
    engine.records = [rec(1, transcript=''), rec(2, summary='')]  # the same refs and count
    with pytest.raises(RuntimeError, match='archive_content_lost'):
        v.run(config, client=cloud, engine=engine)
    assert status(config)['error'] == 'archive_content_lost'
    assert Path(config['protected_file']).read_bytes() == marker
    assert (Path(config['directory']) / 'continuity.json').read_bytes() == baseline
    assert json.loads(baseline)['facts']['parts'] and 'synthetic words' not in baseline.decode()


# spec: ANYM-728
def test_a_failed_journal_write_after_the_proof_is_ready_keeps_the_facts_and_the_retry_adopts_them(recordings_operator, monkeypatch):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, _ = recordings_operator
    v.run(config, client=cloud, engine=engine)
    receive(receiver, cloud, bid=THIRD)
    engine.records = [rec(1), rec(2), rec(3)]
    original = v.local.write_maintenance_json

    def crashing(name, value, **kw):
        original(name, value, **kw)
        if name == 'active.json' and value.get('phase') == 'proof_ready':
            raise OSError('synthetic directory fsync failure')
    monkeypatch.setattr(v.local, 'write_maintenance_json', crashing)
    with pytest.raises(OSError, match='synthetic'):
        v.run(config, client=cloud, engine=engine)
    monkeypatch.setattr(v.local, 'write_maintenance_json', original)
    assert len(list(Path(config['directory']).glob('continuity-*.json'))) == 1  # the durable journal says proof_ready
    assert v.run(config, client=cloud, engine=engine)['backup_id'] == THIRD
    baseline = json.loads((Path(config['directory']) / 'continuity.json').read_text())
    assert len(baseline['facts']['providers']) == 3 and not list(Path(config['directory']).glob('continuity-*.json'))
    # The protected point and its baseline moved together: a later candidate that lost a newly protected part is refused.
    receive(receiver, cloud, bid='20261016T111703Z-2123456789abcdef')
    engine.records = [rec(1), rec(2), rec(5)]
    with pytest.raises(RuntimeError, match='archive_content_lost'):
        v.run(config, client=cloud, engine=engine)


# spec: ANYM-728
def test_the_rebaseline_reason_survives_a_crash_after_adoption(recordings_operator, monkeypatch):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, _ = recordings_operator
    v.run(config, client=cloud, engine=engine)
    receive(receiver, cloud, bid=THIRD)
    engine.options = {'archive_id': 'arch-fresh-0003'}
    reason = 'owner re-initialized the archive'
    assert v.request_rebaseline(config, reason)['status'] == 'rebaseline_requested'
    original = v.record_verified
    calls = []

    def crashing(*args, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('synthetic crash before the success record')
        return original(*args, **kw)
    monkeypatch.setattr(v, 'record_verified', crashing)
    with pytest.raises(OSError, match='synthetic'):
        v.run(config, client=cloud, engine=engine)
    baseline = json.loads((Path(config['directory']) / 'continuity.json').read_text())
    assert baseline['rebaselined'] == reason and not (Path(config['directory']) / 'rebaseline.json').exists()
    assert v.run(config, client=cloud, engine=engine)['rebaselined'] == reason
    assert json.loads((Path(config['directory']) / 'last-success.json').read_text())['rebaselined'] == reason


# spec: ANYM-728
def test_adoption_without_pending_or_adopted_facts_fails_closed(recordings_operator):
    v, config, *_ = recordings_operator
    with v.verifier_lock(config['directory']) as fd:
        with pytest.raises(ValueError, match='continuity_unresolved'):
            v.adopt_continuity(fd, 'a' * 32)


# spec: ANYM-728
def test_a_new_archive_is_refused_until_the_operator_rebaselines_with_a_reason(recordings_operator, capsys):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, write_json = recordings_operator
    v.run(config, client=cloud, engine=engine)
    marker = Path(config['protected_file']).read_bytes()
    receive(receiver, cloud, bid=THIRD)
    engine.options = {'archive_id': 'arch-fresh-0003'}
    with pytest.raises(RuntimeError, match='archive_identity_changed'):
        v.run(config, client=cloud, engine=engine)
    assert Path(config['protected_file']).read_bytes() == marker
    path = Path(config['receiver_config_file']).parent / 'verifier.json'
    write_json(path, config)
    with pytest.raises(SystemExit):
        v.main(['--rebaseline', '--config', str(path)])  # a reason is required
    assert v.main(['--rebaseline', '--config', str(path), '--reason', 'owner re-initialized the archive']) == 0
    assert 'rebaseline_requested' in capsys.readouterr().out
    result = v.run(config, client=cloud, engine=engine)
    assert result['status'] == 'verified' and result['rebaselined'] == 'owner re-initialized the archive'
    assert not (Path(config['directory']) / 'rebaseline.json').exists()
    assert json.loads((Path(config['directory']) / 'continuity.json').read_text())['facts']['archive']['archive_id'] == 'arch-fresh-0003'


# spec: ANYM-728
def test_an_enrolled_point_without_a_baseline_is_refused_not_trusted(recordings_operator):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, _ = recordings_operator
    v.run(config, client=cloud, engine=engine)
    (Path(config['directory']) / 'continuity.json').unlink()
    receive(receiver, cloud, bid=THIRD)
    with pytest.raises(RuntimeError, match='archive_baseline_missing'):
        v.run(config, client=cloud, engine=engine)


# spec: ANYM-728
def test_deletion_with_a_tombstone_rotates_and_the_tombstone_joins_the_baseline(recordings_operator):
    from test_backup_r2 import receive, THIRD
    v, config, receiver, cloud, engine, _ = recordings_operator
    v.run(config, client=cloud, engine=engine)
    receive(receiver, cloud, bid=THIRD)
    engine.records = [rec(2)]
    engine.options = {'tombstones': [tomb(1)], 'user_version': 5, 'events': 3}
    assert v.run(config, client=cloud, engine=engine)['backup_id'] == THIRD
    assert json.loads((Path(config['directory']) / 'continuity.json').read_text())['facts']['tombstones'] == [tomb(1)]


# spec: ANYM-728
def test_proofs_are_bound_to_their_profile_and_the_recordings_checks_never_mix():
    from test_backup_r2 import BID
    v = module()
    receipt = {'wire': {'id': BID, 'bytes': 100, 'sha256': 'a' * 64, 'snapshot_sha256': 'b' * 64,
                        'received_at': '2026-10-16T09:17:03+00:00'}}
    archive_facts = {'archive_id': 'arch-x', 'incarnation': 1, 'sequence': 3, 'records': 2}
    facts = {'v': 1, 'status': 'verified', 'profile': 'recordings', 'schema_version': 4, 'manifest_sha256': 'b' * 64,
             'checks': {k: True for k in r2.RECORDINGS_CHECKS}, 'archive': archive_facts, 'captured_at': CAPTURED}
    proof = v.make_proof(receipt, facts, 'c' * 32, 'recordings')
    assert r2.validate_proof(proof, receipt, 'recordings')
    for other in ('messaging', 'calendar'):
        with pytest.raises(ValueError, match='verifier_profile_mismatch'):
            r2.validate_proof(proof, receipt, other)
    with pytest.raises(ValueError, match='verifier_profile_mismatch'):
        v.make_proof(receipt, facts, 'c' * 32)
    for bad in ({'checks': {k: True for k in r2.CHECKS}}, {'checks': {k: True for k in r2.RECORDINGS_CHECKS | r2.CHECKS}},
                {'archive': {**archive_facts, 'text': 'x'}}, {'archive': {**archive_facts, 'incarnation': 0}},
                {'archive': {**archive_facts, 'archive_id': 'bad id'}}, {'schema_version': 6},
                {'captured_at': 'yesterday'}, {'captured_at': '2026-11-03T21:30:00'}):
        with pytest.raises(ValueError, match='invalid_helper_result'):
            v.make_proof(receipt, {**facts, **bad}, 'c' * 32, 'recordings')
    stripped = {**proof, 'verification': {k: v_ for k, v_ in proof['verification'].items() if k != 'archive'}}
    with pytest.raises(ValueError, match='independent_restore_proof_required'):
        r2.validate_proof(stripped, receipt, 'recordings')


# spec: ANYM-728
def test_a_recordings_receiver_and_verifier_must_agree_on_the_profile(operator):  # noqa: F811  (the imported fixture)
    v, config, receiver, cloud, engine, write_json = operator
    path = Path(config['receiver_config_file']).parent / 'verifier.json'
    write_json(path, {**config, 'profile': 'recordings'})
    with pytest.raises(ValueError, match='verifier_profile_mismatch'):
        v.load_config(path)
    write_json(Path(config['receiver_config_file']), {**receiver, 'profile': 'recordings'})
    assert v.load_config(path)['profile'] == 'recordings'


# --- scratch lifecycle (criterion 4) -----------------------------------------------------------------------------

def scratch(config):
    return sorted(p.name for p in Path(config['directory']).iterdir() if p.name.startswith(('payload-', 'secrets-')))


# spec: ANYM-728
def test_the_verifier_scratch_holds_the_plaintext_0700_and_is_removed_after_success(recordings_operator):
    v, config, receiver, cloud, engine, _ = recordings_operator
    engine.plaintext = b'SYNTHETIC_PLAINTEXT_ARCHIVE'
    seen = []
    restore = engine.restore

    def spy(config, receiver, client, receipt, run, payload, secret_dir):
        result = restore(config, receiver, client, receipt, run, payload, secret_dir)
        seen.append((payload.stat().st_mode & 0o777, (payload / 'archive.sqlite').exists()))
        return result
    engine.restore = spy
    assert v.run(config, client=cloud, engine=engine)['status'] == 'verified'
    assert seen == [(0o700, True)] and scratch(config) == []


# spec: ANYM-728
def test_the_verifier_scratch_is_removed_after_a_failure_and_at_a_restart(recordings_operator):
    v, config, receiver, cloud, engine, _ = recordings_operator
    engine.plaintext = b'SYNTHETIC_PLAINTEXT_ARCHIVE'
    engine.records = [rec(1)]
    engine.failure = None
    restore = engine.restore

    def failing(*args):
        restore(*args)
        raise v.HelperFailure('restore_archive_failed')
    engine.restore = failing
    with pytest.raises(RuntimeError, match='restore_archive_failed'):
        v.run(config, client=cloud, engine=engine)
    assert scratch(config) == [] and status(config)['cleanup_complete'] is True
    # A run killed mid-restore leaves its scratch and journal; the next run removes the scratch before anything else.
    journal = json.loads((Path(config['directory']) / 'active.json').read_text())
    left = Path(config['directory']) / ('payload-' + journal['run'])
    left.mkdir(mode=0o700)
    (left / 'archive.sqlite').write_bytes(b'LEFT_BEHIND')
    engine.restore = restore
    engine.records = [rec(1), rec(2)]
    assert v.run(config, client=cloud, engine=engine)['status'] == 'verified'
    assert not left.exists() and scratch(config) == []


# --- follow and the shared pins (criterion 3) -----------------------------------------------------------------------

@pytest.fixture
def recordings_instance(operator, tmp_path):  # noqa: F811  (the imported fixture)
    """A recordings verifier instance beside the operator's messaging one: its own receiver, state, key and marker."""
    from test_backup_r2 import Cloud, receive
    v, config, receiver, cloud, _, write_json = operator
    for name in ('rec-state', 'rec-control', 'rec-verifier'):
        (tmp_path / name).mkdir(mode=0o700)
    control = tmp_path / 'rec-control'
    write_json(control / 'credentials.json', {'access_key_id': 'e' * 32, 'secret_access_key': 'f' * 64})
    own = {**receiver, 'state_directory': str(tmp_path / 'rec-state'), 'credentials_file': str(control / 'credentials.json'),
           'prefix': 'recordings'}
    own_cloud = Cloud()
    receive(own, own_cloud)
    write_json(control / 'receiver.json', {**own, 'profile': 'recordings'})
    identity = control / 'identity'
    identity.write_text('SYNTHETIC_NOT_A_KEY')
    identity.chmod(0o600)
    recordings = {**config, 'profile': 'recordings', 'receiver_config_file': str(control / 'receiver.json'),
                  'directory': str(tmp_path / 'rec-verifier'), 'identity_file': str(identity),
                  'protected_file': str(control / 'protected.json')}
    path = control / 'verifier.json'
    write_json(path, recordings)
    messaging = Path(config['receiver_config_file']).parent / 'verifier.json'
    write_json(messaging, config)
    return path, recordings, messaging, write_json, own_cloud


# spec: ANYM-728
def test_the_pin_step_moves_messaging_calendar_and_recordings_pins_together(pins, recordings_instance):  # noqa: F811  (the imported fixture)
    s = pins
    path, _, _, write_json, _ = recordings_instance
    s.paths = [*s.paths, path]
    write_json(s.v.INSTANCES_FILE, {'v': 1, 'verifier_configs': [str(p) for p in s.paths]})
    assert s.update()['status'] == 'pinned'
    assert s.pairs() == [(NEW_IMAGE, HEAD)] * 3
    assert sorted(profile for profile, _ in s.tested) == ['calendar', 'messaging', 'recordings']


# spec: ANYM-728
def test_a_recordings_only_helper_failure_holds_every_pin_with_one_fixed_code(pins, recordings_instance):  # noqa: F811  (the imported fixture)
    s = pins
    path, _, _, write_json, _ = recordings_instance
    s.paths = [*s.paths, path]
    write_json(s.v.INSTANCES_FILE, {'v': 1, 'verifier_configs': [str(p) for p in s.paths]})
    before = [p.read_bytes() for p in s.paths]
    s.failures['recordings'] = RuntimeError('anything the old helper said: /private/path')
    with pytest.raises(RuntimeError, match='recordings_helper_failed') as held:
        s.update()
    assert s.f.failure_code(held.value) == 'recordings_helper_failed'
    assert [p.read_bytes() for p in s.paths] == before and not (s.host / 'pins-switch.json').exists()


# spec: ANYM-728
def test_follow_holds_only_the_shared_pin_move_for_a_recordings_failure_and_moves_all_pins_after(follower, recordings_instance):  # noqa: F811  (the imported fixture)
    s = follower
    path, _, messaging, write_json, _ = recordings_instance
    write_json(s.v.INSTANCES_FILE, {'v': 1, 'verifier_configs': [str(messaging), str(path)]})
    before = [p.read_bytes() for p in (messaging, path)]
    s.calendar_failure = RuntimeError('old helper cannot read a recordings bundle')
    with pytest.raises(RuntimeError, match='recordings_helper_failed'):
        s.run()
    assert [p.read_bytes() for p in (messaging, path)] == before
    assert follow_status(s)['error'] == 'recordings_helper_failed'
    s.calendar_failure = None
    assert s.run()['status'] == 'switched'
    assert [(c['helper_image'], c['helper_revision']) for c in map(s.v.load_config, (messaging, path))] == [(NEW_IMAGE, HEAD)] * 2
    assert 'recordings' in s.profiles
    # A rollback tests the kept helper on the recordings profile too, and holds all pins when it cannot read it.
    s.calendar_failure = RuntimeError('kept helper predates the recordings profile')
    with pytest.raises(RuntimeError, match='recordings_helper_failed'):
        s.revert()
    assert [(c['helper_image'], c['helper_revision']) for c in map(s.v.load_config, (messaging, path))] == [(NEW_IMAGE, HEAD)] * 2


# spec: ANYM-728
def test_the_follow_fixture_makes_a_bundle_the_recordings_helper_accepts(tmp_path):
    from messaging_service import backup_r2_follow as follow
    from messaging_service import backup_restore_worker as worker
    from messaging_service.backup_recordings import verify_recordings_bundle
    script = follow.RECORDINGS_FIXTURE_SCRIPT.replace("Path('/fixture')", f'Path({str(tmp_path / "fixture")!r})')
    script = script.replace("recipient=os.environ['DEEDKIT_SYNTHETIC_RECIPIENT']", 'recipient=None')
    script = script.replace("encrypt_snapshot(root/'snapshot',root/'ciphertext',recipient)", 'pass')
    exec(compile(script, 'fixture', 'exec'), {})
    snapshot = tmp_path / 'fixture' / 'snapshot'
    assert verify_recordings_bundle(snapshot)['archive']['records'] == 3
    assert (tmp_path / 'fixture' / 'manifest-sha256').read_text() == hashlib.sha256((snapshot / 'manifest.json').read_bytes()).hexdigest()
    work = tmp_path / 'work'
    work.mkdir()
    assert worker.restore_recordings(snapshot, 'a' * 64, work)['checks'] == {name: True for name in r2.RECORDINGS_CHECKS}


# --- one host, three profiles: failures stay in their own instance -----------------------------------------------------

@pytest.fixture
def trio(instances, recordings_instance, tmp_path):  # noqa: F811  (the imported fixtures)
    path, recordings, _, _, cloud = recordings_instance

    class Engine(HostEngine):
        records = [rec(1), rec(2)]

        def restore(self, config, receiver, client, receipt, run, payload, secret_dir):
            facts = super().restore(config, receiver, client, receipt, run, payload, secret_dir)
            if r2.profile(config) != 'recordings':
                return facts
            found = cont.archive_facts(cont.synthetic_archive(tmp_path / f'trio-{run}.sqlite', self.records))
            descriptor = os.open(payload / 'continuity.json', os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(descriptor, 'w') as out:
                out.write(json.dumps(found, separators=(',', ':')))
            return {'v': 1, 'status': 'verified', 'profile': 'recordings', 'schema_version': 4,
                    'manifest_sha256': receipt['wire']['snapshot_sha256'], 'checks': {k: True for k in r2.RECORDINGS_CHECKS},
                    'archive': found['archive'], 'captured_at': CAPTURED}
    instances.engine = Engine(instances.v, instances.engine.root, instances.host)
    instances.recordings = recordings
    instances.recordings_cloud = cloud
    return instances


# spec: ANYM-728
def test_a_recordings_refusal_never_fails_or_rotates_messaging_or_calendar_and_the_reverse(trio):
    from test_backup_r2 import receive, THIRD
    s = trio
    v = s.v
    runs = ((s.messaging, s.cloud), (s.calendar, s.calendar_cloud), (s.recordings, s.recordings_cloud))
    for config, cloud in runs:
        assert v.run(config, client=cloud, engine=s.engine)['status'] == 'verified'
    markers = [Path(config['protected_file']).read_bytes() for config, _ in runs]
    for config, cloud in runs:
        receive(r2.load_config(config['receiver_config_file']), cloud, bid=THIRD)
    s.engine.records = [rec(1, transcript=''), rec(2, summary='')]
    with pytest.raises(RuntimeError, match='archive_content_lost'):
        v.run(s.recordings, client=s.recordings_cloud, engine=s.engine)
    assert Path(s.recordings['protected_file']).read_bytes() == markers[2]
    for index, (config, cloud) in enumerate(runs[:2]):
        assert v.run(config, client=cloud, engine=s.engine)['backup_id'] == THIRD
        assert Path(config['protected_file']).read_bytes() != markers[index]
    # The reverse: a messaging failure on the next candidate does not touch the recordings point, which then rotates.
    receive(r2.load_config(s.messaging['receiver_config_file']), s.cloud, bid='20261016T111703Z-2123456789abcdef')
    messaging_marker = Path(s.messaging['protected_file']).read_bytes()
    s.engine.failure = RuntimeError('restore_docker_failed')
    with pytest.raises(RuntimeError, match='restore_docker_failed'):
        v.run(s.messaging, client=s.cloud, engine=s.engine)
    s.engine.failure = None
    s.engine.records = [rec(1), rec(2), rec(3)]
    assert Path(s.messaging['protected_file']).read_bytes() == messaging_marker
    assert v.run(s.recordings, client=s.recordings_cloud, engine=s.engine)['backup_id'] == THIRD


# spec: ANYM-728
def test_rollback_with_a_pending_recordings_bundle_and_a_protected_point_keeps_a_reader_for_existing_bundles(follower, trio, recordings_instance):  # noqa: F811
    from test_backup_r2 import receive, THIRD
    s, t = follower, trio
    path, recordings, messaging, write_json, cloud = recordings_instance
    write_json(s.v.INSTANCES_FILE, {'v': 1, 'verifier_configs': [str(messaging), str(path)]})
    # A protected recordings point, then a pending (uploaded, unverified) bundle.
    assert t.v.run(t.recordings, client=cloud, engine=t.engine)['status'] == 'verified'
    marker = Path(t.recordings['protected_file']).read_bytes()
    receive(r2.load_config(t.recordings['receiver_config_file']), cloud, bid=THIRD)
    assert s.run()['status'] == 'switched'
    assert [(c['helper_image'], c['helper_revision']) for c in map(s.v.load_config, (messaging, path))] == [(NEW_IMAGE, HEAD)] * 2
    # The kept helper cannot read the recordings bundle: the rollback holds every pin and the point is untouched.
    s.calendar_failure = RuntimeError('kept helper predates the recordings profile')
    with pytest.raises(RuntimeError, match='recordings_helper_failed'):
        s.revert()
    assert [(c['helper_image'], c['helper_revision']) for c in map(s.v.load_config, (messaging, path))] == [(NEW_IMAGE, HEAD)] * 2
    assert Path(t.recordings['protected_file']).read_bytes() == marker
    # The reader that stays can still verify the pending bundle, which then replaces the protected point.
    t.engine.records = [rec(1), rec(2), rec(3)]
    assert t.v.run(s.v.load_config(path), client=cloud, engine=t.engine)['backup_id'] == THIRD
    assert Path(t.recordings['protected_file']).read_bytes() != marker
    # A rollback that the kept helper passes moves every pin back, and the rolled-back reader verifies the next bundle.
    s.calendar_failure = None
    assert s.revert()['status'] == 'reverted'
    rolled_back = s.v.load_config(path)
    assert (rolled_back['helper_image'], rolled_back['helper_revision']) == (t.recordings['helper_image'], t.recordings['helper_revision'])
    receive(r2.load_config(t.recordings['receiver_config_file']), cloud, bid='20261016T111703Z-2123456789abcdef')
    t.engine.records = [rec(1), rec(2), rec(3), rec(4)]
    assert t.v.run(rolled_back, client=cloud, engine=t.engine)['status'] == 'verified'
