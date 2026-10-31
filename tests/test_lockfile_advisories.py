"""Every committed Python lockfile stays above the fixed version of known advisories.

The lockfiles are what the images install with `--require-hashes`, so a vulnerable
pin here ships to production even when the pyproject range would allow the fix.
"""
from pathlib import Path, PurePosixPath
import json
import os
import re
import shutil
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
UV_LOCKS = ('uv.lock', 'services/messaging/uv.lock', 'services/gateway/uv.lock',
    'services/calls/uv.lock', 'services/calendar/uv.lock', 'tools/dev/uv.lock')
RECORDINGS_LOCK = 'services/recordings/uv.lock'
REQUIREMENTS_LOCKS = ('services/messaging/requirements.lock', 'services/messaging/requirements-test.lock',
    'services/messaging/requirements-enrichment.lock', 'services/messaging/requirements-telemetry.lock',
    'services/messaging/requirements-backup-r2.lock')
# package -> (first fixed version, advisory)
FIXED = {'certifi': ('2026.10.19', 'PYSEC-2026-4320'), 'filelock': ('4.2.1', 'GHSA-v4x8-3wr5-3wq8'),
    'pyasn1': ('0.6.5', 'GHSA-2qm8-x737-gmw5 / GHSA-9m75-vc3g-4x39 / GHSA-fm9c-5rcf-2q5p / GHSA-mm57-4v3p-rchx')}
COMMENT = re.compile(r'(^|\s+)#.*$')  # pip's req_file.COMMENT_RE
PIN = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s=<>!~,;]+)')


def _version(text):
    return tuple(int(part) for part in text.split('.'))


def _name(text):
    return re.sub(r'[-_.]+', '-', text).lower()


def _logical_lines(text):
    """pip's join_lines and ignore_comments: a full comment line never joins a continuation."""
    pending = []
    for line in text.splitlines():
        if line.endswith('\\') and not COMMENT.match(line):
            pending.append(line.rstrip('\\'))
            continue
        yield COMMENT.sub('', ''.join(pending) + (' ' + line if COMMENT.match(line) else line))
        pending = []
    if pending:
        yield COMMENT.sub('', ''.join(pending))


def _pins(text, uv):
    """Every (package, version) occurrence; any requirements line that is not an exact pin fails."""
    if uv:
        return [(_name(item['name']), item['version']) for item in tomllib.loads(text).get('package', ())
            if 'version' in item]
    pins = []
    for line in _logical_lines(text):
        line = re.sub(r'\s--hash=\S+', '', line).split(';', 1)[0].strip()
        if not line or line.startswith('--'):
            continue
        match = PIN.fullmatch(line)
        assert match, f'unsupported requirements line: {line!r}'
        pins.append((_name(match[1]), match[2]))
    return pins


def _below_fix(pins):
    return [f'{name} {version} < {FIXED[name][0]} ({FIXED[name][1]})' for name, version in pins
        if name in FIXED and _version(version) < _version(FIXED[name][0])]


# spec: ANYM-137
@pytest.mark.parametrize('path', UV_LOCKS + REQUIREMENTS_LOCKS)
def test_lockfile_pins_no_version_below_a_known_advisory_fix(path):
    pins = _pins((ROOT / path).read_text(encoding='utf-8'), path.endswith('uv.lock'))
    assert pins, path
    assert not _below_fix(pins), path


# spec: ANYM-137
def test_lockfile_advisory_parser_sees_every_pin_form():
    requirements = ('Certifi == 2026.7.22 \\\n    --hash=sha256:aa\n'
        '  certifi==2026.7.22 ; python_version < "3.14" \\\n    --hash=sha256:bb\n'
        'certifi[extra]==2026.10.23 ; python_version >= "3.14"\n    # via httpx\n--index-url https://pypi.org/simple\n')
    assert _pins(requirements, False) == [('certifi', '2026.7.22'), ('certifi', '2026.7.22'), ('certifi', '2026.10.23')]
    assert len(_below_fix(_pins(requirements, False))) == 2
    assert _pins('# via dependency \\\ncertifi==2026.7.22\nidna==3.20\n', False) == [('certifi', '2026.7.22'), ('idna', '3.20')]
    uv = ('[[package]]\nname = "certifi"\nversion = "2026.7.22"\n\n'
        '[[package]]\nname = "certifi"\nversion = "2026.10.23"\n')
    assert _below_fix(_pins(uv, True)) == ['certifi 2026.7.22 < 2026.10.19 (PYSEC-2026-4320)']
    for line in ('certifi>=2026.1.1\n', '-e ./certifi\n', 'certifi\n'):
        with pytest.raises(AssertionError, match='unsupported'):
            _pins(line, False)


