"""The enrichment config loader's own boundaries (ANYM-70), on private synthetic files only: no model, key, binary or
provider is read, started or reached. `enrichment.configuration` and `validate_profile` are pure: a key file, a model
directory and a Codex binary are named by absolute path and never opened while loading.

Covered elsewhere and not repeated: the budget maps and backlog share (test_processing_admission), Codex's own
refusals of base_url, api_key_file and relative paths (test_codex_enrichment), the deedkit_inference endpoint and key
rules (test_enrichment), timeout and audio-duration limits and the per-kind input maxima (test_enrichment, ANYM-697,
ANYM-702), and Ollama locality (test_enrichment)."""
import errno
import json
import os

import pytest

from messaging_service.enrichment import configuration
from messaging_service.enrichment_providers import validate_profile

LIMIT = 65536


def write(path, raw, mode=0o600):
    if path.exists():
        path.chmod(0o600)  # a read-only mode from the previous case
    path.write_text(raw if isinstance(raw, str) else json.dumps(raw))
    path.chmod(mode)
    return path


def base(tmp_path, **changes):
    return {'enabled': True, 'accounts': ['synthetic-account'],
            'profiles': {'audio': {'provider': 'faster_whisper', 'model': str(tmp_path / 'installed-model')}},
            **changes}


def cloud(tmp_path, provider, **changes):
    return {'provider': provider, 'model': 'fixture-model', 'allow_cloud': True,
            'api_key_file': str(tmp_path / 'never-opened.key'), **changes}


# spec: ANYM-70
def test_the_config_file_is_a_private_regular_file_of_at_most_64_kib(tmp_path):
    config = base(tmp_path)
    exact = json.dumps(config)
    exact += ' ' * (LIMIT - len(exact.encode()))
    assert len(exact.encode()) == LIMIT
    path = write(tmp_path / 'enrichment.json', exact)
    assert configuration(path)['profiles']['audio']['provider'] == 'faster_whisper'
    write(path, exact + ' ')
    with pytest.raises(ValueError, match='^private_config_required$'):
        configuration(path)
    for mode in (0o600, 0o400):
        assert configuration(write(path, config, mode))['accounts'] == ['synthetic-account']
    for mode in (0o640, 0o620, 0o610, 0o604, 0o602, 0o601):
        with pytest.raises(ValueError, match='^private_config_required$'):
            configuration(write(path, config, mode))
    link = tmp_path / 'linked.json'
    link.symlink_to(write(path, config))
    with pytest.raises(OSError) as refused:
        configuration(link)
    assert refused.value.errno == errno.ELOOP
    fifo = tmp_path / 'enrichment.fifo'
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match='^private_config_required$'):
        configuration(fifo)


# spec: ANYM-70
def test_unknown_keys_accounts_and_the_daily_budget_are_bounded(tmp_path):
    path = tmp_path / 'enrichment.json'
    loaded = configuration(write(path, base(tmp_path)))
    assert loaded['max_jobs_per_day'] == 20 and loaded['enabled'] is True
    widest = base(tmp_path, accounts=[f'{i:02d}' + 'a' * 62 for i in range(100)], max_jobs_per_day=10000)
    assert configuration(write(path, widest))['max_jobs_per_day'] == 10000
    profile = base(tmp_path)['profiles']['audio']
    for changed, code in [
            (base(tmp_path, unexpected=True), 'invalid_enrichment_config'),
            (base(tmp_path, profiles={'video': profile}), 'invalid_enrichment_profiles'),
            (base(tmp_path, profiles={}), 'invalid_enrichment_profiles'),
            (base(tmp_path, profiles={'audio': {**profile, 'unexpected': 1}}), 'invalid_model_profile'),
            (base(tmp_path, enabled='yes'), 'enrichment_disabled'),
            (base(tmp_path, accounts=[]), 'enrichment_accounts_required'),
            (base(tmp_path, accounts=['a'] * 101), 'enrichment_accounts_required'),
            (base(tmp_path, accounts=['']), 'enrichment_accounts_required'),
            (base(tmp_path, accounts=['a' * 65]), 'enrichment_accounts_required'),
            (base(tmp_path, max_jobs_per_day=0), 'invalid_enrichment_budget'),
            (base(tmp_path, max_jobs_per_day=10001), 'invalid_enrichment_budget'),
            (base(tmp_path, max_jobs_per_day=True), 'invalid_enrichment_budget')]:
        with pytest.raises(ValueError, match='^' + code + '$'):
            configuration(write(path, changed))


