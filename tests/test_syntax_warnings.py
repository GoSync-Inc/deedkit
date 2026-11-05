"""No tracked Python file compiles with a SyntaxWarning: an invalid escape prints on every import and hides real warnings."""
import json
import os
from pathlib import Path
import subprocess
import warnings

import pytest

from scripts.added_tests import EVIDENCE, Refused, inventory

ROOT = Path(__file__).resolve().parents[1]


def tracked(root):
    listing = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--', '*.py'], check=True, capture_output=True)
    return [name.decode() for name in listing.stdout.split(b'\0') if name]


def excluded(temp):
    """The files this CI job deleted before collecting tests (scripts/added_tests.py strip); no inventory, no skips."""
    if not temp:return set()
    path = Path(temp) / 'ci-evidence' / EVIDENCE
    return set(inventory(path.read_text())['files']) if path.exists() else set()


def problems(root, temp):
    """`file:line: message` of each tracked file that does not compile with SyntaxWarning as an error."""
    skipped, found = excluded(temp), []
    for name in tracked(root):
        path = Path(root) / name
        if not path.is_file():
            if name not in skipped:found.append(f'{name}: tracked but missing from the checkout')
            continue
        with warnings.catch_warnings():
            warnings.simplefilter('error', SyntaxWarning)
            try:compile(path.read_bytes(), name, 'exec')
            except SyntaxError as error:found.append(f'{name}:{error.lineno}: {error.msg}')
    return found


def repository(root, files):
    root.mkdir()
    for name, content in files.items():
        (root / name).write_bytes(content)
    subprocess.run(['git', '-C', str(root), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(root), 'add', '.'], check=True)
    return root


def job_inventory(temp, files):
    evidence = temp / 'ci-evidence'
    evidence.mkdir()
    (evidence / EVIDENCE).write_text(json.dumps({'files': files, 'provenance': []}))
    return temp


# spec: ANYM-803
def test_no_tracked_python_file_compiles_with_a_syntax_warning():
    assert problems(ROOT, os.environ.get('RUNNER_TEMP')) == []


# spec: ANYM-803
def test_an_invalid_escape_is_named_by_file_and_line_and_py_txt_fixtures_are_not_compiled(tmp_path):
    invalid = b'x = 1\ny = "\\`"\n'
    root = repository(tmp_path / 'source', {'clean.py': b'x = 1\n', 'bad.py': invalid, 'fixture.py.txt': invalid})
    found = problems(root, None)
    assert len(found) == 1 and found[0].startswith('bad.py:2: ') and 'invalid escape sequence' in found[0]


# spec: ANYM-803
def test_a_missing_file_listed_in_the_inventory_is_skipped(tmp_path):
    root = repository(tmp_path / 'source', {'added.py': b'x = 1\n'})
    (root / 'added.py').unlink()
    assert problems(root, job_inventory(tmp_path, ['added.py'])) == []


# spec: ANYM-803
def test_a_missing_file_the_inventory_does_not_list_fails_by_name(tmp_path):
    root = repository(tmp_path / 'source', {'added.py': b'x = 1\n', 'lost.py': b'x = 1\n'})
    (root / 'added.py').unlink()
    (root / 'lost.py').unlink()
    assert problems(root, job_inventory(tmp_path, ['added.py'])) == ['lost.py: tracked but missing from the checkout']


# spec: ANYM-803
@pytest.mark.parametrize('temp', ['absent inventory', None], ids=['no-inventory-file', 'no-runner-temp'])
def test_an_absent_inventory_means_no_skips(tmp_path, temp):
    root = repository(tmp_path / 'source', {'lost.py': b'x = 1\n'})
    (root / 'lost.py').unlink()
    assert problems(root, temp and tmp_path) == ['lost.py: tracked but missing from the checkout']


# spec: ANYM-803
def test_an_inventory_listing_a_present_file_with_an_invalid_escape_still_fails(tmp_path):
    root = repository(tmp_path / 'source', {'bad.py': b'y = "\\`"\n'})
    assert [line.split(': ')[0] for line in problems(root, job_inventory(tmp_path, ['bad.py']))] == ['bad.py:1']


# spec: ANYM-803
def test_a_malformed_inventory_is_refused_rather_than_read_as_empty(tmp_path):
    root = repository(tmp_path / 'source', {'added.py': b'x = 1\n'})
    (root / 'added.py').unlink()
    (tmp_path / 'ci-evidence').mkdir()
    (tmp_path / 'ci-evidence' / EVIDENCE).write_text('{"files": ["added.py"]}')
    with pytest.raises(Refused):
        problems(root, tmp_path)
