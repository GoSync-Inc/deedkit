"""Native audio contract. Synthetic recordings only; provider traffic is mocked.

Run in Docker target enricher-test for required FFmpeg + CPU library coverage.
An optional preinstalled tiny model and synthetic speech WAV may be supplied via
ENRICHMENT_TEST_MODEL_DIR / ENRICHMENT_TEST_SPEECH_FILE; tests never download them.
"""
from email import policy
from email.parser import BytesParser
import hashlib
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import time
import wave

import httpx
import pytest

from messaging_service.enrichment_providers import ModelError, audio_duration, infer, validate_profile


@pytest.fixture(autouse=True)
def native_tools():
    missing = [name for name in ('ffmpeg', 'ffprobe') if not shutil.which(name)]
    if missing:
        if os.environ.get('REQUIRE_NATIVE_AUDIO') == '1':
            pytest.fail('Native audio smoke image is missing: ' + ', '.join(missing))
        pytest.skip('Run in the optional enricher-test image for native audio coverage')


def tone(tmp_path, seconds=1.25):
    path = tmp_path/'synthetic.wav'
    rate = 16000
    frames = bytearray()
    for i in range(round(seconds*rate)):
        frames.extend(struct.pack('<h', round(4000*math.sin(2*math.pi*440*i/rate))))
    with wave.open(str(path), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(frames)
    return path


def opus(tmp_path, seconds=1.25):
    source = tone(tmp_path, seconds)
    path = tmp_path/'synthetic.ogg'
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(source), '-c:a', 'libopus',
                    '-b:a', '32k', '-threads', '1', str(path)], check=True, timeout=10,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def profile(tmp_path, **limits):
    key = tmp_path/'synthetic-key'; key.write_text('synthetic-key'); key.chmod(0o600)
    return validate_profile({'provider': 'openai', 'model': 'fixture-asr', 'allow_cloud': True,
        'api_key_file': str(key), 'max_audio_seconds': 2, **limits}, 'audio')


def multipart(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b'MIME-Version: 1.0\r\nContent-Type: '+request.headers['content-type'].encode()+
        b'\r\n\r\n'+request.read())
    fields = {}
    for part in message.iter_parts():
        fields[part.get_param('name', header='content-disposition')] = part
    return fields


@pytest.mark.parametrize('mime', ['audio/wav', 'audio/ogg', 'audio/opus'])
def test_native_duration_and_complete_provider_upload_preserve_original(tmp_path, mime):
    path = tone(tmp_path) if mime == 'audio/wav' else opus(tmp_path)
    original = path.read_bytes(); sha = hashlib.sha256(original).hexdigest()
    duration = audio_duration(path, 2)
    assert abs(duration-1.25) < 0.03
    calls = []
    def respond(request):
        calls.append(request.url.path)
        assert request.method == 'POST' and request.url.path == '/v1/audio/transcriptions'
        assert request.headers['authorization'] == 'Bearer synthetic-key'
        parts = multipart(request)
        assert set(parts) == {'file', 'model', 'response_format'}
        assert parts['model'].get_payload(decode=True) == b'fixture-asr'
        data = parts['file'].get_payload(decode=True)
        if mime == 'audio/wav':
            assert parts['file'].get_filename() == 'recording.wav' and data == original
        else:
            assert parts['file'].get_filename() == 'recording.mp3'
            assert parts['file'].get_content_type() == 'audio/mpeg' and data != original
            uploaded = tmp_path/'uploaded.mp3'; uploaded.write_bytes(data)
            assert abs(audio_duration(uploaded, 3)-duration) <= 0.25
        return httpx.Response(200, json={'text': 'Synthetic transcript.',
            'segments': [{'start': 0, 'end': 1.25, 'text': 'Synthetic transcript.'}]})
    result = infer(profile(tmp_path), 'audio', path, mime, transport=httpx.MockTransport(respond))
    assert calls == ['/v1/audio/transcriptions'] and result['text'] == 'Synthetic transcript.'
    metadata = result['metadata']
    assert metadata['duration_seconds'] == pytest.approx(duration)
    assert metadata['transcoded'] is (mime != 'audio/wav')
    assert metadata['transcode_lossy'] is metadata['transcoded'] and metadata['audio_truncated'] is False
    assert metadata['transcode_format'] == (None if mime == 'audio/wav' else 'mp3')
    assert metadata['segments'][0]['end'] == 1.25
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha


@pytest.mark.parametrize('mime', ['audio/wav', 'audio/ogg'])
def test_native_duration_limit_rejects_before_any_provider_call(tmp_path, mime):
    path = tone(tmp_path, 2.25) if mime == 'audio/wav' else opus(tmp_path, 2.25)
    before = path.read_bytes()
    def forbidden(_):
        pytest.fail('Overlong recordings must never be submitted')
    with pytest.raises(ModelError, match='^audio_duration_limit$'):
        infer(profile(tmp_path, max_audio_seconds=1), 'audio', path, mime,
              transport=httpx.MockTransport(forbidden))
    assert path.read_bytes() == before


