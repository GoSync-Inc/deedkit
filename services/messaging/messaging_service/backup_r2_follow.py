"""Root-only receiver update follow (ANYM-597), invoked by its own timer.

It moves the verifier's pinned helper image forward to the `backup` image of
main's head: main's head by `git ls-remote` (no API quota) into a treeless bare
mirror, forward only; the image's attestation through the pinned tokenless
verifier (as scripts/production.py checks plan A images); a pull by digest; the
verifier's own image preflight; a synthetic PG18+age restore self-test of the
new image; then one atomic rewrite of the verifier config that keeps the
previous pair for `revert`. It then publishes the pinned helper's facts for the
receiver's `capabilities` mode. Anything unproven keeps the current pair and is
reported. It holds the verifier lock throughout, so it never runs while a
verification runs, and it never switches while a retry journal waits.

The verifier code itself (the venv) is not followed: it is installed by hand.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys

from . import backup_r2_receiver as r2
from . import backup_r2_verifier as verifier
from . import backup_receiver as local

SOURCE = 'https://github.com/GoSync-Inc/deedkit.git'
IMAGE = 'ghcr.io/gosync-inc/deedkit-backup'
# The tokenless attestation verifier production.py runs (tools/attest-verify), installed
# by hand; a test keeps this hash equal to tools/attest-verify/attest-verify.linux-amd64.sha256.
ATTEST_VERIFIER = Path('/usr/local/libexec/deedkit/attest-verify')
ATTEST_SHA256 = 'af405433c424fe20580cc86e2d96221670b0a75d6af9068ab06d309ec49344db'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')
# The verifier's refusal lines, read as scripts/production.py reads them: only a registry
# 404 proves the image is not published yet; an unread registry is retried.
ATTEST_ABSENT = re.compile(r'attest-verify: refused: manifest \S+ not found(; |$)', re.M)
ATTEST_UNREACHED = re.compile(r'attest-verify: refused: no Sigstore bundle is attached to |registry (request|read) '
                              r'failed|: HTTP \d+')
# Direct children of the verifier's root-private directory.
MIRROR = 'follow-main.git'
CURRENT = 'follow-current.json'
PREVIOUS = 'follow-previous.json'
STATUS = 'follow-status.json'
SELF_TEST = 'selftest-'
TEMPORARY = re.compile(r'\.capabilities\.json-[0-9a-f]{32}')
# Answers that only mean "not now": the service exits cleanly and the next tick retries.
WAITING = {'verifier_busy', 'verification_pending', 'helper_unattested', 'attestation_unreachable', 'main_unreachable'}
SAFE = WAITING | {'follow_config_mismatch', 'follow_not_forward', 'helper_attestation_refused',
                  'attest_verifier_unavailable', 'helper_pull_failed', 'helper_schemas_unavailable',
                  'helper_self_test_mismatch', 'helper_self_test_failed', 'publication_directory_foreign',
                  'publication_foreign', 'publication_mount_point', 'untrusted_operator_ancestor',
                  'no_previous_pair', 'invalid_follow_record'}
SCHEMA_PROBE = ('import json;from messaging_service.backup import SCHEMAS as s;'
                'print(json.dumps([s.start,s.stop-1] if s.step==1 else None))')
# The native drill's synthetic fixture (tests/test_backup_r2_verifier_native.py), run by the image under test.
FIXTURE_SCRIPT = r'''
import hashlib,json,os
from pathlib import Path
import psycopg
from psycopg.conninfo import make_conninfo
from messaging_service.store import Store
from messaging_service.backup import create_snapshot
from messaging_service.backup_crypto import encrypt_snapshot
root=Path('/fixture')
recipient=os.environ['DEEDKIT_SYNTHETIC_RECIPIENT']
password=os.environ['POSTGRES_PASSWORD']
base=dict(host='restore-postgres',user='restore_owner',password=password,port='5432')
with psycopg.connect(make_conninfo(**base,dbname='deedkit_verify_restore_test'),autocommit=True) as c:
    c.execute('CREATE DATABASE deedkit_fixture')
dsn=make_conninfo(**base,dbname='deedkit_fixture')
store=Store(dsn,root/'originals','http://synthetic.invalid')
try:
    store.migrate();store.seed()
    content=(b'SYNTHETIC_FOLLOW_SELF_TEST_ORIGINAL\n'*(1024**2//36+1))[:1024**2]
    digest=hashlib.sha256(content).hexdigest();(store.files/digest).write_bytes(content)
    with store.pool.connection() as c:
        c.execute("INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES('follow-self-test','demo-tg-2',%s,'synthetic.bin','application/octet-stream',%s)",(digest,len(content)))
    os.environ['PGPASSWORD']=password
    public=make_conninfo(host='restore-postgres',user='restore_owner',port='5432',dbname='deedkit_fixture')
    create_snapshot(store,root/'snapshot',['pg_dump','--dbname='+public])
    encrypt_snapshot(root/'snapshot',root/'ciphertext',recipient)
    (root/'manifest-sha256').write_text(hashlib.sha256((root/'snapshot/manifest.json').read_bytes()).hexdigest())
finally:store.close()
'''


def git_environment(home):
    # git sees only its own repository: no inherited GIT_* variable, global or system
    # configuration (URL rewrites, credential helpers), replacement objects or prompts.
    return {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'XDG_CONFIG_HOME': str(home), 'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_TERMINAL_PROMPT': '0'}


class Main:
    """main's head and ancestry from a persistent treeless bare mirror in the verifier directory."""
    def __init__(self, directory, source=SOURCE):
        self.directory = Path(directory); self.mirror = self.directory / MIRROR; self.source = source

    def git(self, *args, check=True):
        try:
            return subprocess.run(['git', *args], cwd=self.directory, env=git_environment(self.directory),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  timeout=300, check=check)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            # The run's deadline or stop (TimeoutError, InterruptedError) is no network blip.
            if r2.CANCELLED:raise
            raise RuntimeError('main_unreachable') from None

    def head(self):
        line = self.git('ls-remote', self.source, 'refs/heads/main').stdout.decode().split()
        if len(line) != 2 or line[1] != 'refs/heads/main' or not re.fullmatch('[0-9a-f]{40}', line[0]):
            raise RuntimeError('main_unreachable')
        return line[0]

    def forward(self, current, head):
        """Whether `head` descends from `current` on main (commits only: no trees, no blobs)."""
        if not self.mirror.is_dir():
            self.git('init', '--quiet', '--bare', '--template=', str(self.mirror))
        self.git('-C', str(self.mirror), 'fetch', '--quiet', '--filter=tree:0', self.source, head)
        # 0: an ancestor; 1: not one; 128: `current` is no commit of main's history.
        return self.git('-C', str(self.mirror), 'merge-base', '--is-ancestor', current, head,
                        check=False).returncode == 0