# spec: ANYM-70
def test_audio_takes_only_faster_whisper_openai_or_openai_compatible_and_codex_serves_image_and_document(tmp_path):
    codex = {'provider': 'codex_cli', 'model': 'fixture-model', 'allow_cloud': True, 'isolated_runtime': True,
             'codex_home': str(tmp_path / 'codex-home'), 'codex_binary': str(tmp_path / 'never-run-codex')}
    for kind in ('image', 'document'):
        assert validate_profile(codex, kind)['prompt_version'] == 'codex-file-description-v1'
    audio = {'faster_whisper': {'provider': 'faster_whisper', 'model': str(tmp_path / 'installed-model')},
             'openai': cloud(tmp_path, 'openai'),
             'openai_compatible': cloud(tmp_path, 'openai_compatible', base_url='https://models.example.invalid/v1')}
    for provider, profile in audio.items():
        assert validate_profile(profile, 'audio')['provider'] == provider
    for profile in (codex, cloud(tmp_path, 'anthropic'), cloud(tmp_path, 'gemini'),
                    {'provider': 'ollama', 'model': 'local-model', 'local_only_daemon': True}):
        with pytest.raises(ValueError, match='^provider_does_not_support_asr$'):
            validate_profile(profile, 'audio')
    with pytest.raises(ValueError, match='^local_model_directory_required$'):
        validate_profile(audio['faster_whisper'], 'image')


# spec: ANYM-70
@pytest.mark.parametrize('provider', ['openai', 'openai_compatible', 'anthropic', 'gemini'])
def test_a_cloud_provider_needs_an_absolute_key_file_that_loading_never_opens(tmp_path, provider):
    extra = {'base_url': 'https://models.example.invalid/v1'} if provider == 'openai_compatible' else {}
    profile = cloud(tmp_path, provider, **extra)
    assert not os.path.exists(profile['api_key_file'])
    assert validate_profile(profile, 'image')['api_key_file'] == profile['api_key_file']
    for key_file in (None, 'keys/model.key', './model.key', '', 7):
        changed = dict(profile, api_key_file=key_file) if key_file is not None else {
            k: v for k, v in profile.items() if k != 'api_key_file'}
        with pytest.raises(ValueError, match='^api_key_file_required$'):
            validate_profile(changed, 'image')
    with pytest.raises(ValueError, match='^cloud_processing_not_enabled$'):
        validate_profile({k: v for k, v in profile.items() if k != 'allow_cloud'}, 'image')


# spec: ANYM-70
@pytest.mark.parametrize('endpoint,accepted', [
    ('https://models.example.invalid/v1/', 'https://models.example.invalid/v1'),
    ('http://127.0.0.1:8080/v1', 'http://127.0.0.1:8080/v1'),
    ('http://localhost:8080/v1', 'http://localhost:8080/v1'),
    ('http://[::1]:8080/v1', 'http://[::1]:8080/v1'),
    ('http://models.example.invalid/v1', None),
    ('http://127.0.0.2:8080/v1', None),
    ('https://user:synthetic@models.example.invalid/v1', None),
    ('https://user@models.example.invalid/v1', None),
    ('https://models.example.invalid/v1?key=synthetic', None),
    ('https://models.example.invalid/v1#fragment', None),
    ('ftp://models.example.invalid/v1', None),
    ('https:///v1', None),
    ('models.example.invalid/v1', None),
])
def test_an_endpoint_is_https_or_loopback_http_without_credentials_query_or_fragment(tmp_path, endpoint, accepted):
    profile = cloud(tmp_path, 'openai_compatible', base_url=endpoint)
    if accepted:
        assert validate_profile(profile, 'image')['base_url'] == accepted
    else:
        with pytest.raises(ValueError, match='^invalid_model_endpoint$'):
            validate_profile(profile, 'image')


# spec: ANYM-70
@pytest.mark.parametrize('key,default,minimum,maximum', [
    ('max_text_chars', 40000, 1, 100000), ('max_output_tokens', 2000, 1, 16000), ('memory_limit_mb', 2048, 512, 8192)])
def test_text_token_and_memory_limits_have_their_default_and_bounds(tmp_path, key, default, minimum, maximum):
    profile = cloud(tmp_path, 'openai')
    loaded = validate_profile(profile, 'image')
    assert loaded[key] == default
    assert {k: loaded[k] for k in ('timeout_seconds', 'max_audio_seconds', 'max_input_bytes')} == {
        'timeout_seconds': 120, 'max_audio_seconds': 3600, 'max_input_bytes': 20 * 1024 ** 2}
    for value in (minimum, maximum):
        assert validate_profile({**profile, key: value}, 'image')[key] == value
    for value in (minimum - 1, maximum + 1, True, float(default), str(default)):
        with pytest.raises(ValueError, match='^invalid_model_limit$'):
            validate_profile({**profile, key: value}, 'image')
