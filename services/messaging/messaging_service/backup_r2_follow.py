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

With an instances file (ANYM-609) the pins of every verifier instance move
together: the follow holds every instance's lock, self-tests each profile and
switches all configs or none. `update_pins` is the same step by hand.

The receiver's code follows with the pins once its `venv` is a link to a release
(`releases/<revision>`, a venv): the follow stages the head's release from the
same attested image (its `messaging_service`, on the linked release's
dependencies, which must satisfy the commit's receiver lock), self-tests the
pair with that release's own code, and moves the link right after the pins,
both under one switch journal. A `venv` directory (a hand install) keeps its
code by hand: only the pins follow.
"""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import importlib
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
from .backup_r2_verifier import move_pins, pair, recover_pins

SOURCE = 'https://github.com/GoSync-Inc/deedkit.git'
IMAGE = 'ghcr.io/gosync-inc/deedkit-backup'
# The tokenless attestation verifier production.py runs (tools/attest-verify), installed
# by hand; a test keeps this hash equal to tools/attest-verify/attest-verify.linux-amd64.sha256.
ATTEST_VERIFIER = Path('/usr/local/libexec/deedkit/attest-verify')
ATTEST_SHA256 = 'de0544eda3668946aad1d4237557b3fbd747c702d1e511d0060daa5eaa62978f'
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
# A switch in progress (from, to, the image it drops): recovered before anything else.
SWITCH = 'follow-switch.json'
# An image this follow pulled and has not installed: removed when refused or after a crash.
CANDIDATE = 'follow-candidate.json'
SELF_TEST = 'selftest-'
# Free space a candidate pull and its self-test may take, on top of the verifier's reserve.
PULL_BYTES = 4 * verifier.GIB
TEMPORARY = re.compile(r'\.capabilities\.json-[0-9a-f]{32}')
# The receiver's code (ANYM-597 V1): `venv` is a hand-installed directory or a link to one release.
CODE_ROOT = Path('/opt/deedkit-backup-r2')
RELEASES = 'releases'
RELEASE = re.compile('[0-9a-f]{40}')
STAGE = '.stage-'
# The `backup` image's code and receiver lock (Dockerfile, backup stage).
IMAGE_PACKAGE = '/app/messaging_service'
IMAGE_LOCK = '/app/requirements-backup-r2.lock'
# Free space a staged release needs on the code root: a copy of the venv and the image's package.
CODE_BYTES = verifier.GIB
# What receive and `capabilities` load in a staged release beyond the self-test (the SDK lazily).
RECEIVER_IMPORTS = ('boto3', 'botocore.config', 'messaging_service.backup_r2_receiver')
# Answers that only mean "not now": the service exits cleanly and the next tick retries.
WAITING = {'verifier_busy', 'verifier_host_busy', 'verification_pending', 'helper_unattested', 'attestation_unreachable', 'main_unreachable'}
SAFE = WAITING | {'follow_config_mismatch', 'follow_not_forward', 'helper_attestation_refused',
                  'attest_verifier_unavailable', 'helper_pull_failed', 'helper_schemas_unavailable',
                  'helper_self_test_mismatch', 'helper_self_test_failed', 'publication_directory_foreign',
                  'publication_foreign', 'publication_mount_point', 'untrusted_operator_ancestor',
                  'no_previous_pair', 'invalid_follow_record', 'follow_switch_unresolved', 'follow_disk_capacity',
                  'publication_directory_private', 'invalid_verifier_config', 'follow_candidate_unresolved',
                  'verifier_cleanup_unconfirmed', 'verifier_pins_unfinished', 'instances_invalid', 'pins_switch_unresolved',
                  'immutable_reviewed_images_required', 'root_verifier_required', 'verifier_code_invalid',
                  'verifier_dependencies_changed', 'staged_self_test_failed', 'invalid_self_test_request'}
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
# The calendar profile's synthetic bundle (ANYM-609), as the native drill makes it: fcs tables, one enabled
# connection with its vault token, one disabled one without; made and encrypted by the image under test.
CALENDAR_FIXTURE_SCRIPT = r'''
import hashlib,os
from pathlib import Path
from cryptography.fernet import Fernet
import psycopg
from psycopg.conninfo import make_conninfo
from messaging_service.backup_calendar import create_calendar_snapshot
from messaging_service.backup_crypto import encrypt_snapshot
root=Path('/fixture')
recipient=os.environ['DEEDKIT_SYNTHETIC_RECIPIENT']
password=os.environ['POSTGRES_PASSWORD']
base=dict(host='restore-postgres',user='restore_owner',password=password,port='5432')
with psycopg.connect(make_conninfo(**base,dbname='deedkit_verify_restore_test'),autocommit=True) as c:
    c.execute('CREATE DATABASE deedkit_calendar_fixture')
dsn=make_conninfo(**base,dbname='deedkit_calendar_fixture')
with psycopg.connect(dsn,autocommit=True) as c:
    c.execute("""CREATE SCHEMA fcs;
        CREATE TABLE fcs.migrations(version int PRIMARY KEY, checksum text NOT NULL);
        INSERT INTO fcs.migrations SELECT v,'synthetic-'||v FROM generate_series(1,4) v;
        CREATE TABLE fcs.connections(id text PRIMARY KEY, provider text NOT NULL, enabled boolean NOT NULL DEFAULT true);
        INSERT INTO fcs.connections VALUES ('home','google',true),('old','icloud',false);
        CREATE TABLE fcs.events(id int PRIMARY KEY, title text, at timestamptz);
        INSERT INTO fcs.events SELECT g,'synthetic '||g,'2026-10-28 12:00+00'::timestamptz+g*interval '1 hour' FROM generate_series(1,50) g""")
vault=root/'vault';profile=root/'profile';vault.mkdir(mode=0o700);profile.mkdir(mode=0o700)
key=Fernet.generate_key();(vault/'key').write_bytes(key)
(vault/'home.enc').write_bytes(Fernet(key).encrypt(b'{"refresh_token":"SYNTHETIC_NOT_A_TOKEN"}'))
for name,data in (('postgres.env',b'POSTGRES_PASSWORD=synthetic\n'),('runtime.json',b'{}\n'),('gateway.json',b'{}\n'),
                  ('worker.json',b'{}\n')):
    (profile/name).write_bytes(data)
os.environ['PGPASSWORD']=password
public=make_conninfo(host='restore-postgres',user='restore_owner',port='5432',dbname='deedkit_calendar_fixture')
create_calendar_snapshot(dsn,root/'snapshot',['pg_dump','--dbname='+public],vault_directory=vault,profile_directory=profile)
encrypt_snapshot(root/'snapshot',root/'ciphertext',recipient)
(root/'manifest-sha256').write_text(hashlib.sha256((root/'snapshot/manifest.json').read_bytes()).hexdigest())
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

    def docker_root(self):
        root = self.command('info', '--format', '{{.DockerRootDir}}').decode().strip()
        if not Path(root).is_absolute():raise ValueError('invalid_docker_data_root')
        return root

    def image_id(self, proof):
        """The ID this daemon gives the attested image: the manifest digest with the containerd image store
        (`driver-type io.containerd.snapshotter.v1`), else the config digest, the classic store's image ID."""
        try:status = json.loads(self.command('info', '--format', '{{json .DriverStatus}}'))
        except ValueError:raise RuntimeError('restore_docker_failed') from None
        containerd = isinstance(status, list) and ['driver-type', 'io.containerd.snapshotter.v1'] in status
        return proof['digest'] if containerd else proof['config_digest']

    def has_image(self, image):
        """Whether the daemon holds this exact image ID. A daemon failure raises: never read as absence."""
        return image in self.command('image', 'ls', '--all', '--quiet', '--no-trunc').decode().split()

    def remove_image(self, image):
        """Remove one exact image ID; never a prune. True only once it is confirmed gone."""
        try:self.command('image', 'rm', image)
        except RuntimeError:pass  # in use: still listed below
        return not self.has_image(image)

    def extract(self, image, run, paths):
        """Copy {image path: host path} out of `image` through a created, never started, networkless
        container, named and labelled before it exists and removed by exact name (also after a crash)."""
        name = code_container(run)
        self.command('container', 'create', '--name', name, '--label', verifier.LABEL + '=' + run,
                     '--label', verifier.LABEL + '.owned=1', '--network', 'none', '--entrypoint', 'python', image, '-c', '')
        try:
            for source, target in paths.items():self.command('container', 'cp', name + ':' + source, str(target))
        finally:self.remove('helper', name)


def code_container(run):
    return 'deedkit-follow-code-' + run


def plain_tree(root):
    """The image's package as plain code: directories and regular files only (no link, device or
    set-ID bit), each made root-readable and writable by its owner only."""
    for directory, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(directory) / name; info = path.lstat()
            if stat.S_ISDIR(info.st_mode):path.chmod(0o755)
            elif stat.S_ISREG(info.st_mode) and not info.st_mode & (stat.S_ISUID | stat.S_ISGID):path.chmod(0o644)
            else:raise ValueError('verifier_code_invalid')
    Path(root).chmod(0o755)


def lock_pins(text):
    """{normalized name: version} of a hashed pip lock (`name==version \\`)."""
    pins = {}
    for line in text.splitlines():
        match = re.match(r'([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s;\\]+)', line)
        if match:pins[re.sub(r'[-_.]+', '-', match[1]).lower()] = match[2]
    if not pins:raise ValueError('verifier_dependencies_changed')
    return pins


def installed(packages):
    """{normalized name: version} of the distributions in a site-packages directory."""
    found = {}
    for entry in Path(packages).glob('*.dist-info'):
        try:headers = (entry / 'METADATA').read_text(errors='replace').split('\n\n', 1)[0].splitlines()
        except OSError:continue
        fields = dict(line.split(': ', 1) for line in headers if ': ' in line)
        if 'Name' in fields and 'Version' in fields:found[re.sub(r'[-_.]+', '-', fields['Name']).lower()] = fields['Version']
    return found


class Code:
    """The receiver's code under `root`: `venv` is a hand-installed directory (the code stays by hand)
    or a link to `releases/<revision>`, a venv whose `messaging_service` is that revision's."""
    def __init__(self, root=CODE_ROOT):
        self.root = Path(root); self.link = self.root / 'venv'; self.releases = self.root / RELEASES

    def active(self):
        """The linked release, or None for a hand-installed (or absent) `venv`."""
        try:info = self.link.lstat()
        except FileNotFoundError:return None
        if stat.S_ISDIR(info.st_mode):return None
        if not stat.S_ISLNK(info.st_mode):raise ValueError('verifier_code_invalid')
        target = os.readlink(self.link); name = target[len(RELEASES) + 1:]
        if target != RELEASES + '/' + name:raise ValueError('verifier_code_invalid')
        self.trusted(name)
        return name

    def trusted(self, name):
        """A complete release: a root directory with root-only-writable ancestors and one site-packages."""
        if not isinstance(name, str) or not RELEASE.fullmatch(name):raise ValueError('verifier_code_invalid')
        try:
            with local.trusted_directory(self.releases / name):pass
        except (OSError, ValueError):raise ValueError('verifier_code_invalid') from None
        return self.site_packages(self.releases / name)

    @staticmethod
    def site_packages(release):
        found = list(Path(release).glob('lib/python3.*/site-packages'))
        if len(found) != 1 or not (Path(release) / 'pyvenv.cfg').is_file() or found[0].is_symlink():
            raise ValueError('verifier_code_invalid')
        return found[0]

    def stage(self, engine, image, revision, active, keep=()):
        """`releases/<revision>`: a copy of the active release with the image's `messaging_service`.
        Its dependencies stay the active release's; a receiver lock they do not satisfy is refused.
        A kept release of the same revision is reused (it passed this stage before)."""
        target = self.releases / revision
        if revision in keep and target.exists():return self.trusted(revision)
        if shutil.disk_usage(self.root).free < CODE_BYTES:raise ValueError('follow_disk_capacity')
        run = secrets.token_hex(16); work = self.releases / (STAGE + run)
        work.mkdir(mode=0o700); local.fsync_dir(self.releases)
        release = work / 'release'
        shutil.copytree(self.releases / active, release, symlinks=True)
        packages = self.site_packages(release)
        # The wheel's own packages and record go: the release's code is the image's package alone.
        for name in os.listdir(packages):
            if name in ('messaging_service', 'anymsg') or re.fullmatch(r'anymsg-[^/]+\.dist-info', name):
                shutil.rmtree(packages / name)
        engine.extract(image, run, {IMAGE_PACKAGE: packages / 'messaging_service', IMAGE_LOCK: work / 'lock'})
        plain_tree(packages / 'messaging_service')
        have = installed(packages)
        try:pins = lock_pins((work / 'lock').read_text())
        except (OSError, UnicodeDecodeError):raise ValueError('verifier_dependencies_changed') from None
        if any(have.get(name) != version for name, version in pins.items()):raise ValueError('verifier_dependencies_changed')
        # A leftover release of this revision (refused or interrupted earlier) is never the active or a kept one.
        if target.exists():shutil.rmtree(target)
        os.rename(release, target); shutil.rmtree(work); local.fsync_dir(self.releases)
        return self.trusted(revision)

    def point(self, name):
        """`venv` to `releases/<name>` in one rename; a hand-installed `venv` directory is never replaced."""
        self.trusted(name)
        info = self.link.lstat()
        if not stat.S_ISLNK(info.st_mode):raise ValueError('verifier_code_invalid')
        if os.readlink(self.link) == RELEASES + '/' + name:return
        temporary = self.root / ('.venv-' + secrets.token_hex(16))
        os.symlink(RELEASES + '/' + name, temporary)
        try:os.replace(temporary, self.link)
        except BaseException:
            temporary.unlink(missing_ok=True); raise
        local.fsync_dir(self.root)

    def prune(self, engine, keep):
        """Remove interrupted stages (their exact container first) and every release but the active and kept ones."""
        if not self.releases.is_dir():return
        keep = {name for name in keep if name} | {self.active()}
        for name in os.listdir(self.releases):
            path = self.releases / name
            if name.startswith(STAGE) and re.fullmatch('[0-9a-f]{32}', name[len(STAGE):]):
                run = name[len(STAGE):]
                if engine.inspect_owned('helper', code_container(run), run) is not None:engine.remove('helper', code_container(run))
            elif not RELEASE.fullmatch(name) or name in keep:continue
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):shutil.rmtree(path)
        local.fsync_dir(self.releases)

    def tester(self, name):
        """A self-test run by the release's own code (its `self-test` mode) in a child process."""
        python = self.releases / name / 'bin' / 'python'
        def test(engine, config, run):
            # The child's own deadline ends first, so it removes its resources itself.
            remaining = engine.remaining(); seconds = int(remaining) - 120
            if seconds < 60:raise TimeoutError('restore_deadline')
            request = r2.encoded({'v': 1, 'config': config, 'run': run, 'seconds': seconds})
            try:
                # Isolated (-I: no PYTHON* variable, user site or working directory on its path), no bytecode
                # written into the release; its own failure line goes to the journal (stderr).
                result = subprocess.run([str(python), '-I', '-B', '-m', 'messaging_service.backup_r2_follow', 'self-test'],
                                        input=request, stdout=subprocess.PIPE, cwd='/', timeout=remaining)
            except subprocess.TimeoutExpired:raise TimeoutError('restore_deadline') from None
            except OSError:
                if r2.CANCELLED:raise
                raise ValueError('verifier_code_invalid') from None
            try:value = r2.unique_json(result.stdout) if result.returncode == 0 else None
            except ValueError:value = None
            schemas = value.get('helper_schemas') if isinstance(value, dict) else None
            if (not isinstance(value, dict) or set(value) != {'v', 'helper_schemas', 'schema_version'} or value['v'] != 1 or
                    not isinstance(schemas, list) or len(schemas) != 2 or any(type(item) is not int for item in schemas) or
                    type(value['schema_version']) is not int):
                raise RuntimeError('staged_self_test_failed')
            return schemas, value['schema_version']
        return test