def attested(sha):
    """The verified `backup` image of `sha`: {reference, digest, config_digest}, tokenless."""
    try:
        info = ATTEST_VERIFIER.lstat()
        content = ATTEST_VERIFIER.read_bytes() if stat.S_ISREG(info.st_mode) else b''
    except OSError:
        info, content = None, b''
    if (info is None or not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.geteuid()) or
            info.st_mode & 0o022 or hashlib.sha256(content).hexdigest() != ATTEST_SHA256):
        raise ValueError('attest_verifier_unavailable')
    try:
        # An empty environment: no GitHub or registry credential can reach the verifier.
        result = subprocess.run([str(ATTEST_VERIFIER), '--image', IMAGE, '--sha', sha], env={'PATH': '/usr/bin:/bin'},
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError('attestation_unreachable') from None
    except OSError:
        if r2.CANCELLED:raise
        raise ValueError('attest_verifier_unavailable') from None
    if result.returncode:
        text = (result.stderr + result.stdout).decode(errors='replace')
        if ATTEST_ABSENT.search(text):raise RuntimeError('helper_unattested')
        if ATTEST_UNREACHED.search(text):raise RuntimeError('attestation_unreachable')
        raise ValueError('helper_attestation_refused')
    try:value = json.loads(result.stdout)
    except ValueError:value = None
    if (not isinstance(value, dict) or value.get('sha') != sha or
            not all(isinstance(value.get(key), str) and DIGEST.fullmatch(value[key]) for key in ('digest', 'config_digest')) or
            value.get('reference') != IMAGE + '@' + value['digest']):
        raise ValueError('helper_attestation_refused')
    return {key: value[key] for key in ('reference', 'digest', 'config_digest')}


class Docker(verifier.Docker):
    def pull(self, reference):
        try:
            subprocess.run(['docker', 'pull', '--quiet', reference], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=min(1800, self.remaining()), check=True)
        except subprocess.CalledProcessError:raise RuntimeError('helper_pull_failed') from None

    def helper_schemas(self, image):
        """The backup schemas the image's own code reads (`backup.SCHEMAS`), from a networkless run."""
        raw = self.command('run', '--rm', '--network', 'none', '--read-only', '--cap-drop=ALL',
                           '--security-opt=no-new-privileges', '--memory=256m', '--pids-limit=64',
                           '--label', verifier.LABEL + '.owned=1',
                           '--tmpfs', verifier.PG_DATA_PATH + ':' + verifier.HELPER_TMPFS[verifier.PG_DATA_PATH],
                           '--entrypoint', 'python', image, '-c', SCHEMA_PROBE)
        try:value = r2.unique_json(raw)
        except ValueError:value = None
        if (not isinstance(value, list) or len(value) != 2 or any(type(item) is not int for item in value) or
                not 2 <= value[0] <= value[1]):
            raise ValueError('helper_schemas_unavailable')
        return value

    def remove_image(self, image):
        # Best effort: an image a container still uses stays; never a prune.
        try:self.command('image', 'rm', image)
        except (RuntimeError, subprocess.TimeoutExpired):pass


def fixture_run(run):
    return hashlib.sha256(run.encode()).hexdigest()[:32]


def clear_self_test(engine, directory, run):
    """Remove one self-test's exact labelled resources and its workspace (also after a crash)."""
    source = fixture_run(run); name = verifier.names(source)['helper'] + '-fixture'
    if engine.inspect_owned('helper', name, source) is not None:engine.remove('helper', name)
    verifier.cleanup_resources(engine, source); verifier.cleanup_resources(engine, run)
    path = Path(directory) / (SELF_TEST + run)
    try:info = path.lstat()
    except FileNotFoundError:return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise ValueError('invalid_restore_workspace')
    shutil.rmtree(path)


def self_test(engine, config):
    """Restore a synthetic PG18 bundle, made and age-encrypted by the image under test, with
    that image as the helper, through the verifier's own isolated restore. Returns the
    proven schema version. A throwaway identity; the receiver's key is never mounted."""
    directory = Path(config['directory'])
    for name in os.listdir(directory):
        if name.startswith(SELF_TEST) and re.fullmatch('[0-9a-f]{32}', name[len(SELF_TEST):]):
            clear_self_test(engine, directory, name[len(SELF_TEST):])
    run = secrets.token_hex(16); source = fixture_run(run); root = directory / (SELF_TEST + run)
    root.mkdir(mode=0o700)
    fixture = root / 'fixture'; fixture.mkdir(mode=0o700)
    tested = {**config, 'identity_file': str(fixture / 'identity')}
    try:
        try:
            # age reads an OpenSSH ed25519 identity; the receiver has no age-keygen.
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', '', '-f', str(fixture / 'identity')],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=60, check=True)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            if r2.CANCELLED:raise
            raise RuntimeError('helper_self_test_failed') from None
        recipient = (fixture / 'identity.pub').read_text().strip()
        source_secrets = root / 'source-secrets'; source_payload = root / 'source-payload'
        target_secrets = root / 'restore-secrets'; target_payload = root / 'restore-payload'
        for path in (source_secrets, source_payload, target_secrets, target_payload):path.mkdir(mode=0o700)
        password = secrets.token_hex(32)
        env = ('POSTGRES_USER=restore_owner\nPOSTGRES_DB=' + verifier.DATABASE + '\nPOSTGRES_PASSWORD=' + password + '\n').encode()
        verifier.write_secret(source_secrets / 'postgres.env', env)
        verifier.write_secret(source_secrets / 'worker.json', b'{}\n')
        engine.setup(tested, source, source_payload, source_secrets)
        name = verifier.names(source)['helper'] + '-fixture'
        engine.command('container', 'create', '--name', name, '--label', verifier.LABEL + '=' + source,
                       '--label', verifier.LABEL + '.owned=1', '--network', verifier.names(source)['network'],
                       '--memory=512m', '--memory-swap=512m', '--cpus=1', '--pids-limit=256', '--log-driver=none',
                       '--tmpfs', verifier.PG_DATA_PATH + ':' + verifier.HELPER_TMPFS[verifier.PG_DATA_PATH],
                       '--env-file', str(source_secrets / 'postgres.env'), '--env', 'DEEDKIT_SYNTHETIC_RECIPIENT=' + recipient,
                       '--mount', 'type=bind,src=' + str(fixture) + ',dst=/fixture',
                       '--entrypoint', 'python', config['helper_image'], '-c', FIXTURE_SCRIPT)
        engine.command('container', 'start', '-a', name)
        state = r2.unique_json(engine.command('container', 'inspect', name))[0]['State']
        if state.get('ExitCode') != 0:raise RuntimeError('helper_self_test_failed')
        engine.remove('helper', name); verifier.cleanup_resources(engine, source)
        cipher = fixture / 'ciphertext'; snapshot = (fixture / 'manifest-sha256').read_text()
        with cipher.open('rb') as data:digest = hashlib.file_digest(data, 'sha256').hexdigest()
        receipt = {'wire': {'id': '20261028T000000Z-0000000000000597', 'bytes': cipher.stat().st_size,
                            'sha256': digest, 'snapshot_sha256': snapshot}, 'object_key': 'synthetic-self-test'}
        class Fixture:
            def get_object(self, **kwargs):return {'ContentLength': cipher.stat().st_size, 'Body': cipher.open('rb')}
        verifier.write_secret(target_secrets / 'postgres.env', env)
        verifier.write_secret(target_secrets / 'worker.json', r2.encoded({
            'v': 1, 'identity_file': '/run/restore/identity', 'work_directory': '/work', 'database_host': 'restore-postgres',
            'database_password': password, 'snapshot_sha256': snapshot, 'max_plaintext_bytes': receipt['wire']['bytes']}))
        facts = engine.restore(tested, {'bucket': 'synthetic'}, Fixture(), receipt, run, target_payload, target_secrets)
        return verifier.make_proof(receipt, facts, run)['verification']['schema_version']
    finally:
        clear_self_test(Docker(120) if isinstance(engine, verifier.Docker) else engine, directory, run)


def prove(engine, config, test):
    """Preflight, schema probe and self-test of one pinned pair: its follow record."""
    engine.preflight(config)
    schemas = engine.helper_schemas(config['helper_image'])
    if test(engine, config) != schemas[1]:raise ValueError('helper_self_test_mismatch')
    return {'v': 1, 'helper_image': config['helper_image'], 'helper_revision': config['helper_revision'],
            'helper_schemas': schemas, 'tested_at': verifier.now()}


def read_record(fd, name):
    _, value = r2.read_root(fd, name, missing=True)
    if value is None:return None
    if (not isinstance(value, dict) or set(value) != {'v', 'helper_image', 'helper_revision', 'helper_schemas', 'tested_at'} or
            value['v'] != 1 or not isinstance(value['helper_image'], str) or not DIGEST.fullmatch(value['helper_image']) or
            not isinstance(value['helper_revision'], str) or not re.fullmatch('[0-9a-f]{40}', value['helper_revision']) or
            not (value['helper_schemas'] is None or (isinstance(value['helper_schemas'], list) and len(value['helper_schemas']) == 2 and
                 all(type(item) is int for item in value['helper_schemas'])))):
        raise ValueError('invalid_follow_record')
    return value


def pinned(record, config):
    return record is not None and (record['helper_image'], record['helper_revision']) == (
        config['helper_image'], config['helper_revision'])


def write_pins(path, image, revision):
    """One atomic replace of the verifier config with only its two pins changed."""
    value = r2.unique_json(verifier.private_file(path, 16384))
    local.write_maintenance_json(path, {**value, 'helper_image': image, 'helper_revision': revision})
    installed = verifier.load_config(path)
    if (installed['helper_image'], installed['helper_revision']) != (image, revision):
        raise ValueError('invalid_verifier_config')
    return installed


def mount_points():
    points = set()
    with open('/proc/self/mountinfo', 'rb') as source:
        for line in source:
            fields = line.split(b' ')
            if len(fields) > 4:
                points.add(re.sub(rb'\\([0-7]{3})', lambda m: bytes([int(m[1], 8)]), fields[4]).decode(errors='replace'))
    return points


def publish(path, record):
    """Write CAPABILITIES_FILE, bound to the verifier config's current identity.

    Only its own files live in that root directory, the file it replaces must be a
    publication, and the directory must not be a mount point: fail closed otherwise.
    """
    value = {'v': 1, 'verifier_config': r2.config_identity(path), 'helper_revision': record['helper_revision'],
             'helper_schemas': record['helper_schemas']}
    target = r2.CAPABILITIES_FILE; directory = target.parent
    with local.trusted_directory(directory.parent) as parent:
        try:os.mkdir(directory.name, 0o755, dir_fd=parent)
        except FileExistsError:pass
        fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            # Created under the service's 0077 umask: the upload account needs to search it.
            if info.st_uid == 0 and info.st_mode & 0o777 == 0o700:os.fchmod(fd, 0o755)
        finally:os.close(fd)
    if str(directory) in mount_points():raise ValueError('publication_mount_point')
    data = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    with local.trusted_directory(directory) as fd:
        names = os.listdir(fd)
        if any(name != target.name and not TEMPORARY.fullmatch(name) for name in names):
            raise ValueError('publication_directory_foreign')
        for name in names:
            if TEMPORARY.fullmatch(name):os.unlink(name, dir_fd=fd)
        if target.name in names:
            existing = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(existing, 'rb') as source:
                info = os.fstat(source.fileno()); raw = source.read(4097)
            try:old = r2.unique_json(raw)
            except ValueError:old = None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or len(raw) > 4096 or
                    not isinstance(old, dict) or set(old) != set(value) or old.get('v') != 1):
                raise ValueError('publication_foreign')
            if raw == data:return value
        name = '.' + target.name + '-' + secrets.token_hex(16)
        out = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o644, dir_fd=fd)
        try:
            with os.fdopen(out, 'wb') as sink:
                os.fchmod(sink.fileno(), 0o644); sink.write(data); sink.flush(); os.fsync(sink.fileno())
            os.replace(name, target.name, src_dir_fd=fd, dst_dir_fd=fd); local.fsync_dir(fd)
        finally:
            try:os.unlink(name, dir_fd=fd)
            except FileNotFoundError:pass
    return value


