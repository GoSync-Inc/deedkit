"""Recordings egress admission (ANYM-712): the service refuses to bind its port unless the host rules are in force.

Local sockets are the controls: a listener that answers stands for a blocked target that is reachable (rules missing), a
closed port stands for a refusal (which a DROP never sends), and a documentation address with no listener stands for the
timeout a working rule gives. Nothing here touches a provider or a firewall.
"""
from pathlib import Path
import socket
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/recordings/src'))

from deedkit_recordings import admission, cli  # noqa: E402

SILENT = admission.CANARY  # 192.0.2.254:9, TEST-NET-1: never routable, no host answers, as for a dropped target
REQUIRED = 'required'


@pytest.fixture
def listener():
    server = socket.create_server(('127.0.0.1', 0), backlog=8)
    server.settimeout(0.2)
    stop = threading.Event()

    def accept():
        while not stop.is_set():
            try:
                server.accept()[0].close()
            except OSError:
                pass
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f'127.0.0.1:{server.getsockname()[1]}'
    stop.set()
    thread.join(2)
    server.close()


@pytest.fixture
def resolver():
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp.bind(('127.0.0.1', 0))
    udp.settimeout(0.2)
    stop = threading.Event()

    def answer():
        while not stop.is_set():
            try:
                data, peer = udp.recvfrom(512)
                udp.sendto(data[:2] + b'\x81\x80', peer)
            except OSError:
                pass
    thread = threading.Thread(target=answer, daemon=True)
    thread.start()
    yield f'dns:127.0.0.1:{udp.getsockname()[1]}'
    stop.set()
    thread.join(2)
    udp.close()


def environ(blocked, opened, mode=REQUIRED):
    values = {'RECORDINGS_EGRESS_ADMISSION': mode}
    if blocked is not None:
        values['RECORDINGS_EGRESS_BLOCKED'] = blocked
    if opened is not None:
        values['RECORDINGS_EGRESS_OPEN'] = opened
    return values


# spec: ANYM-712
def test_admission_passes_with_the_rules_emulated(listener, resolver):
    # Blocked: nothing answers. Open: a public-443 stand-in connects and the resolver answers.
    assert admission.failures(environ(SILENT, f'{listener},{resolver}'), timeout=0.5) == []
    admission.require(environ(SILENT + ',dns:192.0.2.1', f'{listener},{resolver}'), timeout=0.5)


# spec: ANYM-712
def test_admission_fails_when_a_blocked_target_answers_or_refuses_or_an_open_target_does_not(listener, resolver):
    reasons = admission.failures(environ(f'{SILENT},{listener}', f'{listener},{resolver}'), timeout=0.5)
    assert reasons == [f'blocked target {listener}: connected']
    # A refusal is an answer: a DROP rule never sends one, so the rules are not in force.
    with socket.socket() as closed:
        closed.bind(('127.0.0.1', 0))
        refused = f'127.0.0.1:{closed.getsockname()[1]}'
    assert admission.failures(environ(f'{SILENT},{refused}', f'{listener},{resolver}'), timeout=0.5) == [f'blocked target {refused}: refused']
    assert admission.failures(environ(f'{SILENT},dns:{resolver.removeprefix("dns:")}', f'{listener},{resolver}'), timeout=0.5) == [
        f'blocked target dns:{resolver.removeprefix("dns:")}: answered']
    # An open target that gets no answer or a malformed target fails too.
    assert admission.failures(environ(SILENT, f'{listener},{SILENT}'), timeout=0.5) == [f'open target {SILENT}: timeout']
    assert admission.failures(environ(f'{SILENT},not-an-address:443', listener), timeout=0.5) == ['blocked target not-an-address:443: malformed']
    with pytest.raises(SystemExit) as stop:
        admission.require(environ(listener, listener), timeout=0.5)
    assert stop.value.code == admission.EXIT_CODE == 78


# spec: ANYM-712
@pytest.mark.parametrize('blocked, opened, missing', [(None, '127.0.0.1:9', 'BLOCKED'), (SILENT, None, 'OPEN'), ('', ' , ', 'BLOCKED'),
                                                     (None, None, 'BLOCKED')])
