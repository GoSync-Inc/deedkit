"""A delivery the receiver refuses as busy (its own verifier holds the upload lock) waits instead of failing."""
import json

import pytest

from messaging_service import backup_runner
from messaging_service.backup_runner import read_json
from test_backup_operations import runner_fixture
from test_backup_streaming import streaming_runner


def fake_time(runner):
    """A clock only the runner's sleeps move; returns the list of sleeps longer than the capture poll."""
    now, waits = [0.0], []
    def sleep(seconds):
        now[0] += seconds
        if seconds >= 1:
            waits.append(seconds)
    runner.clock, runner.sleep = (lambda: now[0]), sleep
    return waits


def busy_stream(answers, attempts, *, after_start=False):
    async def send(path, metadata, transfer, timeout, start, completed, stopped=None):
        attempts.append(dict(metadata))
        if after_start:
            start(True)
        if len(attempts) <= answers:
            raise RuntimeError('receiver_busy')
        if not after_start:
            start(True)
        result = await completed()
        return {**result, 'status': 'stored', 'duplicate': False}
    return send


# spec: ANYM-489
def test_a_stream_refused_as_busy_is_offered_again_with_the_same_capture_until_stored(tmp_path):
    attempts = []
    runner, docker = streaming_runner(tmp_path, busy_stream(3, attempts))
    waits = fake_time(runner)
    runner.run()
    # Three minutes of waiting exceed the 60 s capture timeout: the deadline never counts the receiver's wait.
    assert waits == [backup_runner.BUSY_RETRY_SECONDS] * 3
    assert len(attempts) == 4 and all(attempt == attempts[0] for attempt in attempts)
    assert sum(call == ('stop', '--time', '25', 'tg-id') for call in docker.calls) == 1
    assert read_json(tmp_path / 'status.json')['state'] == 'ok' and not (tmp_path / 'journal.json').exists()


# spec: ANYM-489
def test_a_receiver_still_busy_at_the_bound_fails_the_run_and_keeps_the_capture_for_a_retry(tmp_path):
    attempts = []
    runner, docker = streaming_runner(tmp_path, busy_stream(100, attempts))
    runner.cfg['busy_wait_seconds'] = 180
    waits = fake_time(runner)
    with pytest.raises(RuntimeError, match='receiver_busy'):
        runner.run()
    assert sum(waits) <= 180 and len(attempts) == 4
    assert read_json(tmp_path / 'status.json')['error_code'] == 'receiver_busy'
    assert read_json(tmp_path / 'journal.json')['phase'] == 'stream_upload'
    assert docker.containers['tg-id']['State']['Running']


# spec: ANYM-489
def test_a_busy_answer_after_the_stream_started_is_not_retried(tmp_path):
    attempts = []
    runner, _ = streaming_runner(tmp_path, busy_stream(1, attempts, after_start=True))
    waits = fake_time(runner)
    with pytest.raises(RuntimeError, match='receiver_busy'):
        runner.run()
    assert len(attempts) == 1 and waits == []


# spec: ANYM-489
@pytest.mark.parametrize('bound', [0, None])
def test_only_receiver_busy_waits_and_a_zero_bound_disables_the_wait(tmp_path, bound):
    attempts = []
    async def send(path, metadata, transfer, timeout, start, completed, stopped=None):
        attempts.append(1)
        raise RuntimeError('receiver_busy' if bound == 0 else 'receiver_quota')
    runner, _ = streaming_runner(tmp_path, send)
    if bound is not None:
        runner.cfg['busy_wait_seconds'] = bound
    waits = fake_time(runner)
    with pytest.raises(RuntimeError):
        runner.run()
    assert len(attempts) == 1 and waits == []


# spec: ANYM-489
def test_a_staged_bundle_refused_as_busy_is_delivered_once_the_receiver_is_free(tmp_path):
    attempts = []
    async def send(path, result, transfer, timeout):
        attempts.append((result['id'], result['sha256']))
        if len(attempts) == 1:
            raise RuntimeError('receiver_busy')
        return {**result, 'status': 'stored', 'duplicate': False}
    runner, _ = runner_fixture(tmp_path, send)
    waits = fake_time(runner)
    runner.run()
    assert waits == [backup_runner.BUSY_RETRY_SECONDS] and attempts[0] == attempts[1]
    assert read_json(tmp_path / 'status.json')['state'] == 'ok'


