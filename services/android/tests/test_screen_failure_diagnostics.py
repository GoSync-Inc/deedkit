"""A failed Tesseract run names a fixed internal cause and nothing raw; the public refusal stays ocr_failed."""

import io
from pathlib import Path
import subprocess
import sys
import traceback
from types import SimpleNamespace

from PIL import Image
import pytest

from deedkit_android.config import Refused

CANARY = 'canary-raw-ocr-content'  # stands for a path, stderr or recognized text that must never surface


def image():
    return Image.new('RGB', (64, 48), 'white')


def script(tmp_path, body):
    path = tmp_path / f'{CANARY}-engine'
    path.write_text(f'#!{sys.executable}\nimport os, signal, sys\n{body}\n')
    path.chmod(0o700)
    return {'command': str(path), 'languages': 'eng'}


def refused(ocr):
    from deedkit_android.screen_text import tesseract_lines

    with pytest.raises(Refused) as failure:
        tesseract_lines(image(), ocr)
    error = failure.value
    # Public semantics are unchanged: the same code, status and message, and no field a response or the worker sends.
    assert (error.code, error.status, str(error), error.args) == ('ocr_failed', 409, 'ocr_failed', ('ocr_failed',))
    assert not any(hasattr(error, key) for key in ('hint', 'details', 'extra', 'field_errors', 'diagnostic', 'guard'))
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)  # no chained OSError text
    rendered = ''.join(traceback.format_exception(error))
    assert CANARY not in rendered and 'stdout' not in ''.join(error.__notes__)
    note, = error.__notes__
    return error.ocr_cause, note


# spec: ANYM-955
def test_engine_that_cannot_start_is_start_failed_with_the_errno_name_only(tmp_path):
    cause, note = refused({'command': str(tmp_path / CANARY / 'tesseract'), 'languages': 'eng'})
    assert cause == 'start_failed'
    assert note.startswith('ocr_cause=start_failed errno=ENOENT type=FileNotFoundError elapsed_ms=')


# spec: ANYM-955
def test_engine_exit_status_is_exit_nonzero_without_its_stderr(tmp_path):
    cause, note = refused(script(tmp_path, f'sys.stderr.write({CANARY!r}); sys.stdout.write({CANARY!r}); sys.exit(3)'))
    assert cause == 'exit_nonzero' and note.startswith('ocr_cause=exit_nonzero exit=3 elapsed_ms=')


# spec: ANYM-955
def test_engine_killed_by_a_signal_is_exit_nonzero_with_the_signal_name(tmp_path):
    cause, note = refused(script(tmp_path, f'sys.stderr.write({CANARY!r}); os.kill(os.getpid(), signal.SIGTERM)'))
    assert cause == 'exit_nonzero' and note.startswith('ocr_cause=exit_nonzero signal=SIGTERM elapsed_ms=')


# spec: ANYM-955
def test_engine_past_its_limit_is_timeout_and_drops_the_partial_output(tmp_path, monkeypatch):
    from deedkit_android import screen_text

    seen = []

    def run(argv, **options):
        seen.append(options['timeout'])
        raise subprocess.TimeoutExpired(argv, options['timeout'], output=CANARY.encode(), stderr=CANARY.encode())

    monkeypatch.setattr(screen_text.subprocess, 'run', run)
    cause, note = refused({'command': str(tmp_path / CANARY), 'languages': 'eng'})
    assert seen == [20] and screen_text.OCR_TIMEOUT_S == 20  # the limit is unchanged
    assert cause == 'timeout' and note.startswith('ocr_cause=timeout limit_s=20 elapsed_ms=')


# spec: ANYM-955
def test_read_screen_text_still_refuses_ocr_failed_and_removes_the_image(tmp_path):
    from deedkit_android.ui_worker import execute

    target = io.BytesIO()
    image().save(target, format='PNG')
    record = tmp_path / 'argv'
    ocr = script(tmp_path, f'open({str(record)!r}, "w").write(sys.argv[1]); sys.exit(1)')

    class Device:
        _dev = property(lambda self: self)

        def shell2(self, args, timeout=None, encoding='utf-8'):
            return SimpleNamespace(returncode=0, output=target.getvalue())

        def jsonrpc_call(self, method, params, timeout=10):
            raise RuntimeError('no tree')

    with pytest.raises(Refused) as failure:
        execute(Device(), {'readScreenText': {}}, ocr=ocr)
    assert failure.value.code == 'ocr_failed' and failure.value.ocr_cause == 'exit_nonzero'
    assert not Path(record.read_text()).parent.exists()