def fixture_run(run):
    return hashlib.sha256(run.encode()).hexdigest()[:32]


def self_test_containers(run, profile='messaging', names=None):
    """The exact names and owner run of a self-test's own containers."""
    source = fixture_run(run); names = names or (lambda owner: verifier.names(owner, profile))
    return [(names(run)['helper'] + '-probe', run), (names(source)['helper'] + '-fixture', source)]


def self_test_resources(run, profile):
    """Every resource one self-test may create, by exact name and owner: its host admission record."""
    return ([('helper', name, owner) for name, owner in self_test_containers(run, profile)] +
            verifier.run_resources(fixture_run(run), profile) + verifier.run_resources(run, profile))


def clear_self_test(engine, directory, run):
    """Remove one self-test's exact labelled resources and its workspace (also after a crash).
    Its workspace does not name its profile: every profile's names, and those from before ANYM-609."""
    containers = [item for profile in r2.PROFILES for item in self_test_containers(run, profile)]
    for name, owner in containers + self_test_containers(run, names=verifier.legacy_names):
        if engine.inspect_owned('helper', name, owner) is not None:engine.remove('helper', name)
    for profile in r2.PROFILES:
        verifier.cleanup_resources(engine, fixture_run(run), profile); verifier.cleanup_resources(engine, run, profile)
    path = Path(directory) / (SELF_TEST + run)
    try:info = path.lstat()
    except FileNotFoundError:return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise ValueError('invalid_restore_workspace')
    shutil.rmtree(path)