# spec: ANYM-489
@pytest.mark.parametrize('value,valid', [(None, True), (0, True), (7200, True), (-1, False), (7201, False),
                                         ('60', False), (True, False)])
def test_the_busy_wait_bound_is_an_optional_whole_number_of_seconds(tmp_path, value, valid):
    runner, _ = runner_fixture(tmp_path)
    transfer = {'host': 'backup.example.invalid', 'user': 'posbackup', 'identity_file': '/private/delivery_ed25519',
                'known_hosts_file': '/private/known_hosts'}
    cfg = {**runner.cfg, 'transfer': transfer}
    if value is not None:
        cfg['busy_wait_seconds'] = value
    path = tmp_path / 'runner.json'
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    if valid:
        assert backup_runner.config(path) == cfg
    else:
        with pytest.raises(ValueError, match='invalid_backup_timeout'):
            backup_runner.config(path)


# spec: ANYM-489
@pytest.mark.parametrize('after', [30, None])
def test_the_capture_deadline_counts_neither_busy_answers_nor_waits_but_still_ends_a_stuck_worker(tmp_path, after):
    attempts = []
    runner, docker = streaming_runner(tmp_path, busy_stream(3, attempts))
    now = [0.0]
    runner.clock = lambda: now[0]
    def sleep(seconds):
        now[0] += seconds
    runner.sleep = sleep
    # The worker is told to start only by the stored offer, after three minutes of busy answers (timeout 60 s).
    original = docker.inspect
    def inspect(cid):
        info = original(cid)
        if info and cid.startswith('pos-backup-') and (tmp_path / info['Config']['Labels']['personal-os.backup.id']
                                                       / 'result.json').exists():
            stored.setdefault('at', now[0])
            if after is None or now[0] < stored['at'] + after:
                info['State'].update(Running=True, Status='running')
        return info
    stored = {}
    docker.inspect = inspect
    if after is None:
        with pytest.raises(RuntimeError, match='backup_capture_timeout'):
            runner.run()
        assert 3 * backup_runner.BUSY_RETRY_SECONDS + 60 < now[0] <= 3 * backup_runner.BUSY_RETRY_SECONDS + 61
    else:
        runner.run()
        assert read_json(tmp_path / 'status.json')['state'] == 'ok' and len(attempts) == 4


# spec: ANYM-489
def test_no_offer_starts_after_the_bound_and_every_refused_offer_is_journaled(tmp_path, capsys):
    attempts, now = [], [0.0]
    async def send(path, metadata, transfer, timeout, start, completed, stopped=None):
        attempts.append(now[0])
        now[0] += 50  # each busy answer takes 50 s
        raise RuntimeError('receiver_busy')
    runner, _ = streaming_runner(tmp_path, send)
    runner.cfg['busy_wait_seconds'] = 180
    runner.clock = lambda: now[0]
    def sleep(seconds):
        now[0] += seconds
    runner.sleep = sleep
    with pytest.raises(RuntimeError, match='receiver_busy'):
        runner.run()
    first = attempts[0]
    assert len(attempts) == 2 and all(start - first <= 180 for start in attempts)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if 'receiver_busy' in line]
    assert [line['attempt'] for line in lines] == [1]
    assert lines[0] == {'status': 'waiting', 'code': 'receiver_busy', 'id': runner.journal['id'], 'attempt': 1,
                        'retry_seconds': backup_runner.BUSY_RETRY_SECONDS}


# spec: ANYM-489
def test_a_sigterm_during_the_busy_wait_keeps_the_capture_and_the_next_run_stores_it(tmp_path):
    attempts, seen = [], []
    runner, docker = streaming_runner(tmp_path, busy_stream(1, attempts))
    def sleep(seconds):
        if seconds == backup_runner.BUSY_RETRY_SECONDS:
            seen.append((read_json(tmp_path / 'status.json')['state'], docker.containers['tg-id']['State']['Running']))
            raise InterruptedError('backup_interrupted')
    runner.sleep = sleep
    with pytest.raises(InterruptedError):
        runner.run()
    assert seen == [('uploading', True)]
    runner.recover_stop()
    assert read_json(tmp_path / 'journal.json')['phase'] == 'stream_upload'
    runner.run()
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert sum(call == ('stop', '--time', '25', 'tg-id') for call in docker.calls) == 1
    assert read_json(tmp_path / 'status.json')['state'] == 'ok'
