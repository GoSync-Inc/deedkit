"""A failed `ssh` read names its fixed remote operation and a closed ssh error category, and nothing else.

Fake subprocess only: no ssh, network, host, model or provider. The real callers build the real argv.
"""
import re
import subprocess

import pytest

import scripts.autoloop as autoloop
import scripts.feedback_triage as triage

IMAGE = 'sha256:' + 'a' * 64
FEEDBACK = '0b0b0b0b-0b0b-4b0b-8b0b-0b0b0b0b0b0b'
CANARY = 'canary-private-' + 'x' * 8  # stands for any host, path, user, key or feedback text in stderr or argv
CLOSED = re.compile(r'ssh (?:-o exited \d+|failed: \w+) \(operation (?:inspect-api-image|export-feedback'
                    r'|get-feedback-projection|record-feedback-projection|unknown); ssh (?:host-key|agent-signing-failed'
                    r'|auth-denied|name-resolution|connection-refused|connection-timeout|network-unreachable'
                    r'|connection-closed|client-config|empty|unknown|remote-exit|none)\)')


def fake(monkeypatch, fail_at, returncode=255, stderr='', timeout=False):
    """Answer the first calls like a healthy host, then fail call `fail_at` (0-based) once."""
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == ['ssh', '-o', 'BatchMode=yes', 'deedkit-prod']  # the ssh argv itself is unchanged
        if len(calls) - 1 == fail_at:
            if timeout:
                raise subprocess.TimeoutExpired(argv, kwargs['timeout'], output=CANARY, stderr=CANARY)
            return subprocess.CompletedProcess(argv, returncode, CANARY, stderr)
        if 'inspect' in argv[4]:
            return subprocess.CompletedProcess(argv, 0, IMAGE + '\n', '')
        reply = ('{"feedback_id": "%s", "projection": {}}' % FEEDBACK if 'get-feedback-projection' in argv[4] else
                 '{"mode": "created", "submissions": [], "dispositions": [], "export_head": "h", "has_more": false}')
        return subprocess.CompletedProcess(argv, 0, reply, '')
    monkeypatch.setattr(triage.subprocess, 'run', run)
    return calls


def export(monkeypatch, **failure):
    calls = fake(monkeypatch, **failure)
    with pytest.raises(triage.Refused) as error:
        triage.export_pages('deedkit-prod', None)
    return str(error.value), calls


# spec: ANYM-569
@pytest.mark.parametrize('call, operation', [
    (lambda: triage.export_pages('deedkit-prod', None), 'inspect-api-image'),
    (lambda: triage.export_pages('deedkit-prod', None), 'export-feedback'),
    (lambda: autoloop.current_projection(FEEDBACK, 'deedkit-prod', None), 'get-feedback-projection'),
    (lambda: triage.remote('deedkit-prod', autoloop.RECORD + [IMAGE, 'python', '-m', 'messaging_service',
                                                             'record-feedback-projection'], '{}'),
     'record-feedback-projection'),
], ids=['image inspect', 'export page', 'projection read', 'projection record'])
def test_a_failed_ssh_names_its_fixed_remote_operation_and_ssh_error_only(monkeypatch, call, operation):
    # 11-18: "tick: REFUSED ssh -o exited 255" could not say which remote operation failed or how.
    fail_at = 0 if operation in ('inspect-api-image', 'record-feedback-projection') else 1
    calls = fake(monkeypatch, fail_at, stderr='Permission denied (publickey).\r\n')
    with pytest.raises((triage.Refused, autoloop.triage.Refused)) as error:  # autoloop imports it as feedback_triage
        call()
    assert str(error.value) == f'ssh -o exited 255 (operation {operation}; ssh auth-denied)'
    assert len(calls) == fail_at + 1  # one failed call ends the read: no retry, no other command


