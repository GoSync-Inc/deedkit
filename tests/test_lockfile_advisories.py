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
REQUIREMENTS_LOCKS = tuple(sorted(str(path.relative_to(ROOT))
    for path in (ROOT / 'services/messaging').glob('requirements*.lock')))
# package -> (first fixed version, advisory)
FIXED = {'certifi': ('2026.10.19', 'PYSEC-2026-4320')}


def _version(text):
    return tuple(int(part) for part in text.split('.'))


def _locked(path):
    text = (ROOT / path).read_text(encoding='utf-8')
    if path.endswith('uv.lock'):
        return {item['name']: item['version'] for item in tomllib.loads(text).get('package', ())
            if 'version' in item}
    return {match[1].lower(): match[2] for match in re.finditer(r'(?m)^([A-Za-z0-9_.-]+)==([^\s\\;]+)', text)}


# spec: ANYM-137
@pytest.mark.parametrize('path', UV_LOCKS + REQUIREMENTS_LOCKS)
def test_lockfile_pins_no_version_below_a_known_advisory_fix(path):
    locked = _locked(path)
    assert locked or not path.endswith('uv.lock'), path
    for name, (fixed, advisory) in FIXED.items():
        if name in locked:
            assert _version(locked[name]) >= _version(fixed), f'{path}: {name} {locked[name]} < {fixed} ({advisory})'
