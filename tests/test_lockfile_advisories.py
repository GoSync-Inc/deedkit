"""Every committed Python lockfile stays above the fixed version of known advisories.

The lockfiles are what the images install with `--require-hashes`, so a vulnerable
pin here ships to production even when the pyproject range would allow the fix.
"""
from pathlib import Path
import re
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
UV_LOCKS = ('uv.lock', 'services/messaging/uv.lock', 'services/gateway/uv.lock',
    'services/calls/uv.lock', 'services/calendar/uv.lock', 'tools/dev/uv.lock')
REQUIREMENTS_LOCKS = ('services/messaging/requirements.lock', 'services/messaging/requirements-test.lock',
    'services/messaging/requirements-enrichment.lock', 'services/messaging/requirements-telemetry.lock',
    'services/messaging/requirements-backup-r2.lock')
# package -> (first fixed version, advisory)
FIXED = {'certifi': ('2026.10.19', 'PYSEC-2026-4320')}
PIN = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*==\s*([^\s=<>!~,;]+)')


def _version(text):
    return tuple(int(part) for part in text.split('.'))


def _name(text):
    return re.sub(r'[-_.]+', '-', text).lower()


def _pins(text, uv):
    """Every (package, version) occurrence; any requirements line that is not an exact pin fails."""
    if uv:
        return [(_name(item['name']), item['version']) for item in tomllib.loads(text).get('package', ())
            if 'version' in item]
    pins = []
    for line in re.sub(r'\\\n', ' ', text).splitlines():
        line = re.sub(r'\s--hash=\S+', '', line.split(' #', 1)[0]).split(';', 1)[0].strip()
        if not line or line.startswith(('#', '--')):
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
    uv = ('[[package]]\nname = "certifi"\nversion = "2026.7.22"\n\n'
        '[[package]]\nname = "certifi"\nversion = "2026.10.23"\n')
    assert _below_fix(_pins(uv, True)) == ['certifi 2026.7.22 < 2026.10.19 (PYSEC-2026-4320)']
    for line in ('certifi>=2026.1.1\n', '-e ./certifi\n', 'certifi\n'):
        with pytest.raises(AssertionError, match='unsupported'):
            _pins(line, False)


# spec: ANYM-137
def test_lockfile_advisory_check_lists_every_committed_lockfile():
    found = set()
    for directory, dirs, files in ROOT.walk():
        dirs[:] = [name for name in dirs if not name.startswith('.') and name not in {'node_modules', '__pycache__'}]
        found |= {str((directory / name).relative_to(ROOT)) for name in files
            if name == 'uv.lock' or re.fullmatch(r'requirements[^/]*\.lock', name)}
    assert found == set(UV_LOCKS + REQUIREMENTS_LOCKS)
