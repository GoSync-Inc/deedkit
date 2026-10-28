"""Recovery fixtures contain no provider session and never connect to providers."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tarfile

import pytest

from messaging_service.archive import Archive
from messaging_service.backup import create_snapshot,verify_snapshot
from messaging_service.backup_crypto import encrypt_snapshot,decrypt_snapshot
from messaging_service.collector_state import session_lock
from messaging_service.recovery_state import materialize_recovery,verify_recovery,snapshot_sources

DUMP=[sys.executable,'-c','print("UNIT_DUMP_NOT_A_DATABASE")']


def recovery_fixture(store,path):
    Archive(store).register('recovery-fixture','telegram','Synthetic session fixture',synthetic=False,include_chats=['1'])
    session=path/'session';session.mkdir(mode=0o700);(session/'example.session').write_bytes(b'FAKE_SESSION_BYTES')
    with session_lock(session/'example.session'):pass
    spool=path/'spool';spool.mkdir();(spool/'pending.json').write_text('{"test":"pending"}')
    (spool/'empty').mkdir()
    settings=path/'settings.json';settings.write_text('{"secret":"FAKE","enabled":true}')
    return {'accounts':[{'account':'recovery-fixture','session_path':str(session/'example.session')}],
        'sources':[{'name':'telegram-session','kind':'session','path':str(session),'account':'recovery-fixture'},
                   {'name':'telegram-spool','kind':'spool','path':str(spool),'account':'recovery-fixture'},
                   {'name':'settings','kind':'settings','path':str(settings),'account':None}]}


def test_full_recovery_roundtrip_quarantines_without_source_paths(seeded,tmp_path):
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path)
    create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    assert verify_snapshot(s,tmp_path/'snapshot')['tables']==82
    text=(tmp_path/'snapshot/manifest.json').read_text();manifest=json.loads(text)
    assert manifest['format']==2 and manifest['includes_provider_sessions'] is True
    assert str(tmp_path) not in text and 'FAKE_SESSION_BYTES' not in text
    result=materialize_recovery(tmp_path/'snapshot',tmp_path/'quarantine')
    assert result=={'status':'quarantined','sources':3,'activated':False}
    restored=tmp_path/'quarantine/telegram-session/root/example.session'
    assert restored.read_bytes()==b'FAKE_SESSION_BYTES' and restored.stat().st_mode&0o777==0o600
    assert (tmp_path/'quarantine/telegram-spool/root/empty').is_dir()
    assert (tmp_path/'quarantine/settings/root').read_text()=='{"secret":"FAKE","enabled":true}'
    # No original path is ever replaced, and existing quarantine is immutable.
    (tmp_path/'session/example.session').write_bytes(b'NEW_SESSION')
    with pytest.raises(ValueError,match='destination_exists'):
        materialize_recovery(tmp_path/'snapshot',tmp_path/'quarantine')
    assert (tmp_path/'session/example.session').read_bytes()==b'NEW_SESSION'


def test_recovery_requires_all_accounts_and_spools(seeded,tmp_path):
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path)
    Archive(s).register('another-account','whatsapp','Another fixture',synthetic=False,include_chats=['1@c.us'])
    with pytest.raises(ValueError,match='account_coverage'):create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    cfg['sources']=[x for x in cfg['sources'] if x['kind']!='spool']
    with pytest.raises(ValueError,match='spool_required'):create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    assert not (tmp_path/'snapshot').exists() and not list(tmp_path.glob('.backup-*'))


def test_recovery_rejects_active_session_and_lease(seeded,tmp_path):
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path)
    with session_lock(cfg['accounts'][0]['session_path']):
        with pytest.raises(RuntimeError,match='session_already_owned'):create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    archive=Archive(s);epoch=archive.acquire('recovery-fixture','test-backup-worker',60)
    with pytest.raises(RuntimeError,match='collector_active'):create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    archive.release('recovery-fixture','test-backup-worker',epoch)
    create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)


@pytest.mark.parametrize('attack',['symlink','hardlink','fifo','overlap'])
def test_recovery_rejects_unsafe_source_and_cleans_partial(seeded,tmp_path,attack):
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path)
    if attack=='symlink':(tmp_path/'spool/outside').symlink_to(tmp_path/'settings.json')
    elif attack=='hardlink':os.link(tmp_path/'settings.json',tmp_path/'spool/shared')
    elif attack=='fifo':os.mkfifo(tmp_path/'spool/pipe')
    else:cfg['sources'][1]['path']=str(tmp_path/'session')
    with pytest.raises(ValueError,match='special_file|source_overlap'):
        create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    assert not (tmp_path/'snapshot').exists() and not list(tmp_path.glob('.backup-*'))


@pytest.mark.parametrize('attack',['corruption','missing','extra','traversal','duplicate','file_parent'])
def test_recovery_manifest_and_files_fail_closed(seeded,tmp_path,attack):
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path);target=tmp_path/'snapshot'
    create_snapshot(s,target,DUMP,cfg);manifest=json.loads((target/'manifest.json').read_text())
    entry=next(e for e in manifest['recovery']['entries'] if e['type']=='file')
    blob=target/'recovery'/entry['sha256']
    if attack=='corruption':blob.write_bytes(b'x'*entry['bytes'])
    elif attack=='missing':blob.unlink()
    elif attack=='extra':(target/'recovery'/('0'*64)).write_bytes(b'unreferenced')
    elif attack=='traversal':entry['path']='root/../../escape'
    elif attack=='duplicate':manifest['recovery']['entries'].append(copy.deepcopy(entry))
    else:
        child=copy.deepcopy(entry);child['path']=entry['path']+'/bad';manifest['recovery']['entries'].append(child)
        manifest['recovery']['logical_bytes']+=entry['bytes']
    (target/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises((ValueError,RuntimeError)):materialize_recovery(target,tmp_path/'quarantine')
    assert not (tmp_path/'quarantine').exists() and not (tmp_path/'escape').exists()


def test_recovery_byte_and_file_limits(tmp_path):
    source=tmp_path/'source';source.mkdir();(source/'large').write_bytes(b'abc')
    sources={'test':{'name':'test','kind':'settings','path':source,'account':None}}
    with pytest.raises(ValueError,match='size_limit'):snapshot_sources(sources,tmp_path/'bounded',max_bytes=2)
    with pytest.raises(ValueError,match='file_limit'):snapshot_sources(sources,tmp_path/'count',max_files=1)


def test_recovery_detects_mid_copy_mutation(monkeypatch,tmp_path):
    source=tmp_path/'source';source.write_bytes(b'abc')
    sources={'test':{'name':'test','kind':'settings','path':source,'account':None}}
    original=os.read;mutated=False
    def changing_read(fd,size):
        nonlocal mutated
        chunk=original(fd,size)
        if not mutated:
            mutated=True;source.write_bytes(b'replaced with different bytes')
        return chunk
    monkeypatch.setattr(os,'read',changing_read)
    with pytest.raises(ValueError,match='source_changed'):snapshot_sources(sources,tmp_path/'bounded')


def test_recovery_rechecks_account_coverage_in_dump_snapshot(seeded,tmp_path,monkeypatch):
    import messaging_service.recovery_state as state
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path);original=state.snapshot_sources
    def changed_accounts(*args,**kwargs):
        result=original(*args,**kwargs)
        Archive(s).register('new-account','whatsapp','Fixture registered during backup',synthetic=False,include_chats=['1@c.us'])
        return result
    monkeypatch.setattr(state,'snapshot_sources',changed_accounts)
    with pytest.raises(ValueError,match='account_coverage'):create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    assert not (tmp_path/'snapshot').exists() and not list(tmp_path.glob('.backup-*'))


def test_cold_snapshot_releases_session_lock_before_blob_copy(seeded,tmp_path):
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path);calls=[]
    def captured():
        # The provider could safely resume at this point. Its new state must not
        # change the cold bytes already copied into this snapshot.
        with session_lock(cfg['accounts'][0]['session_path']):
            (tmp_path/'session/example.session').write_bytes(b'NEW_PROVIDER_STATE')
        calls.append('captured')
    create_snapshot(s,tmp_path/'snapshot',DUMP,cfg,captured_callback=captured)
    assert calls==['captured']
    materialize_recovery(tmp_path/'snapshot',tmp_path/'quarantine')
    assert (tmp_path/'quarantine/telegram-session/root/example.session').read_bytes()==b'FAKE_SESSION_BYTES'


def test_cold_snapshot_never_creates_root_owned_session_lock(seeded,tmp_path):
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path)
    lock=Path(cfg['accounts'][0]['session_path']+'.owner.lock');lock.unlink()
    with pytest.raises(FileNotFoundError):create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    assert not lock.exists() and not (tmp_path/'snapshot').exists()


@pytest.mark.parametrize('attack',['extra_blob','dump'])
def test_bundle_verification_rejects_untracked_or_corrupt_data(seeded,tmp_path,attack):
    from messaging_service.backup import verify_bundle
    s,*_=seeded;create_snapshot(s,tmp_path/'snapshot',DUMP)
    if attack=='extra_blob':(tmp_path/'snapshot/files'/('f'*64)).write_bytes(b'unreferenced')
    else:(tmp_path/'snapshot/database.dump').write_bytes(b'corrupt')
    with pytest.raises((ValueError,RuntimeError)):verify_bundle(tmp_path/'snapshot')


def test_encrypted_bundle_shape_checks_with_transport_stub(seeded,tmp_path):
    # No cryptography assertion here: actual pinned age is tested by deployment drill.
    age=tmp_path/'age-stub'
    age.write_text('#!'+sys.executable+'\nimport sys,shutil\n'
        'source=open(sys.argv[-1],"rb") if "-d" in sys.argv else sys.stdin.buffer\n'
        'shutil.copyfileobj(source,sys.stdout.buffer)\n')
    age.chmod(0o700)
    s,*_=seeded;cfg=recovery_fixture(s,tmp_path);target=tmp_path/'snapshot'
    create_snapshot(s,target,DUMP,cfg)
    encrypt_snapshot(target,tmp_path/'bundle','fake',age=str(age))
    decrypt_snapshot(tmp_path/'bundle','fake',tmp_path/'restored',age=str(age))
    assert verify_snapshot(s,tmp_path/'restored')['tables']==82
    # Valid tar framing cannot smuggle a path or quietly omit a recovery blob.
    for attack in ['path','duplicate','omit']:
        with tarfile.open(tmp_path/'bundle','r:') as src,tarfile.open(tmp_path/attack,'w:') as out:
            for member in src:
                if attack=='omit' and member.name.startswith('recovery/'):continue
                out.addfile(member,src.extractfile(member))
            if attack!='omit':
                member=tarfile.TarInfo('../escape' if attack=='path' else 'manifest.json');member.size=2
                out.addfile(member,io.BytesIO(b'{}'))
        with pytest.raises((ValueError,RuntimeError)):
            decrypt_snapshot(tmp_path/attack,'fake',tmp_path/(attack+'-restored'),age=str(age))
        assert not (tmp_path/(attack+'-restored')).exists()


def test_chromium_session_snapshot_omits_only_ephemeral_singleton_symlinks(tmp_path):
    source=tmp_path/'session-root';profile=source/'whatsapp/session'
    (profile/'Default').mkdir(parents=True)
    (profile/'Local State').write_bytes(b'BROWSER_STATE_FIXTURE')
    (profile/'Default/auth.db').write_bytes(b'AUTH_FIXTURE')
    for name in ['SingletonLock','SingletonSocket','SingletonCookie']:
        (profile/name).symlink_to('/nonexistent/runtime-only')
    sources={'wa-session':{'name':'wa-session','kind':'session','path':source,'account':'wa'}}
    result=snapshot_sources(sources,tmp_path/'backup')
    assert not any('Singleton' in row['path'] for row in result['entries'])
    assert (tmp_path/'backup'/hashlib.sha256(b'AUTH_FIXTURE').hexdigest()).read_bytes()==b'AUTH_FIXTURE'
    assert (profile/'SingletonLock').is_symlink()  # Read-only capture never mutates original state.
    (profile/'Default/not-a-runtime-link').symlink_to('/etc/passwd')
    with pytest.raises(ValueError,match='recovery_special_file'):
        snapshot_sources(sources,tmp_path/'rejected')


@pytest.mark.parametrize('kind,profile_shape',[('settings',True),('spool',True),('session',False)])
def test_singleton_named_links_outside_session_profiles_still_rejected(tmp_path,kind,profile_shape):
    source=tmp_path/'source';source.mkdir()
    if profile_shape:
        (source/'Default').mkdir();(source/'Local State').write_text('{}')
    (source/'SingletonLock').symlink_to('/etc/passwd')
    sources={'test':{'name':'test','kind':kind,'path':source,'account':'wa' if kind!='settings' else None}}
    with pytest.raises(ValueError,match='recovery_special_file'):
        snapshot_sources(sources,tmp_path/'backup')