def load(path):
    path = Path(path); config = verifier.load_config(path)
    # The receiver's `capabilities` accepts only a publication of the installed service's config.
    if r2.load_config(config['receiver_config_file']).get('verifier_config_file') != str(path):
        raise ValueError('follow_config_mismatch')
    return path, config


def no_retry_pending(fd):
    # A retry journal pins its helper pair (backup_r2_verifier.run): never switch under it.
    if r2.read_root(fd, 'active.json', missing=True)[1] is not None:raise RuntimeError('verification_pending')


def follow(path, *, engine=None, main=None, attest=None, test=None):
    path, config = load(path)
    engine = engine if engine is not None else Docker(3600)
    main = main if main is not None else Main(config['directory'])
    attest = attest or attested; test = test or self_test
    with verifier.verifier_lock(config['directory']) as fd, verifier.deadline(3600):
        status = {'v': 1, 'status': 'current', 'revision': config['helper_revision'], 'checked_at': verifier.now()}
        try:
            no_retry_pending(fd)
            current = read_record(fd, CURRENT)
            if not pinned(current, config):current = None
            head = main.head(); refusal = untested = None
            if current is None:
                # The installed pair is published only once proven here. A pair from before
                # `backup.SCHEMAS` cannot be: a proven newer pair may still replace it.
                try:current = prove(engine, config, test)
                except Exception as error:
                    if r2.CANCELLED:raise
                    untested = error
                else:local.write_maintenance_json(CURRENT, current, dir_fd=fd)
            if head != config['helper_revision']:
                status['candidate'] = head
                try:
                    if not main.forward(config['helper_revision'], head):raise ValueError('follow_not_forward')
                    proof = attest(head)
                    engine.pull(proof['reference'])
                    record = prove(engine, {**config, 'helper_image': proof['config_digest'], 'helper_revision': head}, test)
                except Exception as error:
                    if r2.CANCELLED:raise
                    refusal = error
                else:
                    kept = read_record(fd, PREVIOUS); replaced = config['helper_image']
                    # The kept pair is written first: a crash before the switch leaves the old config.
                    local.write_maintenance_json(PREVIOUS, {
                        'v': 1, 'helper_image': config['helper_image'], 'helper_revision': config['helper_revision'],
                        'helper_schemas': current['helper_schemas'] if current else None,
                        'tested_at': current['tested_at'] if current else verifier.now()}, dir_fd=fd)
                    config = write_pins(path, record['helper_image'], record['helper_revision'])
                    local.write_maintenance_json(CURRENT, record, dir_fd=fd); current = record
                    status.update(status='switched', revision=head)
                    # The pair before the kept one is no undo any more: drop its image.
                    if kept and kept['helper_image'] not in (record['helper_image'], replaced):
                        engine.remove_image(kept['helper_image'])
            if current is None:raise refusal or untested or RuntimeError('follow_failed')
            publish(path, current)
            if refusal is not None:raise refusal
            local.write_maintenance_json(STATUS, status, dir_fd=fd)
            return status
        except BaseException as error:
            code = failure_code(error)
            status.update(status='waiting' if code in WAITING else 'refused', error=code,
                          diagnostic=verifier.describe(error))
            try:local.write_maintenance_json(STATUS, status, dir_fd=fd)
            except Exception:pass
            raise