def sweep_self_tests(engine, directory):
    """Remove every interrupted self-test's exact containers and workspace (under the verifier lock)."""
    for name in os.listdir(directory):
        if name.startswith(SELF_TEST) and re.fullmatch('[0-9a-f]{32}', name[len(SELF_TEST):]):
            clear_self_test(engine, directory, name[len(SELF_TEST):])


def self_test(engine, config, run=None):
    """Read the image's `backup.SCHEMAS` in a networkless container, then restore a synthetic
    bundle of the config's profile, made and age-encrypted by the image under test, with that image
    as the helper, through the verifier's own isolated restore. Returns (schemas, proven schema version).
    A throwaway identity; the receiver's key is never mounted. Every container is named and
    labelled before it exists, so a crash or a hung one is removed by exact name later."""
    directory = Path(config['directory']); profile = r2.profile(config)
    sweep_self_tests(engine, directory)
    run = run or secrets.token_hex(16); source = fixture_run(run); root = directory / (SELF_TEST + run)
    root.mkdir(mode=0o700); local.fsync_dir(directory)
    fixture = root / 'fixture'; fixture.mkdir(mode=0o700)
    # The synthetic fixture keeps a Docker volume: only a verification run records and creates a data directory (ANYM-634).
    tested = {**{k: v for k, v in config.items() if k != 'postgres_data_directory'}, 'identity_file': str(fixture / 'identity')}
    (probe, _), (name, _) = self_test_containers(run, profile)
    pg_tmpfs = verifier.PG_DATA_PATH + ':' + verifier.HELPER_TMPFS[verifier.PG_DATA_PATH]
    try:
        engine.command('container', 'create', '--name', probe, '--label', verifier.LABEL + '=' + run,
                       '--label', verifier.LABEL + '.owned=1', '--network', 'none', '--read-only', '--cap-drop=ALL',
                       '--security-opt=no-new-privileges', '--memory=256m', '--memory-swap=256m', '--pids-limit=64',
                       '--tmpfs', pg_tmpfs, '--entrypoint', 'python', config['helper_image'], '-c', SCHEMA_PROBE)
        raw = engine.command('container', 'start', '-a', probe); engine.remove('helper', probe)
        try:schemas = r2.unique_json(raw)
        except ValueError:schemas = None
        if (not isinstance(schemas, list) or len(schemas) != 2 or any(type(item) is not int for item in schemas) or
                not 2 <= schemas[0] <= schemas[1]):
            raise ValueError('helper_schemas_unavailable')
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
        engine.command('container', 'create', '--name', name, '--label', verifier.LABEL + '=' + source,
                       '--label', verifier.LABEL + '.owned=1', '--network', verifier.names(source, profile)['network'],
                       '--memory=512m', '--memory-swap=512m', '--cpus=1', '--pids-limit=256', '--log-driver=none',
                       '--tmpfs', pg_tmpfs,
                       '--env-file', str(source_secrets / 'postgres.env'), '--env', 'DEEDKIT_SYNTHETIC_RECIPIENT=' + recipient,
                       '--mount', 'type=bind,src=' + str(fixture) + ',dst=/fixture',
                       '--entrypoint', 'python', config['helper_image'], '-c',
                       CALENDAR_FIXTURE_SCRIPT if profile == 'calendar' else FIXTURE_SCRIPT)
        engine.command('container', 'start', '-a', name)
        state = r2.unique_json(engine.command('container', 'inspect', name))[0]['State']
        if state.get('ExitCode') != 0:raise RuntimeError('helper_self_test_failed')
        engine.remove('helper', name); verifier.cleanup_resources(engine, source, profile)
        cipher = fixture / 'ciphertext'; snapshot = (fixture / 'manifest-sha256').read_text()
        with cipher.open('rb') as data:digest = hashlib.file_digest(data, 'sha256').hexdigest()
        receipt = {'wire': {'id': '20261028T000000Z-0000000000000597', 'bytes': cipher.stat().st_size,
                            'sha256': digest, 'snapshot_sha256': snapshot}, 'object_key': 'synthetic-self-test'}
        class Fixture:
            def get_object(self, **_):return {'ContentLength': cipher.stat().st_size, 'Body': cipher.open('rb')}
        verifier.write_secret(target_secrets / 'postgres.env', env)
        worker = {'v': 1, 'identity_file': '/run/restore/identity', 'work_directory': '/work', 'database_host': 'restore-postgres',
                  'database_password': password, 'snapshot_sha256': snapshot, 'max_plaintext_bytes': receipt['wire']['bytes']}
        if profile == 'calendar':worker['profile'] = profile
        verifier.write_secret(target_secrets / 'worker.json', r2.encoded(worker))
        facts = engine.restore(tested, {'bucket': 'synthetic'}, Fixture(), receipt, run, target_payload, target_secrets)
        return schemas, verifier.make_proof(receipt, facts, run, profile)['verification']['schema_version']
    finally:
        clear_self_test(Docker(120) if isinstance(engine, verifier.Docker) else engine, directory, run)


