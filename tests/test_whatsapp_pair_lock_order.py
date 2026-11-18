"""ANYM-87: the real pairing command (whatsapp_pair) removes the browser profile's stale Singleton* symlinks while it
holds the account's session lock, before the store opens or the pairing child starts.

The command's own code runs: the owner-terminal check on a real pseudo-terminal, the private configuration and permit,
the real session flock and the real cleanup. The store, the archive lease and the child process entry are recording
fakes. The child is never executed: its executables are absolute paths that do not exist. So the "paired" result here
is the command's answer to a fake child that exited 0, never live pairing acceptance. No database, provider, browser,
Node, session or network is opened."""
import asyncio
import fcntl
import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from messaging_service import collector_state, session_claim, whatsapp_pair

SINGLETONS = ('SingletonLock', 'SingletonSocket', 'SingletonCookie')
NO_PID = 2**31 - 1  # the fake child's pid: no process group can have it, even if os.killpg were not faked


def lock_state(session):
    """The real exclusion: a fresh open file description of the session's lock file asks for the same flock."""
    try: fd = os.open(session.parent / (session.name + '.owner.lock'), os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError: return 'absent'
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return 'held'
        fcntl.flock(fd, fcntl.LOCK_UN)
        return 'free'
    finally: os.close(fd)


def second_opener(session):
    """What another opener of this session meets: the real session_lock, taken and released at once."""
    try:
        with collector_state.session_lock(session): return 'acquired'
    except RuntimeError as exc: return str(exc)


def tree(root):
    """Every entry under root, symlinks never followed: a link's text, a file's bytes and inode, a directory's mode."""
    entries = {}
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode): entries[str(path.relative_to(root))] = ('link', os.readlink(path))
            elif stat.S_ISDIR(info.st_mode): entries[str(path.relative_to(root))] = ('dir', stat.S_IMODE(info.st_mode))
            else: entries[str(path.relative_to(root))] = ('file', path.read_bytes(), info.st_ino, info.st_mtime_ns)
    return entries


def present(profile):
    return sorted(name for name in SINGLETONS if os.path.lexists(profile / name))