def revert(path, *, engine=None):
    """Switch back to the kept pair; the replaced pair becomes the kept one."""
    path, config = load(path)
    engine = engine if engine is not None else Docker(600)
    with verifier.verifier_lock(config['directory']) as fd:
        no_retry_pending(fd)
        previous = read_record(fd, PREVIOUS)
        if previous is None:raise ValueError('no_previous_pair')
        engine.preflight({**config, 'helper_image': previous['helper_image'], 'helper_revision': previous['helper_revision']})
        current = read_record(fd, CURRENT); proven = current if pinned(current, config) else None
        replaced = {'v': 1, 'helper_image': config['helper_image'], 'helper_revision': config['helper_revision'],
                    'helper_schemas': proven['helper_schemas'] if proven else None,
                    'tested_at': proven['tested_at'] if proven else verifier.now()}
        local.write_maintenance_json(PREVIOUS, replaced, dir_fd=fd)
        write_pins(path, previous['helper_image'], previous['helper_revision'])
        if previous['helper_schemas'] is None:
            # Never self-tested here: no publication until a follow run proves it.
            try:os.unlink(CURRENT, dir_fd=fd)
            except FileNotFoundError:pass
        else:
            local.write_maintenance_json(CURRENT, previous, dir_fd=fd); publish(path, previous)
        status = {'v': 1, 'status': 'reverted', 'revision': previous['helper_revision'], 'checked_at': verifier.now()}
        local.write_maintenance_json(STATUS, status, dir_fd=fd)
        return status


def failure_code(error):
    if str(error) in SAFE:return str(error)
    code = verifier.failure_code(error)
    return 'follow_failed' if code == 'independent_restore_failed' else code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('mode', choices=('follow', 'revert'))
    args = parser.parse_args(argv)
    try:
        result = follow(args.config) if args.mode == 'follow' else revert(args.config)
    except Exception as error:
        code = failure_code(error)
        print(json.dumps({'error': code, 'diagnostic': verifier.describe(error)}, separators=(',', ':')), file=sys.stderr)
        return 0 if code in WAITING else 1
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':raise SystemExit(main())