def admitted_test(engine, config, test):
    """One self-test as the host's one heavy restore (ANYM-609): its exact resources are recorded
    before any exists. Its caller holds every instance's lock, so it never waits for a verification;
    a holder it finds is one outside the instances file and is a wait, not a refusal."""
    profile = r2.profile(config); run = secrets.token_hex(16)
    with verifier.host_lock(config, wait=0) as fd:
        verifier.admit(engine, fd, profile, run, self_test_resources(run, profile))
        result = test(engine, config, run)
        # Kept after a failure: the next holder confirms its resources gone first.
        verifier.release_admission(fd)
    return result


def prove(engine, config, test):
    """Preflight, schema probe and self-test of one pinned pair: its follow record."""
    engine.preflight(config)
    schemas, version = admitted_test(engine, config, test)
    # The calendar's schema is fcs.migrations', not the image's backup.SCHEMAS.
    if r2.profile(config) == 'messaging' and version != schemas[1]:raise ValueError('helper_self_test_mismatch')
    return {'v': 1, 'helper_image': config['helper_image'], 'helper_revision': config['helper_revision'],
            'helper_schemas': schemas, 'tested_at': verifier.now()}


def valid_record(value):
    # `code`: the release the pair was proven with (a linked `venv`); absent for a hand-installed one.
    keys = {'v', 'helper_image', 'helper_revision', 'helper_schemas', 'tested_at'}
    if (not isinstance(value, dict) or set(value) - {'code'} != keys or
            ('code' in value and not (isinstance(value['code'], str) and RELEASE.fullmatch(value['code']))) or
            value['v'] != 1 or not isinstance(value['helper_image'], str) or not DIGEST.fullmatch(value['helper_image']) or
            not isinstance(value['helper_revision'], str) or not re.fullmatch('[0-9a-f]{40}', value['helper_revision']) or
            not (value['helper_schemas'] is None or (isinstance(value['helper_schemas'], list) and len(value['helper_schemas']) == 2 and
                 all(type(item) is int for item in value['helper_schemas'])))):
        raise ValueError('invalid_follow_record')
    return value


