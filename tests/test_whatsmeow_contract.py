"""Native-provider boundaries using synthetic files and subprocesses only.

No test in this module constructs a real WhatsApp client or opens a session.
The subprocess fixture exercises the production Python launch/IPC path; Go's
offline normalizer fixture, when supplied by the build job, is checked against
the actual archive validator rather than a second Python normalization model.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from messaging_service import whatsapp_collector as wa
from messaging_service.collector_state import session_lock, validate_capture
from test_whatsapp import capture, event


def _private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


@pytest.fixture
def native_config(tmp_path):
    permit = _private_json(tmp_path / 'permit.json', {
        'account': 'wa-native-test', 'expected_wid': '123456789@c.us',
        'prior_clients_stopped': True,
    })
    value = {'enabled': True, 'account': 'wa-native-test',
        'expected_wid': '123456789@c.us', 'permit_file': str(permit),
        'provider': 'whatsmeow', 'native_executable': '/synthetic/anymsg-whatsmeow',
        'session_directory': str(tmp_path / 'native-auth'),
        'events_directory': str(tmp_path / 'events'),
        'pages_directory': str(tmp_path / 'pages')}
    def save(changes=None):
        return _private_json(tmp_path / 'config.json', {**value, **(changes or {})})
    return value, save


def test_native_configuration_needs_no_browser_executable(native_config):
    _, save = native_config
    value = wa.configuration(save())
    assert value['provider'] == 'whatsmeow'
    assert 'node_executable' not in value and 'chromium_executable' not in value
    assert value['disk_reserve_bytes'] >= 1024**3
    assert value['media_disk_reserve_bytes'] >= value['disk_reserve_bytes']


@pytest.mark.parametrize('changes', [
    {'provider': 'auto'}, {'provider': 'baileys'}, {'provider': None},
    {'native_executable': 'anymsg-whatsmeow'}, {'native_executable': None},
])
def test_unknown_provider_and_relative_native_executable_fail_closed(native_config, changes):
    _, save = native_config
    with pytest.raises(ValueError):
        wa.configuration(save(changes))


def test_legacy_config_remains_explicit_browser_and_is_not_converted(native_config):
    cfg, save = native_config
    path = save({'node_executable': '/synthetic/node', 'chromium_executable': '/synthetic/chromium'})
    persisted = json.loads(path.read_text())
    del persisted['provider']
    _private_json(path, persisted)
    value = wa.configuration(path)
    assert value['provider'] == 'webjs'
    assert 'provider' not in json.loads(path.read_text())
    assert value['session_directory'] == cfg['session_directory']
    assert type(wa.provider_for_config(value, path, 3)) is wa.NodeProvider


def test_native_selector_never_resolves_browser_assets_or_cleans_profile_locks(native_config, monkeypatch):
    cfg, save = native_config
    monkeypatch.setattr(wa, 'whatsapp_asset', lambda *_: pytest.fail('browser asset resolved'))
    monkeypatch.setattr(wa, 'clear_browser_runtime_locks', lambda *_: pytest.fail('browser profile touched'))
    path = save()
    provider = wa.provider_for_config(wa.configuration(path), path, 3)
    assert isinstance(provider, wa.WhatsmeowProvider)
    assert provider.command() == [cfg['native_executable'], '--config', str(path)]
    provider.prepare_session()


@pytest.mark.parametrize('state', ['missing', 'public', 'symlink', 'directory'])
def test_native_session_requires_private_regular_auth_file_without_conversion(native_config, state):
    cfg, _ = native_config
    root = Path(cfg['session_directory'])
    root.mkdir(mode=0o700)
    auth = root / 'auth.sqlite'
    if state == 'public':
        auth.write_bytes(b'synthetic')
        auth.chmod(0o644)
    elif state == 'symlink':
        target = root / 'target'
        target.write_bytes(b'synthetic')
        target.chmod(0o600)
        auth.symlink_to(target)
    elif state == 'directory':
        auth.mkdir(mode=0o700)
    with pytest.raises(RuntimeError, match='whatsapp_pairing_required|private_session_required'):
        wa.require_paired_session(cfg)
    assert not (root / 'session').exists()


@pytest.mark.parametrize('provenance', ['whatsmeow_message_alt_v1', 'whatsmeow_lid_store_v1'])
def test_native_namespace_evidence_stays_source_bound_and_outside_revision(provenance):
    e = event(1, chat='900@lid')
    e.update(senderId='900@lid', identityObservations=[{
        'identity_id': '900@lid', 'pn': '123456789@c.us', 'lid': '900@lid',
        'provenance': provenance, 'observed_at': '2026-10-15T00:00:00Z'}])
    value = capture(e, account='wa-native-test')
    normalized = validate_capture(value)
    assert normalized['identity_observations'][0]['provenance'] == provenance
    assert 'identity_observations' not in normalized['envelope']
    assert normalized == validate_capture(normalized)
    value['event']['identityObservations'][0]['identity_id'] = '901@lid'
    with pytest.raises(ValueError, match='identity_source_mismatch'):
        validate_capture(value)


@pytest.fixture
def synthetic_native(tmp_path):
    """A non-network executable speaking the same inherited-FD protocol."""
    script = tmp_path / 'synthetic-native'
    script.write_text('#!' + sys.executable + '\n' + r'''
import base64,json,os,socket,sys,threading
if len(sys.argv)!=3 or sys.argv[1]!='--config':sys.exit(4)
release=threading.Event()
stop=threading.Event()
foreground=socket.socket(fileno=int(os.environ['ANYMSG_WHATSAPP_SEND_FD']))
def emit(out,value):
 out.write((json.dumps(value)+'\n').encode());out.flush()
def action():
 with foreground.makefile('rwb',buffering=0) as stream:
  for line in stream:
   request=json.loads(line)
   if request['method']=='send_text':
    release.set()
    emit(stream,{'id':request['id'],'result':{'provider_message_id':'synthetic-confirmed'}})
   else:emit(stream,{'id':request['id'],'error':'operation_unsupported'})
threading.Thread(target=action,daemon=True).start()
for line in sys.stdin.buffer:
 request=json.loads(line)
 if request['method']=='media':
  emit(sys.stdout.buffer,{'id':request['id'],'chunk':base64.b64encode(b'synthetic').decode()})
  release.wait(10)
  emit(sys.stdout.buffer,{'id':request['id'],'result':{'verified':True}})
 elif request['method']=='page':emit(sys.stdout.buffer,{'id':request['id'],'result':{'pending':True}})
 else:emit(sys.stdout.buffer,{'id':request['id'],'result':{'ready':True}})
''')
    script.chmod(0o700)
    return script


def test_native_launch_keeps_foreground_independent_of_blocked_media(tmp_path, synthetic_native, monkeypatch):
    monkeypatch.setattr(wa.NodeProvider, 'command', lambda *_: pytest.fail('browser fallback'))
    monkeypatch.setattr(wa, 'clear_browser_runtime_locks', lambda *_: pytest.fail('browser locks'))
    async def run():
        with session_lock(tmp_path / 'owned-session') as fd:
            provider = wa.WhatsmeowProvider({'provider': 'whatsmeow',
                'native_executable': str(synthetic_native)}, tmp_path / 'unused-config', fd)
            provider.policy = {}
            stream = None
            try:
                await provider.connect()
                stream = provider.chunks({'provider_chat': '123@c.us', 'provider_id': 'fixture'}, {})
                assert await anext(stream) == b'synthetic'
                assert provider.lock.locked()
                async with provider.send_gate:
                    result = await asyncio.wait_for(provider.send_text({
                        'provider_chat': '123@c.us', 'body': 'Synthetic test',
                        'expires_at': time.time() + 60}), 2)
                assert result == {'provider_message_id': 'synthetic-confirmed'}
                assert provider.lock.locked()
                assert [value async for value in stream] == []
                assert await provider.page('123@c.us', limit=1) == {'pending': True}
            finally:
                if stream is not None:
                    await stream.aclose()
                await provider.disconnect()
            assert provider.process.returncode is not None
            assert provider.send_writer is None
    asyncio.run(run())


def test_native_launch_failure_never_retries_as_browser(tmp_path, monkeypatch):
    monkeypatch.setattr(wa.NodeProvider, 'command', lambda *_: pytest.fail('browser fallback'))
    monkeypatch.setattr(wa, 'clear_browser_runtime_locks', lambda *_: pytest.fail('browser locks'))
    async def run():
        with session_lock(tmp_path / 'owned-session') as fd:
            provider = wa.WhatsmeowProvider({'provider': 'whatsmeow',
                'native_executable': str(tmp_path / 'missing-native')}, tmp_path / 'unused', fd)
            with pytest.raises(FileNotFoundError):
                await provider.connect()
            assert provider.process is None and provider.send_writer is None
    asyncio.run(run())


@pytest.fixture(scope='session')
def native_fixture_binary(tmp_path_factory):
    configured = os.environ.get('ANYMSG_WHATSMEOW_TEST_BINARY')
    if configured:
        result = Path(configured)
        assert result.is_absolute() and result.is_file(), 'invalid native fixture binary'
        return result
    go = shutil.which('go')
    assert go, 'Go is required for the actual native/Python contract; install it or supply ANYMSG_WHATSMEOW_TEST_BINARY'
    root = Path(__file__).resolve().parents[1]
    result = tmp_path_factory.mktemp('native-fixture-build') / 'anymsg-whatsmeow'
    build = subprocess.run([go, 'build', '-o', str(result), '.'],
        cwd=root / 'whatsapp' / 'whatsmeow', text=True, capture_output=True, timeout=120)
    assert build.returncode == 0, build.stderr
    return result


def _normalize_fixture(binary, tmp_path, changes=None):
    value = {'synthetic': True, 'account': 'wa-native-test', 'chat': '123@c.us',
        'sender': '123@c.us', 'owner': '999@c.us', 'id': 'fixture123',
        'timestamp': 1700000000, 'from_me': False, 'text': 'Привет 🌍', 'kind': 'chat',
        **(changes or {})}
    source = _private_json(tmp_path / 'fixture.json', value)
    answer = subprocess.run([str(binary), '--normalize-fixture', str(source)],
        text=True, capture_output=True, timeout=10)
    assert answer.returncode == 0, answer.stderr
    captured = json.loads(answer.stdout)
    return captured, validate_capture(captured)


@pytest.mark.parametrize('changes,kind', [
    ({}, 'text'),
    ({'from_me': True, 'sender': '999@c.us'}, 'text'),
    ({'kind': 'image', 'mime': 'image/jpeg', 'bytes': 1024}, 'photo'),
    ({'kind': 'document', 'filename': 'synthetic.txt', 'mime': 'text/plain', 'bytes': 512}, 'document'),
    ({'kind': 'ptt', 'text': '', 'mime': 'audio/ogg; codecs=opus', 'bytes': 2048, 'duration_seconds': 12}, 'voice'),
    ({'kind': 'notification', 'text': ''}, 'system'),
])
def test_actual_go_normalizer_is_accepted_by_python_spool(native_fixture_binary, tmp_path, changes, kind):
    from messaging_service.collector_state import CaptureSpool
    raw, normalized = _normalize_fixture(native_fixture_binary, tmp_path, changes)
    envelope = normalized['envelope']
    assert envelope['account'] == 'wa-native-test'
    assert envelope['chat'] == '123@c.us'
    assert envelope['metadata']['message_kind'] == kind
    assert envelope['metadata']['timestamp_resolution'] == 'second'
    assert envelope['direction'] == ('outgoing' if changes.get('from_me') else 'incoming')
    assert envelope['provider_id'] == f"{str(bool(changes.get('from_me'))).lower()}_123@c.us_fixture123"
    if kind in ('photo', 'document', 'voice'):
        assert envelope['media'][0]['bytes'] == changes['bytes']
    if kind == 'voice':
        assert envelope['metadata']['media_duration_seconds'] == 12
        assert envelope['media'][0]['mime'] == 'audio/ogg'
    if kind == 'system':
        assert envelope['metadata']['is_service'] is True
    spool = CaptureSpool(tmp_path / 'validated-spool', reserve_bytes=0)
    spool.put(raw)
    accepted = []
    spool.drain(accepted.append)
    assert accepted == [normalized]
    assert list(spool.path.glob('*.json')) == []


def test_actual_group_capture_preserves_lid_and_authenticated_owner_attention(native_fixture_binary, tmp_path):
    _, normalized = _normalize_fixture(native_fixture_binary, tmp_path, {
        'chat': '123-456@g.us', 'sender': '700@lid', 'sender_alt': '123456789@c.us',
        'owner_lid': '888@lid', 'mentions': ['888@lid'],
        'reply_to_id': 'quoted123', 'reply_participant': '888@lid'})
    envelope = normalized['envelope']
    assert envelope['sender'] == '700@lid'
    assert envelope['provider_id'] == 'false_123-456@g.us_fixture123_700@lid'
    assert envelope['metadata']['mentions_owner'] is True
    assert envelope['metadata']['reply_to_owner'] is True
    assert normalized['identity_observations'][0]['pn'] == '123456789@c.us'
    assert normalized['identity_observations'][0]['lid'] == '700@lid'
    assert normalized['identity_observations'][0]['provenance'] == 'whatsmeow_message_alt_v1'


def test_actual_native_capture_replay_does_not_invent_a_revision(native_fixture_binary, tmp_path):
    from messaging_service.envelope import fingerprint
    _, first = _normalize_fixture(native_fixture_binary, tmp_path)
    _, second = _normalize_fixture(native_fixture_binary, tmp_path)
    assert fingerprint(first['envelope']) == fingerprint(second['envelope'])


def test_actual_history_capture_uses_archive_backfill_origin(native_fixture_binary, tmp_path):
    _, normalized = _normalize_fixture(native_fixture_binary, tmp_path, {'origin': 'history'})
    assert normalized['envelope']['origin'] == 'backfill'


# spec: ANYM-471
def test_actual_go_album_photo_and_template_are_archived_with_content(native_fixture_binary, tmp_path, seeded):
    from messaging_service.archive import Archive
    store = seeded[0]
    archive = Archive(store)
    archive.register('wa-native-test', 'whatsapp', 'Synthetic native', include_chats=['123@c.us'])
    _, photo = _normalize_fixture(native_fixture_binary, tmp_path, {
        'kind': 'album_photo', 'text': 'site visit', 'mime': 'image/jpeg', 'bytes': 2048})
    envelope = photo['envelope']
    assert envelope['metadata']['message_kind'] == 'photo' and envelope['body'] == 'site visit'
    assert envelope['media'] == [{'provider_id': 'false_123@c.us_fixture123',
        'filename': 'false_123@c.us_fixture123.bin', 'mime': 'image/jpeg', 'bytes': 2048}]
    message = archive.ingest(envelope)['message']
    with store.pool.connection() as c:
        jobs = c.execute('SELECT id FROM media_jobs WHERE message=%s', (message,)).fetchall()
    assert len(jobs) == 1
    _, template = _normalize_fixture(native_fixture_binary, tmp_path, {
        'kind': 'template', 'text': 'Check-in at 14:00', 'id': 'fixture124'})
    envelope = template['envelope']
    assert envelope['metadata']['message_kind'] == 'text'
    assert envelope['metadata']['provider_message_type'] == 'template'
    assert envelope['body'] == 'Check-in at 14:00' and envelope['media'] == []
    assert archive.ingest(envelope)['outcome'] == 'created'


# spec: ANYM-471
def test_actual_go_document_after_sender_key_event_is_created_without_conflict(native_fixture_binary, tmp_path, seeded):
    from messaging_service.archive import Archive
    store = seeded[0]
    archive = Archive(store)
    archive.register('wa-native-test', 'whatsapp', 'Synthetic native', include_chats=['123@c.us'])
    source = _private_json(tmp_path / 'sender-key.json', {'synthetic': True, 'chat': '123@c.us',
        'sender': '123@c.us', 'owner': '999@c.us', 'id': 'fixture125', 'timestamp': 1700000000, 'kind': 'sender_key'})
    answer = subprocess.run([str(native_fixture_binary), '--normalize-fixture', str(source)],
        text=True, capture_output=True, timeout=10)
    assert answer.returncode == 0, answer.stderr
    assert json.loads(answer.stdout) is None
    _, document = _normalize_fixture(native_fixture_binary, tmp_path, {'kind': 'document', 'id': 'fixture125',
        'text': '', 'filename': 'report.pdf', 'mime': 'application/pdf', 'bytes': 4096})
    envelope = document['envelope']
    assert envelope['metadata']['message_kind'] == 'document'
    result = archive.ingest(envelope)
    assert result['outcome'] == 'created'
    assert archive.ingest(envelope)['outcome'] == 'duplicate'
    with store.pool.connection() as c:
        jobs = c.execute('SELECT id FROM media_jobs WHERE message=%s', (result['message'],)).fetchall()
    assert len(jobs) == 1


def test_actual_go_round_video_is_archived_as_video_with_one_media_job(native_fixture_binary, tmp_path, seeded):
    from messaging_service.archive import Archive
    store = seeded[0]
    archive = Archive(store)
    archive.register('wa-native-test', 'whatsapp', 'Synthetic native', include_chats=['123@c.us'])
    _, normalized = _normalize_fixture(native_fixture_binary, tmp_path, {
        'kind': 'ptv', 'text': '', 'mime': 'video/mp4', 'bytes': 4096, 'duration_seconds': 9})
    envelope = normalized['envelope']
    assert envelope['metadata']['message_kind'] == 'video'
    assert envelope['metadata']['provider_message_type'] == 'ptv'
    assert envelope['metadata']['media_duration_seconds'] == 9
    assert envelope['media'] == [{'provider_id': 'false_123@c.us_fixture123',
        'filename': 'false_123@c.us_fixture123.bin', 'mime': 'video/mp4', 'bytes': 4096}]
    message = archive.ingest(envelope)['message']
    with store.pool.connection() as c:
        jobs = c.execute('SELECT id FROM media_jobs WHERE message=%s', (message,)).fetchall()
    assert len(jobs) == 1
