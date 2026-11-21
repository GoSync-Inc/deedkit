"""ANYM-697: the committed mini enrichment example promises the full 512 MiB audio original, the runtime's own bound,
so a fresh install matches the worker's working space. Pure file and profile validation; no inference or network.
ANYM-709: the mini worker's working space holds that original, and the worker copies exactly up to that bound."""
import hashlib
import json
import os
from pathlib import Path

import pytest
import yaml

# Historical test names and parameter IDs are stable Tiden identities; assertions follow the current limit.
ROOT = Path(__file__).resolve().parents[1]
AUDIO_LIMIT = 536_870_912


def example():
    # reads: deploy/enrichment-mini.example.json
    return json.loads((ROOT / 'deploy' / 'enrichment-mini.example.json').read_text())


def valid(profile, kind, tmp_path):
    from messaging_service.enrichment_providers import validate_profile
    key = tmp_path / 'key'
    key.write_text('synthetic-key')
    key.chmod(0o600)
    return validate_profile({**profile, 'api_key_file': str(key)}, kind)


# spec: ANYM-697
def test_the_example_audio_profile_carries_the_runtime_200_mib_original_bound(tmp_path):
    from messaging_service.enrichment_providers import AUDIO_ORIGINAL_MAX_BYTES
    audio = example()['profiles']['audio']
    assert audio['max_input_bytes'] == AUDIO_LIMIT == AUDIO_ORIGINAL_MAX_BYTES
    assert valid(audio, 'audio', tmp_path)['max_input_bytes'] == AUDIO_LIMIT


# spec: ANYM-697
def test_the_example_audio_profile_one_byte_over_is_refused_by_the_profile_validation(tmp_path):
    audio = example()['profiles']['audio']
    with pytest.raises(ValueError, match='^invalid_model_limit$'):
        valid({**audio, 'max_input_bytes': audio['max_input_bytes'] + 1}, 'audio', tmp_path)


def tmpfs_bytes(entry):
    """The size of a Compose `tmpfs` entry such as `/tmp:size=640m,noexec,nosuid`, in bytes."""
    options = dict(item.partition('=')[::2] for item in entry.partition(':')[2].split(','))
    number, unit = options['size'][:-1], options['size'][-1].lower()
    return int(number) * 1024 ** {'k': 1, 'm': 2, 'g': 3}[unit]


def overlay_tmpfs():
    # reads: deploy/compose.enrichment.yaml
    document = yaml.safe_load((ROOT / 'deploy' / 'compose.enrichment.yaml').read_text())
    [mini] = document['services']['enricher-mini']['tmpfs']
    [common] = document['x-enrichment-common']['tmpfs']
    return tmpfs_bytes(mini), tmpfs_bytes(common)


# spec: ANYM-709
def test_the_mini_working_space_holds_the_largest_audio_original_with_its_upload_and_result(tmp_path):
    """A 200 MiB profile in a 128 MiB /tmp fails every large note with `enrichment_failed` (ANYM-701). The worker's
    peak in /tmp is the verified copy of the original, the transcoded Opus upload beside it and the child's result."""
    from messaging_service.enrichment import MAX_RESULT_BYTES
    from messaging_service.enrichment_providers import UPLOAD_LIMIT_BYTES
    mini, common = overlay_tmpfs()
    largest = valid(example()['profiles']['audio'], 'audio', tmp_path)['max_input_bytes']
    assert mini == 640 * 1024 ** 2 and largest == AUDIO_LIMIT
    assert largest + UPLOAD_LIMIT_BYTES + MAX_RESULT_BYTES <= mini
    # The other workers keep 128 MiB: the mini's audio profile would not fit there (the state before the release).
    assert common == 128 * 1024 ** 2 < largest


def sparse_original(files, size):
    """A synthetic original of `size` zero bytes stored under its digest; sparse, so only its copy is written."""
    path = files / 'incoming'
    with path.open('wb') as handle:
        handle.seek(size - 1)  # a hole up to the last byte
        handle.write(b'\0')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while block := handle.read(1 << 20):
            digest.update(block)
    os.replace(path, files / digest.hexdigest())
    return {'source_digest': digest.hexdigest(), 'bytes': size}


# spec: ANYM-709
@pytest.mark.parametrize('extra,accepted', [(0, True), (1, False)], ids=['200_mib', 'one_byte_more'])
def test_the_example_audio_profile_copies_a_200_mib_original_and_refuses_one_byte_more_as_final(tmp_path, extra, accepted):
    from messaging_service.enrichment import FINAL_LIMIT_CODES, verified_copy
    from messaging_service.enrichment_providers import ModelError
    limit = valid(example()['profiles']['audio'], 'audio', tmp_path)['max_input_bytes']
    files, work = tmp_path / 'files', tmp_path / 'work'
    files.mkdir()
    work.mkdir()
    row = sparse_original(files, AUDIO_LIMIT + extra)
    if accepted:
        assert verified_copy(files, row, work, limit).stat().st_size == AUDIO_LIMIT
    else:
        with pytest.raises(ModelError, match='^original_size_limit$'):
            verified_copy(files, row, work, limit)
        # Refused before any byte is copied, with the code a claim never retries under this profile.
        assert (work / 'original').stat().st_size == 0 and 'original_size_limit' in FINAL_LIMIT_CODES