def read_record(fd, name):
    _, value = r2.read_root(fd, name, missing=True)
    return None if value is None else valid_record(value)


def unproven(config, code=None):
    return with_code({'v': 1, 'helper_image': config['helper_image'], 'helper_revision': config['helper_revision'],
                      'helper_schemas': None, 'tested_at': verifier.now()}, code)


def with_code(record, code):
    return {**record, 'code': code} if code is not None else record


def pinned(record, config):
    return record is not None and (record['helper_image'], record['helper_revision']) == (
        config['helper_image'], config['helper_revision'])


def unlink(fd, name):
    try:os.unlink(name, dir_fd=fd)
    except FileNotFoundError:return
    local.fsync_dir(fd)


def point_code(code, record):
    """The `venv` link to the record's release; a record without one leaves the code as it is."""
    if 'code' in record:code.point(record['code'])


def finish_switch(fd, engine, journal, code):
    """After the config holds `to`: its code, the kept pair, the installed record, the dropped image and release."""
    point_code(code, journal['to'])
    local.write_maintenance_json(PREVIOUS, journal['from'], dir_fd=fd)
    if journal['to']['helper_schemas'] is None:unlink(fd, CURRENT)
    else:local.write_maintenance_json(CURRENT, journal['to'], dir_fd=fd)
    # The pair before the kept one is no undo any more.
    if journal['drop'] is not None:engine.remove_image(journal['drop'])
    unlink(fd, SWITCH)
    if 'code' in journal['to']:code.prune(engine, {journal['to']['code'], journal['from'].get('code')})


def switch(path, fd, engine, journal, paths, code):
    """Journal, then every instance's config to `to` (move_pins), then its code and the records:
    `from` survives every crash, and the code follows the pins on recovery."""
    local.write_maintenance_json(SWITCH, journal, dir_fd=fd)
    with verifier.host_directory(verifier.load_config(path)) as host:
        move_pins(host, paths, journal['to']['helper_image'], journal['to']['helper_revision'])
    finish_switch(fd, engine, journal, code)
    return verifier.load_config(path)


def read_instances(file, default=None):
    """The instances file's verifier configs: {"v": 1, "verifier_configs": [absolute paths]}.
    Without the file, `default` (the follow's own config alone), else `instances_invalid`."""
    try:value = r2.unique_json(verifier.private_file(file, 16384))
    except FileNotFoundError:
        if default is None:raise ValueError('instances_invalid') from None
        return default
    configs = value.get('verifier_configs') if isinstance(value, dict) else None
    if (not isinstance(value, dict) or set(value) != {'v', 'verifier_configs'} or type(value['v']) is not int or
            value['v'] != 1 or not isinstance(configs, list) or not 1 <= len(configs) <= 8 or
            any(not isinstance(p, str) or not Path(p).is_absolute() or '..' in Path(p).parts for p in configs) or
            len(set(configs)) != len(configs)):
        raise ValueError('instances_invalid')
    return [Path(p) for p in configs]


def load_instances(paths):
    """Every instance's config: separate directories, one shared host directory."""
    configs = [verifier.load_config(p) for p in paths]
    if (len({c['directory'] for c in configs}) != len(configs) or
            len({verifier.host_settings(c)[0] for c in configs}) != 1):
        raise ValueError('instances_invalid')
    return configs


@contextmanager
def locked(configs):
    """Every instance's verifier lock, in the instances file's order; none waits (`verifier_busy`)."""
    with ExitStack() as stack:
        yield [stack.enter_context(verifier.verifier_lock(c['directory'])) for c in configs]


def instances(path, config):
    """The follow's instances: its own config first, then the others the instances file lists."""
    paths = read_instances(verifier.INSTANCES_FILE, default=[path])
    if path not in paths:raise ValueError('follow_config_mismatch')
    paths = [path, *[p for p in paths if p != path]]
    return paths, [config, *load_instances(paths)[1:]]