def test_native_ogg_transcode_size_limit_never_submits_partial_audio(tmp_path):
    path = opus(tmp_path)
    limit = path.stat().st_size+512
    def forbidden(_):
        pytest.fail('An oversized conversion must not be submitted or truncated')
    with pytest.raises(ModelError, match='^audio_transcode_size_limit$'):
        infer(profile(tmp_path, max_input_bytes=limit), 'audio', path, 'audio/ogg',
              transport=httpx.MockTransport(forbidden))
    assert audio_duration(path, 2) == pytest.approx(1.25, abs=0.03)


def test_invalid_native_audio_does_not_reach_provider(tmp_path):
    path = tmp_path/'invalid'; path.write_bytes(b'Synthetic invalid recording')
    def forbidden(_):
        pytest.fail('Invalid audio must never be submitted')
    with pytest.raises(ModelError, match='^invalid_audio$'):
        infer(profile(tmp_path), 'audio', path, 'audio/wav', transport=httpx.MockTransport(forbidden))


# spec: ANYM-395
@pytest.mark.parametrize('kind', ['no_audio_stream', 'no_duration'])
def test_readable_file_without_audio_stream_or_duration_does_not_reach_provider(tmp_path, kind):
    if kind == 'no_audio_stream':
        path, mime = tmp_path/'synthetic.mp4', 'video/mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-f', 'lavfi', '-i', 'color=c=black:s=16x16:d=1',
                        '-c:v', 'mpeg4', '-threads', '1', str(path)], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        path, mime = tmp_path/'synthetic.wav', 'audio/wav'
        with wave.open(str(path), 'wb') as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(b'')
    def forbidden(_):
        pytest.fail('Audio without a known duration must never be submitted')
    with pytest.raises(ModelError, match='^audio_duration_unknown$'):
        infer(profile(tmp_path), 'audio', path, mime, transport=httpx.MockTransport(forbidden))


def test_optional_cpu_package_loads_without_gpu_or_model_download():
    if os.environ.get('REQUIRE_NATIVE_AUDIO') != '1':
        pytest.importorskip('faster_whisper')
    from faster_whisper import WhisperModel
    import ctranslate2
    assert callable(WhisperModel) and 'int8' in ctranslate2.get_supported_compute_types('cpu')


@pytest.mark.parametrize('container', ['wav', 'ogg'])
def test_faster_whisper_decoder_compatibility_without_model(tmp_path, container):
    # Import alone does not validate the native decoder API. In particular,
    # PyAV 19 imports successfully but rejects faster-whisper 1.2.1's
    # metadata_errors argument. Exercise the real path without model weights.
    if os.environ.get('REQUIRE_NATIVE_AUDIO') != '1':
        pytest.importorskip('faster_whisper')
    from faster_whisper.audio import decode_audio
    import numpy as np
    path = tone(tmp_path) if container == 'wav' else opus(tmp_path)
    decoded = decode_audio(str(path), sampling_rate=16000)
    assert decoded.ndim == 1 and decoded.dtype == np.float32
    assert abs(len(decoded)/16000-1.25) < 0.01
    assert np.isfinite(decoded).all() and float(np.max(np.abs(decoded))) > 0.05


def test_preinstalled_tiny_cpu_model_on_synthetic_speech(tmp_path, monkeypatch, record_property):
    model = os.environ.get('ENRICHMENT_TEST_MODEL_DIR')
    speech = os.environ.get('ENRICHMENT_TEST_SPEECH_FILE')
    if not model or not speech:
        pytest.skip('Optional: supply an already installed tiny model and synthetic speech WAV')
    assert Path(model).is_absolute() and Path(model).is_dir()
    assert Path(speech).is_file()
    # No hidden Hub fallback is possible even if a supplied model is incomplete.
    monkeypatch.setenv('HF_HUB_OFFLINE', '1')
    monkeypatch.setenv('HF_HUB_DISABLE_TELEMETRY', '1')
    monkeypatch.setenv('TOKENIZERS_PARALLELISM', 'false')
    from faster_whisper import transcribe
    monkeypatch.setattr(transcribe, 'download_model', lambda *a, **k: pytest.fail('No model downloads in inference'))
    cfg = validate_profile({'provider': 'faster_whisper', 'model': model,
                            'max_audio_seconds': 30}, 'audio')
    started = time.monotonic()
    result = infer(cfg, 'audio', Path(speech), 'audio/wav')
    elapsed = time.monotonic()-started
    record_property('inference_wall_seconds', round(elapsed, 3))
    record_property('audio_seconds', result['metadata']['duration_seconds'])
    assert result['text'].strip() and result['metadata']['segments']
    assert result['metadata']['transcoded'] is False and result['metadata']['audio_truncated'] is False
