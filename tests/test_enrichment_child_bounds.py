"""The enrichment child's own guards, proven on a real `--child` process with a synthetic model call.

`process()` starts the real child entry (`messaging_service.enrichment.main --child`) in its own process. The only
change is that the child's `infer` is a synthetic function written here, so no provider, model or network is reached.
Resource limits are set inside that child only; the test process keeps its own limits.
"""
import errno
import hashlib
import resource
import subprocess
import sys

import pytest

import messaging_service.enrichment as worker_module
from messaging_service.enrichment import process, verified_copy
from messaging_service.enrichment_providers import ModelError, validate_profile

ENTRY_TAIL = 'from messaging_service.enrichment import main;main()'
LIMITS = ('CPU', 'FSIZE', 'NOFILE', 'CORE', 'AS')
MIB = 1024 ** 2


def synthetic_profile(tmp_path, **limits):
    key = tmp_path / 'model-key'; key.write_text('synthetic-key'); key.chmod(0o600)
    return validate_profile({'provider': 'openai', 'model': 'synthetic-model', 'allow_cloud': True,
                             'api_key_file': str(key), 'timeout_seconds': 30, **limits}, 'image')


def stored_original(tmp_path, data=b'synthetic-original'):
    files = tmp_path / 'files'; files.mkdir(exist_ok=True)
    digest = hashlib.sha256(data).hexdigest(); (files / digest).write_bytes(data)
    return files, {'source_digest': digest, 'bytes': len(data), 'kind': 'image', 'mime': 'image/png'}


def run_child(monkeypatch, tmp_path, infer_source, **limits):
    """Run process() with the real child entry; the child's infer is `infer_source`. Returns (result, spawned)."""
    real_popen, spawned = subprocess.Popen, []
    def popen(args, **kwargs):
        assert list(args[:3]) == [sys.executable, '-I', '-c'] and list(args[4:]) == ['--child']
        assert args[3].endswith(ENTRY_TAIL)  # the seam replaces only the model call, never the entry
        entry = args[3][:-len(ENTRY_TAIL)] + '\nimport messaging_service.enrichment as worker\n' + infer_source \
            + '\nworker.infer=infer\nworker.main()\n'
        spawned.append(entry)
        return real_popen([*args[:3], entry, *args[4:]], **kwargs)
    monkeypatch.setattr(worker_module.subprocess, 'Popen', popen)
    files, row = stored_original(tmp_path)
    return process(files, row, synthetic_profile(tmp_path, **limits)), spawned


PROBE_LIMITS = '''
import errno, mmap, os, resource
def infer(profile, kind, path, mime, source_text):
    seen = {name: list(resource.getrlimit(getattr(resource, 'RLIMIT_' + name))) for name in ('CPU', 'CORE')}
    bound = max(8 * 1024 ** 2, profile['max_input_bytes'])
    with open('probe', 'wb', buffering=0) as f:
        f.seek(bound - 1); f.write(b'x')
        f.seek(bound)
        try: f.write(b'x'); seen['write_past_bound'] = 'written'
        except OSError as exc: seen['write_past_bound'] = errno.errorcode[exc.errno]
    opened = []
    try:
        for _ in range(4096): opened.append(os.open(os.devnull, os.O_RDONLY))
        seen['open_files'] = 'unbounded'
    except OSError as exc: seen['open_files'] = errno.errorcode[exc.errno]
    seen['highest_fd'] = max(opened)
    for fd in opened: os.close(fd)
    for name, size in (('small_map', 64 * 1024 ** 2), ('profile_memory_map', profile['memory_limit_mb'] * 1024 ** 2)):
        try: mmap.mmap(-1, size).close(); seen[name] = 'mapped'
        except (OSError, MemoryError) as exc: seen[name] = type(exc).__name__
    return seen
'''


# spec: ANYM-72
def test_the_child_runs_under_the_profile_cpu_file_descriptor_core_and_memory_limits(tmp_path, monkeypatch):
    host = {name: resource.getrlimit(getattr(resource, 'RLIMIT_' + name)) for name in LIMITS}
    seen, spawned = run_child(monkeypatch, tmp_path, PROBE_LIMITS, memory_limit_mb=512, max_input_bytes=MIB)
    assert len(spawned) == 1
    assert seen['CPU'] == [70, 70] and seen['CORE'] == [0, 0]  # 2 x timeout_seconds + 10; no core dump
    # A write may reach max(8 MiB, max_input_bytes) and not one byte past it.
    assert seen['write_past_bound'] == 'EFBIG'
    # At most 128 descriptors: the 129th open fails.
    assert seen['open_files'] == 'EMFILE' and seen['highest_fd'] == 127
    if sys.platform.startswith('linux'):  # the address-space cap is Linux-only by design
        assert seen['small_map'] == 'mapped' and seen['profile_memory_map'] == 'OSError'
    # The limits were the child's alone.
    assert {name: resource.getrlimit(getattr(resource, 'RLIMIT_' + name)) for name in LIMITS} == host


# spec: ANYM-72
def test_a_model_answer_over_8_mib_is_refused_by_the_child_before_it_is_written(tmp_path, monkeypatch):
    # With max_input_bytes 1 MiB the child may write at most 8 MiB, so only the child's own size check can name it.
    large = "def infer(profile, kind, path, mime, source_text):\n    return {'text': 'a' * (8 * 1024 ** 2)}\n"
    with pytest.raises(ModelError, match='^model_response_too_large$'):
        run_child(monkeypatch, tmp_path, large, max_input_bytes=MIB)


RAW_OUTPUT = '''
import os, sys
def infer(profile, kind, path, mime, source_text):
    sys.stdout.buffer.write(%s); sys.stdout.buffer.flush(); os._exit(0)
'''


# spec: ANYM-72
def test_child_output_over_8_mib_is_refused_by_the_parent_however_it_was_written(tmp_path, monkeypatch):
    # The child writes past its own check straight to stdout; the parent reads at most 8 MiB + 1 and refuses it.
    with pytest.raises(ModelError, match='^model_response_too_large$'):
        # The body is built in the child: an 8 MiB literal would not fit in one argv string.
        run_child(monkeypatch, tmp_path, RAW_OUTPUT % "b'{' + b' ' * (8 * 1024 ** 2)", max_input_bytes=20 * MIB)


# spec: ANYM-72
@pytest.mark.parametrize('output', [b'not json', b'{"text": "cut', b'["a list"]', b'"a string"', b''],
                         ids=['text', 'truncated', 'list', 'string', 'empty'])
def test_child_output_that_is_not_a_json_object_is_an_invalid_model_response(tmp_path, monkeypatch, output):
    with pytest.raises(ModelError, match='^invalid_model_response$'):
        run_child(monkeypatch, tmp_path, RAW_OUTPUT % repr(output))


# spec: ANYM-72
def test_a_symlinked_original_is_never_followed_or_given_to_the_child(tmp_path, monkeypatch):
    files, row = stored_original(tmp_path)
    target = tmp_path / 'elsewhere'; target.write_bytes(b'synthetic-original')  # same bytes, same digest
    (files / row['source_digest']).unlink(); (files / row['source_digest']).symlink_to(target)
    copy = tmp_path / 'copy'; copy.mkdir()
    with pytest.raises(OSError) as refused:
        verified_copy(files, row, copy, MIB)
    assert refused.value.errno == errno.ELOOP and list(copy.iterdir()) == []
    spawned = []
    monkeypatch.setattr(worker_module.subprocess, 'Popen', lambda *args, **kwargs: spawned.append(args))
    with pytest.raises(OSError):
        process(files, row, synthetic_profile(tmp_path))
    assert spawned == []