def reload_instances(paths, configs):
    """The configs as they are under every lock, in the same directories."""
    fresh = load_instances(paths)
    if [c['directory'] for c in fresh] != [c['directory'] for c in configs]:raise ValueError('instances_invalid')
    return fresh


def recover(fd, engine, config, code):
    """Finish or drop an interrupted switch (its code with its pins), then remove an interrupted candidate's image."""
    _, journal = r2.read_root(fd, SWITCH, missing=True)
    if journal is not None:
        if (not isinstance(journal, dict) or set(journal) != {'v', 'from', 'to', 'drop'} or journal['v'] != 1 or
                not (journal['drop'] is None or (isinstance(journal['drop'], str) and DIGEST.fullmatch(journal['drop'])))):
            raise ValueError('invalid_follow_record')
        valid_record(journal['from']); valid_record(journal['to'])
        if pinned(journal['to'], config):finish_switch(fd, engine, journal, code)
        elif pinned(journal['from'], config):point_code(code, journal['from']); unlink(fd, SWITCH)
        else:raise ValueError('follow_switch_unresolved')
    _, candidate = r2.read_root(fd, CANDIDATE, missing=True)
    if candidate is not None:
        if not isinstance(candidate, dict) or set(candidate) != {'v', 'helper_image'} or not DIGEST.fullmatch(str(candidate['helper_image'])):
            raise ValueError('invalid_follow_record')
        drop_candidate(fd, engine, config, candidate['helper_image'])


def drop_candidate(fd, engine, config, image):
    """Remove a pulled, not installed image unless it is the installed or the kept pair's."""
    previous = read_record(fd, PREVIOUS)
    if image in (config['helper_image'], previous and previous['helper_image']) or engine.remove_image(image):
        unlink(fd, CANDIDATE)


def mount_points():
    points = set()
    with open('/proc/self/mountinfo', 'rb') as source:
        for line in source:
            fields = line.split(b' ')
            if len(fields) > 4:
                points.add(re.sub(rb'\\([0-7]{3})', lambda m: bytes([int(m[1], 8)]), fields[4]).decode(errors='replace'))
    return points


def publication(value):
    """Whether `value` has exactly the shape the receiver's reader accepts."""
    bound = value.get('verifier_config') if isinstance(value, dict) else None
    schemas = value.get('helper_schemas') if isinstance(value, dict) else None
    return (isinstance(value, dict) and set(value) == {'v', 'verifier_config', 'helper_revision', 'helper_schemas'} and
            type(value['v']) is int and value['v'] == 1 and isinstance(bound, dict) and
            set(bound) == {'path', 'dev', 'ino', 'size', 'mtime_ns', 'ctime_ns'} and isinstance(bound['path'], str) and
            all(type(bound[key]) is int for key in bound if key != 'path') and
            isinstance(value['helper_revision'], str) and re.fullmatch('[0-9a-f]{40}', value['helper_revision']) is not None and
            isinstance(schemas, list) and len(schemas) == 2 and all(type(item) is int for item in schemas) and
            2 <= schemas[0] <= schemas[1])


