"""Regression: a recording slightly above 200 MiB must fit an explicit audio profile."""
import hashlib
import pytest

from messaging_service.enrichment import verified_copy
from messaging_service.enrichment_providers import validate_profile


def audio_profile(tmp_path, size):
    key = tmp_path / 'key'
    key.write_text('synthetic-key')
    key.chmod(0o600)
    return validate_profile({'provider': 'deedkit_inference', 'local_inference': True,
        'model': 'audio-default', 'base_url': 'https://mini.example.ts.net/v1',
        'api_key_file': str(key), 'max_input_bytes': size}, 'audio')


# spec: ANYM-697
def test_explicit_audio_limit_accepts_512_mib_and_refuses_the_next_byte(tmp_path):
    limit = 512 * 1024**2
    assert audio_profile(tmp_path, limit)['max_input_bytes'] == limit
    with pytest.raises(ValueError, match='invalid_model_limit'):
        audio_profile(tmp_path, limit + 1)


# spec: ANYM-697
def test_a_226_mb_synthetic_original_is_verified_without_loading_it_into_memory(tmp_path):
    maximum = audio_profile(tmp_path, 512 * 1024**2)['max_input_bytes']
    files, work = tmp_path / 'files', tmp_path / 'work'
    files.mkdir(); work.mkdir()
    original = files / 'incoming'
    with original.open('wb') as stream:
        stream.seek(226_069_100 - 1)
        stream.write(b'\0')
    with original.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    original.rename(files / digest)
    row = {'source_digest': digest, 'bytes': 226_069_100}
    copied = verified_copy(files, row, work, maximum)
    with copied.open('rb') as stream:
        assert hashlib.file_digest(stream, 'sha256').hexdigest() == digest
    assert copied.stat().st_size == row['bytes']