@pytest.fixture
def pairing(tmp_path, monkeypatch):
    """A private, synthetic browser-provider pairing setup and the recording fakes around the real command."""
    root = tmp_path / 'private'; root.mkdir(mode=0o700)
    sessions = root / 'wa'; sessions.mkdir(mode=0o700)
    session = sessions / 'whatsapp'; session.mkdir(mode=0o700)
    profile = session / 'session'; (profile / 'Default').mkdir(mode=0o700, parents=True)
    (profile / 'Local State').write_bytes(b'SYNTHETIC_LOCAL_STATE')
    (profile / 'Default' / 'Preferences').write_bytes(b'SYNTHETIC_PREFERENCES')
    (profile / 'RunningChromeVersion').symlink_to('0.0.0.0')  # a runtime link that is not a Singleton: kept
    (profile / 'Default' / 'SingletonLock').symlink_to('nested-not-runtime')  # only the profile root is cleaned
    targets = root / 'targets'; targets.mkdir(mode=0o700)
    (targets / 'lock-target').write_bytes(b'SYNTHETIC_TARGET')
    (targets / 'socket-target').mkdir(mode=0o700); (targets / 'socket-target' / 'inside').write_bytes(b'SYNTHETIC_INSIDE')
    links = {'SingletonLock': str(targets / 'lock-target'), 'SingletonSocket': str(targets / 'socket-target'),
             'SingletonCookie': str(root / 'gone-container')}  # the last one dangles, like a dead container's
    for name, target in links.items(): (profile / name).symlink_to(target)
    permit = root / 'permit.json'
    permit.write_text(json.dumps({'account': 'wa-pair-lock', 'expected_wid': '100000@c.us',
                                  'prior_clients_stopped': True}))
    config = root / 'config.json'
    config.write_text(json.dumps({'enabled': True, 'account': 'wa-pair-lock', 'expected_wid': '100000@c.us',
        'permit_file': str(permit), 'session_directory': str(session), 'events_directory': str(root / 'events'),
        'pages_directory': str(root / 'pages'), 'chromium_executable': '/synthetic/never-run/chromium',
        'node_executable': '/synthetic/never-run/node'}))
    permit.chmod(0o600); config.chmod(0o600)
    dsn = root / 'synthetic.dsn'; dsn.write_text('postgresql://synthetic.invalid/messaging_probe_test')
    monkeypatch.setenv('DATABASE_URL_FILE', str(dsn)); monkeypatch.setenv('PROBE_FILES', str(root / 'files'))
    monkeypatch.delenv('ANYMSG_WHATSAPP_ASSETS_DIR', raising=False)
    # The browser provider pairs only from an overlay without the claim mount (COLLECTORS.md): none here.
    monkeypatch.setattr(session_claim, 'LOCK_DIRECTORY', root / 'no-claim-mount')
    rec = SimpleNamespace(events=[], command=None, exit_code=0, synthetic_account=False, session=session,
                          profile=profile, targets=targets, links=links, config=config)

    class Cursor:
        def execute(self, *_): return self
        def fetchone(self): return {'platform': 'whatsapp', 'synthetic': rec.synthetic_account}

    class Pool:
        def connection(self):
            class Connection:
                def __enter__(self): return Cursor()
                def __exit__(self, *_): return False
            return Connection()

    class FakeStore:
        def __init__(self, dsn_text, files, base):
            rec.events.append(('store', lock_state(session), present(profile), second_opener(session)))
            self.pool = Pool()
        def close(self): rec.events.append(('store_close', lock_state(session)))

    class FakeArchive:
        def __init__(self, store): pass
        def acquire(self, account, owner): rec.events.append(('lease', lock_state(session))); return 7
        def renew(self, account, owner, epoch): pass
        def release(self, account, owner, epoch): rec.events.append(('lease_release', lock_state(session)))

    class FakeChild:
        pid = NO_PID
        returncode = None
        async def wait(self):
            self.returncode = rec.exit_code
            return rec.exit_code
        def terminate(self): rec.events.append(('fake_child_terminate',))

    async def fake_pair_child(*command, **kwargs):
        # Recorded, never executed: the stand-in for the provider's pairing process entry.
        inherited = os.fstat(kwargs['pass_fds'][0]).st_ino
        lock_file = os.stat(session.parent / (session.name + '.owner.lock')).st_ino
        rec.command = command
        rec.events.append(('fake_pair_child', lock_state(session), present(profile), inherited == lock_file))
        return FakeChild()

    def fake_killpg(pid, sig):
        rec.events.append(('fake_killpg', pid))
        raise ProcessLookupError

    real_unlink = os.unlink
    def unlink(path, *, dir_fd=None):
        if dir_fd is not None and path in SINGLETONS:
            rec.events.append(('unlink', path, lock_state(session), os.readlink(path, dir_fd=dir_fd)))
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(whatsapp_pair, 'Store', FakeStore)
    monkeypatch.setattr(whatsapp_pair, 'Archive', FakeArchive)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', fake_pair_child)
    monkeypatch.setattr(os, 'killpg', fake_killpg)
    monkeypatch.setattr(os, 'unlink', unlink)
    master, slave = os.openpty()  # the owner's terminal is a real pseudo-terminal; nothing is written to it
    rec.terminal = (os.fdopen(os.dup(slave), 'r'), os.fdopen(os.dup(slave), 'w'))
    try: yield rec
    finally:
        for stream in rec.terminal: stream.close()
        os.close(slave); os.close(master)


@contextmanager
def owner_terminal(rec):
    """The real require_owner_terminal sees a pseudo-terminal only during the call (pytest's capture takes the
    standard streams back at each test phase)."""
    saved = sys.stdin, sys.stderr
    sys.stdin, sys.stderr = rec.terminal
    try: yield
    finally: sys.stdin, sys.stderr = saved


def pair(rec):
    with owner_terminal(rec): asyncio.run(whatsapp_pair.pair_config(rec.config))


def kinds(rec):
    return [event[0] for event in rec.events]


def run_cli(rec, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['whatsapp_pair', '--config', str(rec.config)])
    with owner_terminal(rec): whatsapp_pair.main()


