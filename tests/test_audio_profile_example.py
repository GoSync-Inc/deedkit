"""ANYM-697: the committed mini enrichment example promises the full 200 MiB audio original, the runtime's own bound,
so a fresh install matches the worker's working space. Pure file and profile validation; no inference or network."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MIB_200 = 209_715_200


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
    assert audio['max_input_bytes'] == MIB_200 == AUDIO_ORIGINAL_MAX_BYTES
    assert valid(audio, 'audio', tmp_path)['max_input_bytes'] == MIB_200


# spec: ANYM-697
def test_the_example_audio_profile_one_byte_over_is_refused_by_the_profile_validation(tmp_path):
    audio = example()['profiles']['audio']
    with pytest.raises(ValueError, match='^invalid_model_limit$'):
        valid({**audio, 'max_input_bytes': audio['max_input_bytes'] + 1}, 'audio', tmp_path)