# spec: ANYM-137
def test_lockfile_advisory_check_lists_every_committed_lockfile():
    # Tracked files only: CI's second checkout (ci-tools/) and local venvs are not this repository's lockfiles.
    tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], check=True, capture_output=True, text=True).stdout
    found = {path for path in tracked.split('\0')
        if PurePosixPath(path).name == 'uv.lock' or re.fullmatch(r'requirements[^/]*\.lock', PurePosixPath(path).name)}
    assert found == set(UV_LOCKS + REQUIREMENTS_LOCKS) | {RECORDINGS_LOCK}


# spec: ANYM-648
def test_recordings_lockfile_pins_no_version_below_a_known_advisory_fix():
    pins = _pins((ROOT / RECORDINGS_LOCK).read_text(encoding='utf-8'), True)
    assert pins
    assert not _below_fix(pins)


# spec: ANYM-137
def test_whatsapp_archive_dependencies_are_patched_and_keep_their_callers_working(tmp_path):
    source = ROOT / 'whatsapp'
    packages = json.loads((source / 'package-lock.json').read_text())['packages']
    fixed = {'glob': '14.0.2', 'minimatch': '11.0.1'}
    for name, version in fixed.items():
        resolved = [item['version'] for path, item in packages.items() if path.endswith('/' + name)]
        assert resolved and all(_version(item) >= _version(version) for item in resolved), (name, resolved)
    # Install only these reviewed locks, with lifecycle scripts and browser downloads disabled.
    # Exercise the APIs actually used by archiver-utils and readdir-glob, then an archive round trip.
    for name in ('package.json', 'package-lock.json'):
        shutil.copyfile(source / name, tmp_path / name)
    env = {**os.environ, 'PUPPETEER_SKIP_DOWNLOAD': 'true'}
    subprocess.run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], cwd=tmp_path,
        env=env, capture_output=True, text=True, check=True, timeout=180)
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    (fixture / 'keep.txt').write_text('synthetic archive content')
    (fixture / 'omit.txt').write_text('excluded')
    (fixture / '.hidden.txt').write_text('hidden')
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {once} = require('node:events');
const {createRequire} = require('node:module');
const archiverRequire = createRequire(require.resolve('archiver'));
const utils = archiverRequire('archiver-utils');
const readdir = archiverRequire('readdir-glob');
(async () => {
  assert.deepEqual(utils.file.expand({cwd:'fixture'}, ['*.txt', '!omit.txt']), ['keep.txt']);
  const files = await new Promise((resolve,reject) => readdir('fixture',
    {pattern:'**/*.txt',ignore:'omit.txt',nodir:true}, (err,matches) => err ? reject(err) : resolve(matches)));
  assert.deepEqual(files, ['keep.txt']);
  const output = fs.createWriteStream('synthetic.zip');
  const archive = require('archiver')('zip');
  const finished = once(output, 'close');
  archive.on('error', error => output.destroy(error));
  archive.pipe(output);
  archive.glob('**/*.txt', {cwd:'fixture',ignore:['omit.txt'],dot:false});
  await archive.finalize();
  await finished;
  const zip = await require('unzipper').Open.file('synthetic.zip');
  assert.deepEqual(zip.files.map(file => file.path), ['keep.txt']);
  assert.equal((await zip.files[0].buffer()).toString(), 'synthetic archive content');
})().catch(error => {console.error(error);process.exitCode=1;});
'''
    subprocess.run(['node', '-e', script], cwd=tmp_path, env=env, capture_output=True,
        text=True, check=True, timeout=30)