# spec: ANYM-87
def test_fake_pair_child_is_reached_only_after_singleton_cleanup_under_the_held_session_lock(pairing, monkeypatch,
                                                                                             capsys):
    rec = pairing
    profile_before, targets_before = tree(rec.profile), tree(rec.targets)
    assert lock_state(rec.session) == 'absent' and present(rec.profile) == sorted(SINGLETONS)
    run_cli(rec, monkeypatch)
    # The command's own answer to a FAKE child that exited 0: never live pairing acceptance.
    assert json.loads(capsys.readouterr().out) == {'status': 'paired', 'collector_started': False}
    unlinks = [event for event in rec.events if event[0] == 'unlink']
    # Each stale link is removed by name while the session flock is held, and is still a link when it goes.
    assert sorted(unlinks) == sorted(('unlink', name, 'held', target) for name, target in rec.links.items())
    assert kinds(rec)[:5] == ['unlink', 'unlink', 'unlink', 'store', 'lease']
    store, = [event for event in rec.events if event[0] == 'store']
    assert store == ('store', 'held', [], 'session_already_owned')
    child, = [event for event in rec.events if event[0] == 'fake_pair_child']
    # Reached last, with the cleaned profile, the lock still held and the locked descriptor inherited.
    assert child == ('fake_pair_child', 'held', [], True)
    assert kinds(rec).index('fake_pair_child') > kinds(rec).index('lease')
    assert rec.command[0] == '/synthetic/never-run/node' and Path(rec.command[1]).name == 'pair.mjs'
    assert rec.command[2] == str(rec.config)
    # Links were unlinked, never followed: every target and every other profile entry is unchanged.
    assert tree(rec.targets) == targets_before
    assert tree(rec.profile) == {k: v for k, v in profile_before.items() if k not in SINGLETONS}
    assert lock_state(rec.session) == 'free' and second_opener(rec.session) == 'acquired'


# spec: ANYM-87
@pytest.mark.parametrize('kind', ['file', 'directory'])
@pytest.mark.parametrize('name', SINGLETONS)
def test_a_singleton_entry_that_is_not_a_symlink_refuses_invalid_browser_lock_before_the_store_or_fake_child(
        pairing, monkeypatch, capsys, name, kind):
    rec = pairing
    (rec.profile / name).unlink()
    if kind == 'file': (rec.profile / name).write_bytes(b'SYNTHETIC_REGULAR_ENTRY')
    else: (rec.profile / name).mkdir(mode=0o700); (rec.profile / name / 'inside').write_bytes(b'SYNTHETIC_DIR_ENTRY')
    profile_before, targets_before = tree(rec.profile), tree(rec.targets)
    with pytest.raises(ValueError, match='^invalid_browser_lock$'): pair(rec)
    # The same refusal through the command line, which reports a fixed code and exits 1.
    with pytest.raises(SystemExit) as stopped: run_cli(rec, monkeypatch)
    assert stopped.value.code == 1
    assert json.loads(capsys.readouterr().out) == {'status': 'not_paired', 'code': 'pairing_failed'}
    assert not {'store', 'lease', 'fake_pair_child'} & set(kinds(rec))
    assert rec.command is None
    # The entry that is not a link is left in place; no target or other profile entry changed.
    assert tree(rec.profile)[name] == profile_before[name]
    assert {k: v for k, v in tree(rec.profile).items() if k not in SINGLETONS} == \
        {k: v for k, v in profile_before.items() if k not in SINGLETONS}
    assert tree(rec.targets) == targets_before
    assert lock_state(rec.session) == 'free' and second_opener(rec.session) == 'acquired'


# spec: ANYM-87
def test_while_another_opener_holds_the_session_lock_the_pairing_command_leaves_every_singleton_link(pairing):
    rec = pairing
    profile_before, targets_before = tree(rec.profile), tree(rec.targets)
    with collector_state.session_lock(rec.session):
        with pytest.raises(RuntimeError, match='^session_already_owned$'): pair(rec)
    assert rec.events == [] and rec.command is None
    assert tree(rec.profile) == profile_before and tree(rec.targets) == targets_before


# spec: ANYM-87
@pytest.mark.parametrize('outcome', ['fake_child_exit_0', 'fake_child_exit_1', 'synthetic_account'])
def test_the_pairing_command_releases_the_session_lock_when_it_exits_or_fails(pairing, outcome):
    rec = pairing
    rec.exit_code = 1 if outcome == 'fake_child_exit_1' else 0
    rec.synthetic_account = outcome == 'synthetic_account'
    if outcome == 'fake_child_exit_0': pair(rec)
    else:
        expected = 'pairing_failed' if outcome == 'fake_child_exit_1' else 'live_account_required'
        with pytest.raises(RuntimeError, match=f'^{expected}$'): pair(rec)
    assert ('fake_pair_child' in kinds(rec)) == (outcome != 'synthetic_account')
    # The store closes inside the lock; afterwards the lock is free and the cleanup it guarded has happened.
    assert rec.events[-1] == ('store_close', 'held')
    assert lock_state(rec.session) == 'free' and second_opener(rec.session) == 'acquired'
    assert present(rec.profile) == []