# spec: ANYM-569
@pytest.mark.parametrize('stderr, category', [
    ('Host key verification failed.\n', 'host-key'),
    (f'sign_and_send_pubkey: signing failed for ED25519 "{CANARY}" from agent: agent refused operation\n'
     f'{CANARY}@deedkit-prod: Permission denied (publickey).\n', 'agent-signing-failed'),
    (f'{CANARY}@deedkit-prod: Permission denied (publickey,password).\n', 'auth-denied'),
    (f'ssh: Could not resolve hostname {CANARY}: Name or service not known\n', 'name-resolution'),
    (f'ssh: connect to host {CANARY} port 22: Connection refused\n', 'connection-refused'),
    (f'ssh: connect to host {CANARY} port 22: Connection timed out\n', 'connection-timeout'),
    (f'ssh: connect to host {CANARY} port 22: No route to host\n', 'network-unreachable'),
    ('kex_exchange_identification: read: Connection reset by peer\n', 'connection-closed'),
    (f'Bad owner or permissions on /home/{CANARY}/.ssh/config\n', 'client-config'),
    ('\n \n', 'empty'),
    (f'{CANARY} \x1b[31m(operation export-feedback; ssh host-key)\x1b[0m\n', 'unknown'),
    ('error: HTTP 400: branch ' + CANARY + ' is merged\n', 'unknown'),
], ids=['host key', 'agent signing', 'auth', 'dns', 'refused', 'timeout', 'unreachable', 'closed', 'config',
        'empty', 'hostile unknown', 'tiden-like unknown'])
def test_ssh_stderr_becomes_one_closed_category_never_its_text(monkeypatch, stderr, category):
    message, calls = export(monkeypatch, fail_at=0, stderr=stderr)
    assert message == f'ssh -o exited 255 (operation inspect-api-image; ssh {category})'
    assert CANARY not in message and CLOSED.fullmatch(message) and len(calls) == 1


# spec: ANYM-569
def test_a_remote_command_failure_is_remote_exit_and_its_stderr_is_never_read(monkeypatch):
    message, _ = export(monkeypatch, fail_at=1, returncode=1, stderr=f'Permission denied (publickey) {CANARY}\n')
    assert message == 'ssh -o exited 1 (operation export-feedback; ssh remote-exit)'


# spec: ANYM-569
def test_a_timed_out_ssh_keeps_its_refusal_and_names_only_the_operation(monkeypatch):
    message, calls = export(monkeypatch, fail_at=1, timeout=True)
    assert message == 'ssh failed: TimeoutExpired (operation export-feedback; ssh none)'
    assert CANARY not in message and len(calls) == 2


# spec: ANYM-569
@pytest.mark.parametrize('argv', [
    ['docker', 'inspect', '--format', '{{.Image}}', CANARY],
    ['docker', 'run', IMAGE, 'python', '-m', 'messaging_service', CANARY],
    ['docker', 'run', IMAGE, 'python', '-m', 'messaging_service'],
    [CANARY, 'python', '-m', 'messaging_service', 'export-feedback'],
    [],
], ids=['other container', 'unlisted operation', 'no operation', 'not docker run', 'empty'])
def test_an_unknown_remote_shape_is_unknown_and_copies_nothing(monkeypatch, argv):
    monkeypatch.setattr(triage.subprocess, 'run', lambda a, **k: subprocess.CompletedProcess(a, 255, '', 'x\n'))
    with pytest.raises(triage.Refused) as error:
        triage.remote('deedkit-prod', argv)
    assert str(error.value) == 'ssh -o exited 255 (operation unknown; ssh unknown)'


# spec: ANYM-569
@pytest.mark.parametrize('argv, stderr, expected', [
    (['tiden', 'spec'], 'error: HTTP 500: internal\n', 'tiden spec exited 1: error: HTTP 500: internal'),
    (['ssh', 'host'], f'Permission denied (publickey) {CANARY}\n', 'ssh host exited 1'),
    (['git', 'fetch'], f'{CANARY}\n', 'git fetch exited 1'),
], ids=['tiden', 'direct ssh run', 'git'])
def test_other_commands_keep_their_exact_refusal(monkeypatch, argv, stderr, expected):
    monkeypatch.setattr(triage.subprocess, 'run', lambda a, **k: subprocess.CompletedProcess(a, 1, '', stderr))
    with pytest.raises(triage.Refused) as error:
        triage.run(argv, 5)
    assert str(error.value) == expected