def publish(path, record):
    """Write CAPABILITIES_FILE, bound to the verifier config's current identity.

    Every check runs before any change: the directory is no mount point, holds only
    this file (and its own temporaries), and the file it replaces is a publication.
    Only a directory this call created gets its mode set; fail closed otherwise.
    """
    value = {'v': 1, 'verifier_config': r2.config_identity(path), 'helper_revision': record['helper_revision'],
             'helper_schemas': record['helper_schemas']}
    target = r2.CAPABILITIES_FILE; directory = target.parent
    with local.trusted_directory(directory.parent) as parent:
        try:
            os.mkdir(directory.name, 0o700, dir_fd=parent); created = True
        except FileExistsError:created = False
        fd = os.open(directory.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if str(directory) in mount_points() or info.st_dev != os.fstat(parent).st_dev:
                raise ValueError('publication_mount_point')
            # The upload account reads it: searchable, never writable by others.
            if created:os.fchmod(fd, 0o755); local.fsync_dir(parent)
            elif info.st_mode & 0o005 != 0o005:raise ValueError('publication_directory_private')
        finally:os.close(fd)
    data = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    with local.trusted_directory(directory) as fd:
        names = os.listdir(fd)
        if any(name != target.name and not TEMPORARY.fullmatch(name) for name in names):
            raise ValueError('publication_directory_foreign')
        if target.name in names:
            existing = os.open(target.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(existing, 'rb') as source:
                info = os.fstat(source.fileno()); raw = source.read(4097)
            try:old = r2.unique_json(raw)
            except ValueError:old = None
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or len(raw) > 4096 or
                    not publication(old)):
                raise ValueError('publication_foreign')
            if raw == data:return value
        for name in names:
            if TEMPORARY.fullmatch(name):os.unlink(name, dir_fd=fd)
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


def reload(path, config):
    """The config as it is under the verifier lock: a run that loaded it earlier never acts on old pins."""
    _, fresh = load(path)
    if fresh['directory'] != config['directory']:raise ValueError('invalid_verifier_config')
    return fresh


def no_retry_pending(fd):
    # A retry journal pins its helper pair (backup_r2_verifier.run): never switch under it.
    if r2.read_root(fd, 'active.json', missing=True)[1] is not None:raise RuntimeError('verification_pending')


def candidate(fd, engine, config, head, attest, test, profiles=(), stage=None):
    """Pull, check and self-test main's head image: its record. A refused pull is removed.
    With `stage`, the head's code is staged from the preflighted image and runs every self-test."""
    # An unresolved earlier candidate keeps its record: never overwritten by a new one.
    if r2.read_root(fd, CANDIDATE, missing=True)[1] is not None:raise ValueError('follow_candidate_unresolved')
    # The pin is the daemon's own image ID (the verifier's preflight compares `Id`): the record names it before the pull.
    proof = attest(head); image = engine.image_id(proof)
    owned = not engine.has_image(image)
    if owned:
        if shutil.disk_usage(engine.docker_root()).free < config['reserve_bytes'] + PULL_BYTES:
            raise ValueError('follow_disk_capacity')
        # Recorded before the pull: a crash leaves an image the next run removes.
        local.write_maintenance_json(CANDIDATE, {'v': 1, 'helper_image': image}, dir_fd=fd)
    try:
        engine.pull(proof['reference'])
        if not engine.has_image(image):raise RuntimeError('helper_pull_failed')
        tested = {**config, 'helper_image': image, 'helper_revision': head}
        if stage is not None:engine.preflight(tested); test = stage(image)
        record = prove(engine, tested, test)
        # Every other instance's profile on its own synthetic bundle: the pins move only together.
        for profile in profiles:admitted_test(engine, {**tested, 'profile': profile}, test)
        return record
    except Exception:
        if owned and not r2.CANCELLED:drop_candidate(fd, engine, config, image)
        raise


def follow(path, *, engine=None, main=None, attest=None, test=None, code=None):
    path, config = load(path); paths, configs = instances(path, config)
    engine = engine if engine is not None else Docker(3600); code = code if code is not None else Code()
    main = main if main is not None else Main(config['directory'])
    attest = attest or attested; test = test or self_test
    with locked(configs) as fds, verifier.deadline(3600):
        fd = fds[0]
        status = {'v': 1, 'status': 'current', 'revision': config['helper_revision'], 'checked_at': verifier.now()}
        try:
            config = reload(path, config); configs = reload_instances(paths, configs)
            status['revision'] = config['helper_revision']
            # Leftover self-test containers go first, also while a verification retry waits for its resources.
            sweep_self_tests(engine, config['directory'])
            for held in fds:no_retry_pending(held)
            # A stopped pin step first, then the follow's own switch against the configs as they are now.
            with verifier.host_directory(config) as host:recover_pins(host, paths)
            config = reload(path, config); recover(fd, engine, config, code)
            config = reload(path, config); configs = reload_instances(paths, configs)
            profiles = list(dict.fromkeys(r2.profile(c) for c in configs if r2.profile(c) != r2.profile(config)))
            current = read_record(fd, CURRENT); kept = read_record(fd, PREVIOUS)
            # The release the running code came from; None while the code is installed by hand.
            active = code.active()
            if active is not None:
                status['code'] = active
                code.prune(engine, {current and current.get('code'), kept and kept.get('code')})
            if not pinned(current, config) or current.get('code') != active:current = None
            head = main.head(); refusal = untested = unaligned = None
            if any(pair(c) != pair(config) for c in configs):
                # A newly listed instance (or one pinned by hand) joins this pair once the pair passes
                # that profile's self-test; a candidate switch below moves every instance anyway.
                try:
                    for profile in profiles:admitted_test(engine, {**config, 'profile': profile}, test)
                    with verifier.host_directory(config) as host:move_pins(host, paths, *pair(config))
                except Exception as error:
                    if r2.CANCELLED:raise
                    unaligned = error
                else:status['aligned'] = True
            if current is None:
                # The installed pair is published only once proven here. A pair from before
                # `backup.SCHEMAS` cannot be: a proven newer pair may still replace it.
                try:current = with_code(prove(engine, config, test), active)
                except Exception as error:
                    if r2.CANCELLED:raise
                    untested = error
                else:local.write_maintenance_json(CURRENT, current, dir_fd=fd)
            if head != config['helper_revision']:
                status['candidate'] = head; stage = None
                keep = {active, kept and kept.get('code')}
                try:
                    if not main.forward(config['helper_revision'], head):raise ValueError('follow_not_forward')
                    if active not in (None, head):
                        # The code moves forward too: never to a commit older than the linked release's.
                        if not main.forward(active, head):raise ValueError('follow_not_forward')
                        def stage(image):
                            code.stage(engine, image, head, active, keep); return code.tester(head)
                    record = with_code(candidate(fd, engine, config, head, attest, test, profiles, stage),
                                       None if active is None else head)
                except Exception as error:
                    if r2.CANCELLED:raise
                    refusal = error
                    if stage is not None:
                        # A refused release goes now; one that cannot be removed goes at the next run's start.
                        try:code.prune(engine, keep)
                        except Exception:pass
                else:
                    drop = kept['helper_image'] if kept and kept['helper_image'] not in (
                        config['helper_image'], record['helper_image']) else None
                    config = switch(path, fd, engine, {'v': 1, 'from': current or unproven(config, active), 'to': record,
                                                       'drop': drop}, paths, code)
                    unlink(fd, CANDIDATE); current = record
                    status.update(status='switched', revision=head)
                    if 'code' in record:status['code'] = record['code']
            if current is None:raise refusal or untested or unaligned or RuntimeError('follow_failed')
            publish(path, current)
            if refusal is not None:raise refusal
            if unaligned is not None and status['status'] != 'switched':raise unaligned
            local.write_maintenance_json(STATUS, status, dir_fd=fd)
            return status
        except BaseException as error:
            failure = failure_code(error)
            status.update(status='waiting' if failure in WAITING else 'refused', error=failure,
                          diagnostic=verifier.describe(error))
            try:local.write_maintenance_json(STATUS, status, dir_fd=fd)
            except Exception:pass
            raise


def revert(path, *, engine=None, test=None, code=None):
    """Switch back to the kept pair and its release; the replaced pair becomes the kept one. Every listed
    instance moves with it, once the kept pair passes each other profile's self-test (by the kept code)."""
    path, config = load(path); paths, configs = instances(path, config)
    engine = engine if engine is not None else Docker(3600); test = test or self_test
    code = code if code is not None else Code()
    with locked(configs) as fds, verifier.deadline(3600):
        fd = fds[0]
        config = reload(path, config); configs = reload_instances(paths, configs)
        sweep_self_tests(engine, config['directory'])
        for held in fds:no_retry_pending(held)
        with verifier.host_directory(config) as host:recover_pins(host, paths)
        config = reload(path, config); recover(fd, engine, config, code); config = reload(path, config)
        previous = read_record(fd, PREVIOUS)
        if previous is None:raise ValueError('no_previous_pair')
        kept = {**config, 'helper_image': previous['helper_image'], 'helper_revision': previous['helper_revision']}
        engine.preflight(kept); active = code.active()
        if 'code' in previous:code.trusted(previous['code']); test = code.tester(previous['code'])
        # A kept helper from before a profile existed (calendar: 115a55d3) refuses its bundle: no change then.
        for profile in dict.fromkeys(r2.profile(c) for c in configs if r2.profile(c) != r2.profile(config)):
            admitted_test(engine, {**kept, 'profile': profile}, test)
        current = read_record(fd, CURRENT)
        # Every instance moves back with it: the pins move only together.
        switch(path, fd, engine, {'v': 1, 'from': current if pinned(current, config) and current.get('code') == active
                                  else unproven(config, active), 'to': previous, 'drop': None}, paths, code)
        # A pair never proven here is not published until a follow run proves it.
        if previous['helper_schemas'] is not None:publish(path, previous)
        status = {'v': 1, 'status': 'reverted', 'revision': previous['helper_revision'], 'checked_at': verifier.now()}
        local.write_maintenance_json(STATUS, status, dir_fd=fd)
        return status


def update_pins(instances_file, image, revision, *, engine=None, test=None):
    """The pin step (ANYM-609, C2): every config the instances file lists to one helper pair in one
    run, or none. It holds every instance's lock, refuses while any has a retry journal, preflights
    the pair for each, self-tests it on each profile's synthetic bundle, then moves all pins."""
    if os.geteuid() != 0:raise ValueError('root_verifier_required')
    if (not isinstance(image, str) or not DIGEST.fullmatch(image) or
            not isinstance(revision, str) or not re.fullmatch('[0-9a-f]{40}', revision)):
        raise ValueError('immutable_reviewed_images_required')
    paths = read_instances(Path(instances_file)); configs = load_instances(paths)
    engine = engine if engine is not None else Docker(3600); test = test or self_test
    with locked(configs) as fds, verifier.deadline(3600):
        configs = reload_instances(paths, configs); base = configs[0]
        sweep_self_tests(engine, base['directory'])
        with verifier.host_directory(base) as host:recover_pins(host, paths)
        for held in fds:no_retry_pending(held)
        for config in configs:engine.preflight({**config, 'helper_image': image, 'helper_revision': revision})
        tested = {**base, 'helper_image': image, 'helper_revision': revision}
        for profile in dict.fromkeys(r2.profile(c) for c in configs):prove(engine, {**tested, 'profile': profile}, test)
        with verifier.host_directory(base) as host:move_pins(host, paths, image, revision)
    return {'v': 1, 'status': 'pinned', 'helper_image': image, 'helper_revision': revision,
            'verifier_configs': [str(p) for p in paths]}


def staged_self_test(source):
    """The `self-test` mode: one self-test by this code, for a follow (root) that staged it and holds
    its locks and host admission. One JSON request on stdin: {"v": 1, "config", "run", "seconds"}."""
    if os.geteuid() != 0:raise ValueError('root_verifier_required')
    try:request = r2.unique_json(source.read(65537))
    except ValueError:request = None
    if (not isinstance(request, dict) or set(request) != {'v', 'config', 'run', 'seconds'} or request['v'] != 1 or
            not isinstance(request['config'], dict) or not isinstance(request['run'], str) or
            not re.fullmatch('[0-9a-f]{32}', request['run']) or type(request['seconds']) is not int or
            not 60 <= request['seconds'] <= 3600):
        raise ValueError('invalid_self_test_request')
    try:
        for name in RECEIVER_IMPORTS:importlib.import_module(name)
    except ImportError:raise ValueError('verifier_dependencies_changed') from None
    with verifier.deadline(request['seconds']):
        schemas, version = self_test(Docker(request['seconds']), request['config'], request['run'])
    return {'v': 1, 'helper_schemas': schemas, 'schema_version': version}


def failure_code(error):
    if str(error) in SAFE:return str(error)
    code = verifier.failure_code(error)
    return 'follow_failed' if code == 'independent_restore_failed' else code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path)
    # `self-test` is the staged code's side of a follow (Code.tester), never a unit's command.
    parser.add_argument('mode', choices=('follow', 'revert', 'self-test'))
    args = parser.parse_args(argv)
    if (args.config is None) != (args.mode == 'self-test'):parser.error('--config is required for follow and revert only')
    try:
        if args.mode == 'self-test':result = staged_self_test(sys.stdin.buffer)
        else:result = follow(args.config) if args.mode == 'follow' else revert(args.config)
    except Exception as error:
        code = failure_code(error)
        print(json.dumps({'error': code, 'diagnostic': verifier.describe(error)}, separators=(',', ':')), file=sys.stderr)
        return 0 if code in WAITING else 1
    print(json.dumps(result, separators=(',', ':')))
    return 0


if __name__ == '__main__':raise SystemExit(main())