def test_admission_fails_closed_when_a_variable_is_missing_or_empty(blocked, opened, missing, capsys):
    reasons = admission.failures(environ(blocked, opened), timeout=0.2)
    assert reasons and any(f'RECORDINGS_EGRESS_{missing} is missing' in reason for reason in reasons)
    with pytest.raises(SystemExit) as stop:
        admission.require(environ(blocked, opened), timeout=0.2)
    assert stop.value.code == 78 and 'is missing' in capsys.readouterr().err


# spec: ANYM-712
def test_admission_refuses_to_start_without_the_raw_layer_when_the_filter_layer_alone_answers(listener, monkeypatch):
    """A boot where inference failed: ufw loaded the filter rules, the raw rules were skipped. The canary then gets a
    refusal (the filter chain REJECTs it) instead of the timeout the raw chain's DROP gives, and the start is refused."""
    def host(canary_outcome):
        def tcp(target, timeout, literal):
            return canary_outcome if target == admission.CANARY else ('timeout' if literal else 'connected')
        return tcp
    other_blocked = '172.18.0.1:8080'
    values = environ(f'{other_blocked},{admission.CANARY}', listener)
    monkeypatch.setattr(admission, '_tcp', host('refused'))  # raw missing, filter refusing
    assert admission.failures(values, timeout=0.5) == [f'blocked target {admission.CANARY}: refused']
    with pytest.raises(SystemExit) as stop:
        admission.require(values, timeout=0.5)
    assert stop.value.code == 78
    monkeypatch.setattr(admission, '_tcp', host('timeout'))  # raw present: the DROP gives a timeout
    assert admission.failures(values, timeout=0.5) == []
    # The canary cannot be left out of the list: fail closed.
    assert admission.failures(environ(other_blocked, listener), timeout=0.5) == [
        f'RECORDINGS_EGRESS_BLOCKED must name the canary {admission.CANARY}']


# spec: ANYM-712
@pytest.mark.parametrize('mode', ['', 'optional', 'off', 'Required'])
def test_admission_is_skipped_when_the_variable_is_not_required(mode, listener):
    # Source runs and tests: nothing is probed, even with lists that would fail.
    assert admission.failures(environ(listener, None, mode)) == []
    assert admission.failures({}) == []
    admission.require({'RECORDINGS_EGRESS_ADMISSION': mode})


# spec: ANYM-712
def test_the_cli_runs_admission_before_it_binds_its_port_and_not_for_the_archive_backup(monkeypatch, tmp_path, listener):
    served = []
    uvicorn = type(sys)('uvicorn')
    uvicorn.run = lambda *args, **kwargs: served.append((args, kwargs))
    monkeypatch.setitem(sys.modules, 'uvicorn', uvicorn)
    monkeypatch.setattr('deedkit_recordings.app.create_app', lambda path: 'app')
    config = tmp_path / 'config.json'
    monkeypatch.setattr(sys, 'argv', ['deedkit-recordings', '--config', str(config)])
    monkeypatch.setenv('RECORDINGS_EGRESS_ADMISSION', REQUIRED)
    monkeypatch.setenv('RECORDINGS_EGRESS_BLOCKED', listener)  # reachable: the rules are not in force
    monkeypatch.setenv('RECORDINGS_EGRESS_OPEN', listener)
    with pytest.raises(SystemExit) as stop:
        cli.main()
    assert stop.value.code == 78 and served == []
    monkeypatch.setenv('RECORDINGS_EGRESS_BLOCKED', SILENT)
    cli.main()
    assert len(served) == 1 and served[0][1]['host'] == '127.0.0.1'
    # The admission variable unset (source run): the server starts with no probe.
    served.clear()
    monkeypatch.delenv('RECORDINGS_EGRESS_ADMISSION')
    monkeypatch.setenv('RECORDINGS_EGRESS_BLOCKED', listener)
    cli.main()
    assert len(served) == 1

