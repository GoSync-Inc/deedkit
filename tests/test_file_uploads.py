"""Private uploaded originals; fixtures only, never real messenger sessions."""
import asyncio
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import psycopg
import pytest
from starlette.testclient import TestClient

from messaging_service.app import create_app
from messaging_service.commands import CommandWorker
from messaging_service.envelope import stable_id
from messaging_service.store import APIError
from messaging_service.uploads import CHUNK_BYTES, MAX_BYTES, RESERVE_BYTES
from messaging_service.storage_admission import STREAM_WORKSPACE_BYTES
from test_messenger_commands import operator as operator


@pytest.fixture
def uploader(operator):
    s,archive,_,state=operator
    archive.set_sending('command-fixture',True)
    key=s.issue_key('upload-agent',['command-fixture'],permissions=['read','send','upload'])
    yield s,key,state


def metadata(content=b'Synthetic file',**changes):
    return dict(account='command-fixture',chat_id=stable_id('chat','command-fixture','100'),
        filename='fixture.txt',mime='text/plain',bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),request_id='synthetic-upload-0001',**changes)


def prepare(s,key,content=b'Synthetic file',**changes):
    data=metadata(content);data.update(changes)
    return s.prepare_file_upload(key,**data)


def chunk(s,key,row,content,offset=0):
    return s.upload_file_chunk(key,row['upload_id'],offset,base64.b64encode(content).decode())


def send_args(row,**changes):
    values=dict(account=row['account'],operation='send_file',chat_id=row['chat_id'],
        parameters={'upload_id':row['upload_id'],'sha256':row['sha256'],'mode':'document',
                    'caption':'Synthetic caption'})
    return {**values,**changes}


def test_chunked_upload_is_exact_idempotent_and_never_creates_archive_messages(uploader):
    s,key,_=uploader
    content=b'a'*CHUNK_BYTES+b'end'
    row=prepare(s,key,content)
    assert row['chunk_bytes']==CHUNK_BYTES and row['status']=='uploading'
    assert prepare(s,key,content)['upload_id']==row['upload_id']
    first=chunk(s,key,row,content[:CHUNK_BYTES])
    assert first['offset']==CHUNK_BYTES
    assert chunk(s,key,row,content[:CHUNK_BYTES])==first
    ready=chunk(s,key,row,content[CHUNK_BYTES:],CHUNK_BYTES)
    assert ready['status']=='ready' and ready['offset']==len(content)
    assert chunk(s,key,row,content[CHUNK_BYTES:],CHUNK_BYTES)==ready
    assert (s.files/'.uploads'/row['upload_id']/row['sha256']).read_bytes()==content
    with s.pool.connection() as c:
        assert c.execute('SELECT count(*) AS n FROM messages WHERE account=%s',('command-fixture',)).fetchone()['n']==2
        assert c.execute('SELECT count(*) AS n FROM messenger_commands').fetchone()['n']==0
    with pytest.raises(APIError,match='idempotency_conflict'):
        prepare(s,key,content,filename='changed.txt')


@pytest.mark.parametrize('value',['not-base64','YQ==\n','YR==',''])
def test_chunk_rejects_noncanonical_base64_without_growing_file(uploader,value):
    s,key,_=uploader;row=prepare(s,key)
    with pytest.raises(APIError,match='invalid_upload_chunk'):
        s.upload_file_chunk(key,row['upload_id'],0,value)
    assert s.get_file_upload(key,row['upload_id'])['offset']==0


def test_upload_boundaries_and_last_chunk_hash_rollback(uploader):
    s,key,_=uploader;row=prepare(s,key,b'right')
    with pytest.raises(APIError,match='upload_offset_conflict'):chunk(s,key,row,b'x',1)
    with pytest.raises(APIError,match='invalid_upload_chunk'):chunk(s,key,row,b'x'*(CHUNK_BYTES+1))
    with pytest.raises(APIError,match='upload_hash_mismatch'):chunk(s,key,row,b'wrong')
    assert s.get_file_upload(key,row['upload_id'])['offset']==0
    assert chunk(s,key,row,b'right')['status']=='ready'
    with pytest.raises(APIError,match='upload_offset_conflict'):chunk(s,key,row,b'wrong')


@pytest.mark.parametrize('field,value',[
    ('filename','../file.txt'),('filename','unsafe\nname'),('bytes',MAX_BYTES+1),
    ('bytes',True),('bytes',0),('mime','text/plain; charset=utf8'),('sha256','bad')])
def test_prepare_rejects_unsafe_metadata(uploader,field,value):
    s,key,_=uploader
    with pytest.raises(APIError,match='invalid_upload'):prepare(s,key,**{field:value})


def test_upload_is_credential_and_destination_bound_and_explicit_permission(uploader):
    s,key,_=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    other=s.issue_key('upload-agent',['command-fixture'],permissions=['read','send','upload'])
    for access in [lambda:s.get_file_upload(other,row['upload_id']),
                   lambda:s.preview_messenger_operation(other,**send_args(row)),
                   lambda:s.preview_messenger_operation(key,**send_args(row,
                       chat_id=stable_id('chat','command-fixture','200')))]:
        with pytest.raises(APIError,match='not_found'):access()
    legacy=s.issue_key('legacy-upload',['command-fixture'],permissions=['read','send','download'])
    with pytest.raises(APIError,match='forbidden'):prepare(s,legacy)
    assert 'prepare_file_upload' not in s.capabilities(legacy)['available']
    with pytest.raises(APIError,match='invalid_operation_parameters'):
        s.preview_messenger_operation(key,**send_args(row,parameters={
            'upload_id':row['upload_id'],'attachment_id':'att_fixture','sha256':row['sha256']}))


def test_preview_digest_binds_file_metadata_and_worker_rechecks_upload_permission(uploader):
    s,key,state=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    args=send_args(row)
    draft=s.preview_messenger_operation(key,**args)
    assert draft['required_permissions']==['read','send','upload']
    assert 'registered_file_path' not in json.dumps(draft)
    queued=s.execute_messenger_operation(key,**args,preview_digest=draft['preview_digest'],
        request_id='synthetic-upload-send')
    s.set_key_permissions(s.capabilities(key)['credential_id'],['read','send'])
    observed=[]
    async def provider(job):observed.append(job);return {'outcome':'confirmed'}
    assert asyncio.run(CommandWorker(state,provider).once())
    assert observed==[]
    with s.pool.connection() as c:
        assert c.execute('SELECT status FROM messenger_commands WHERE id=%s',(queued['operation_id'],)).fetchone()['status']=='cancelled'


def test_finalized_upload_dispatches_verified_private_path_with_no_download_grant(uploader):
    s,key,state=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    args=send_args(row);draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,preview_digest=draft['preview_digest'],
        request_id='synthetic-upload-send')
    observed=[]
    async def provider(job):observed.append(job);return {'outcome':'confirmed','provider_message_id':'42'}
    assert asyncio.run(CommandWorker(state,provider).once())
    assert len(observed)==1 and observed[0]['file_upload_id']==row['upload_id']
    assert observed[0]['registered_file_path']==str(s.files/'.uploads'/row['upload_id']/row['sha256'])
    assert s.get_messenger_operation(key,queued['operation_id'])['status']=='succeeded'


def test_cleanup_is_namespace_bounded_and_keeps_queued_sources(uploader):
    s,key,_=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    args=send_args(row);draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,preview_digest=draft['preview_digest'],
        request_id='synthetic-upload-send')
    original=s.files/row['sha256'];original.write_bytes(b'Archive source must stay')
    with s.pool.connection() as c:c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second'")
    s.cleanup_file_uploads()
    path=s.files/'.uploads'/row['upload_id']/row['sha256']
    assert path.exists()
    with s.pool.connection() as c:c.execute("UPDATE messenger_commands SET status='cancelled' WHERE id=%s",(queued['operation_id'],))
    s.cleanup_file_uploads()
    assert not path.exists() and original.exists()
    assert s.get_file_upload(key,row['upload_id'])['status']=='expired'


def test_cleanup_reaps_own_crashed_publication_temporary_only(uploader):
    s,key,_=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    root=s.files/'.uploads'/row['upload_id']
    (root/('.pending-'+'a'*32)).write_bytes(b'Interrupted copy')
    with s.pool.connection() as c:c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second'")
    s.cleanup_file_uploads()
    assert not root.exists()


def test_expired_pinned_upload_is_retained_only_for_original_command(uploader):
    s,key,state=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    args=send_args(row);draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,preview_digest=draft['preview_digest'],
        request_id='synthetic-upload-send')
    with s.pool.connection() as c:c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second'")
    with pytest.raises(APIError,match='attachment_unavailable'):s.preview_messenger_operation(key,**args)
    s.cleanup_file_uploads()
    observed=[]
    async def provider(job):observed.append(job);return {'outcome':'confirmed'}
    assert asyncio.run(CommandWorker(state,provider).once()) and len(observed)==1
    assert s.get_messenger_operation(key,queued['operation_id'])['status']=='succeeded'


def test_source_validation_fences_cleanup_until_enqueue_commit(uploader,monkeypatch):
    from messaging_service import uploads
    s,key,_=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    args=send_args(row);draft=s.preview_messenger_operation(key,**args)
    validated=Event();release=Event();original=uploads.source
    def paused_source(*values,**options):
        source=original(*values,**options)
        validated.set()
        assert release.wait(5),'Synthetic enqueue barrier timed out'
        return source
    monkeypatch.setattr(uploads,'source',paused_source)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(s.execute_messenger_operation,key,**args,
            preview_digest=draft['preview_digest'],request_id='synthetic-enqueue-gc-race')
        try:
            assert validated.wait(5),'Source was not validated'
            with s.pool.connection() as c:
                c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second' WHERE id=%s",
                          (row['upload_id'],))
            # A second real PostgreSQL transaction must not reap a validated
            # source before the command that retains it is committed.
            with pytest.raises(psycopg.errors.LockNotAvailable):
                with s.pool.connection() as c:
                    c.execute("SET LOCAL lock_timeout='100ms'")
                    uploads.cleanup(s,c)
        finally:release.set()
        queued=future.result(timeout=5)
    s.cleanup_file_uploads()
    assert queued['status']=='queued'
    assert (s.files/'.uploads'/row['upload_id']/row['sha256']).is_file()


@pytest.mark.parametrize('condition',[
    'queued_expired','dispatching_expired','lease_expired','lease_replaced','lease_missing'])
def test_expired_or_unfenced_commands_do_not_pin_upload_quota(uploader,condition):
    s,key,state=uploader;rows=[]
    for index in range(4):
        row=prepare(s,key,request_id=f'synthetic-stale-pin-{index}')
        chunk(s,key,row,b'Synthetic file');rows.append(row)
        args=send_args(row);draft=s.preview_messenger_operation(key,**args)
        s.execute_messenger_operation(key,**args,preview_digest=draft['preview_digest'],
            request_id=f'synthetic-stale-command-{index}')
    with s.pool.connection() as c:
        c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second'")
        if condition!='queued_expired':
            c.execute("UPDATE messenger_commands SET status='dispatching',lease_epoch=%s",(state.lease[1],))
        if condition.endswith('_expired') and not condition.startswith('lease_'):
            c.execute("UPDATE messenger_commands SET expires_at=clock_timestamp()-interval '1 second'")
        elif condition=='lease_expired':
            c.execute("UPDATE collector_leases SET expires_at=clock_timestamp()-interval '1 second'")
        elif condition=='lease_replaced':c.execute('UPDATE collector_leases SET epoch=epoch+1')
        elif condition=='lease_missing':c.execute('DELETE FROM collector_leases')
    s.cleanup_file_uploads()
    for row in rows:assert not (s.files/'.uploads'/row['upload_id']).exists()
    assert prepare(s,key,request_id='synthetic-reclaimed-pin')['status']=='uploading'
    with s.pool.connection() as c:
        assert c.execute("SELECT count(*) AS n FROM file_uploads WHERE state='expired'").fetchone()['n']==4


def test_live_dispatching_pin_uses_lease_not_transient_connection_health(uploader):
    s,key,state=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    args=send_args(row);draft=s.preview_messenger_operation(key,**args)
    s.execute_messenger_operation(key,**args,preview_digest=draft['preview_digest'],
        request_id='synthetic-live-dispatch')
    job=CommandWorker(state,None).claim()
    assert job and not job.get('cancelled')
    with s.pool.connection() as c:
        c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second'")
    state.phase('connection','retry_wait','provider_disconnected')
    s.cleanup_file_uploads()
    assert (s.files/'.uploads'/row['upload_id']/row['sha256']).is_file()


def test_admission_checks_and_reserves_actual_upload_volume(uploader,monkeypatch):
    from messaging_service import uploads
    s,key,_=uploader;root=s.files/'.uploads';observed=[]
    def selected_volume(path):
        observed.append(path)
        return 'synthetic-upload-fs' if path==root else 'synthetic-archive-fs'
    def available(path):
        observed.append(path)
        return SimpleNamespace(free=0 if path==root else 100*1024**3)
    monkeypatch.setattr(uploads,'volume_key',selected_volume)
    monkeypatch.setattr(uploads.shutil,'disk_usage',available)
    with pytest.raises(APIError,match='upload_capacity'):prepare(s,key)
    assert observed and all(path==root for path in observed)
    reservation=2*len(b'Synthetic file')+STREAM_WORKSPACE_BYTES
    monkeypatch.setattr(uploads.shutil,'disk_usage',lambda _:SimpleNamespace(free=RESERVE_BYTES+reservation))
    first=prepare(s,key)
    with s.pool.connection() as c:
        assert c.execute('SELECT volume_key FROM file_uploads WHERE id=%s',
            (first['upload_id'],)).fetchone()['volume_key']=='synthetic-upload-fs'
        # A previous volume's durable reservation still counts towards job
        # slots, but must not consume capacity on this filesystem.
        c.execute("UPDATE file_uploads SET volume_key='synthetic-other-fs' WHERE id=%s",(first['upload_id'],))
    assert prepare(s,key,request_id='synthetic-other-volume')['status']=='uploading'
    with pytest.raises(APIError,match='upload_capacity'):
        prepare(s,key,request_id='synthetic-same-volume-full')


def test_api_gateway_database_role_can_upload_without_mutating_archive(uploader):
    from contextlib import contextmanager
    from copy import copy
    from pathlib import Path
    from uuid import uuid4
    import psycopg
    from psycopg import sql
    s,key,_=uploader;role='synthetic_upload_'+uuid4().hex
    ddl=Path(__file__).resolve().parents[1].joinpath(
        'services/messaging/messaging_service/gateway_role.sql').read_text().replace('messaging_gateway',role)
    class RestrictedPool:
        @contextmanager
        def connection(self):
            with s.pool.connection() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                yield c
    restricted=copy(s);restricted.pool=RestrictedPool()
    try:
        with s.pool.connection() as c:c.execute(ddl)
        row=prepare(restricted,key)
        assert chunk(restricted,key,row,b'Synthetic file')['status']=='ready'
        with restricted.pool.connection() as c,pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE messages SET body='Not allowed'")
    finally:
        with s.pool.connection() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_quota_is_bounded_and_expiry_reclaims_it(uploader):
    s,key,_=uploader
    for index in range(4):prepare(s,key,request_id=f'synthetic-quota-{index}')
    with pytest.raises(APIError,match='upload_limit'):prepare(s,key,request_id='synthetic-quota-next')
    with s.pool.connection() as c:c.execute("UPDATE file_uploads SET expires_at=clock_timestamp()-interval '1 second'")
    assert prepare(s,key,request_id='synthetic-quota-next')['status']=='uploading'


def test_upload_rejects_symlink_and_changed_content_at_dispatch(uploader,tmp_path):
    s,key,state=uploader;row=prepare(s,key);chunk(s,key,row,b'Synthetic file')
    args=send_args(row);draft=s.preview_messenger_operation(key,**args)
    queued=s.execute_messenger_operation(key,**args,preview_digest=draft['preview_digest'],request_id='synthetic-upload-send')
    path=s.files/'.uploads'/row['upload_id']/row['sha256'];path.unlink()
    outside=tmp_path/'outside';outside.write_bytes(b'Synthetic file');path.symlink_to(outside)
    observed=[]
    async def provider(job):observed.append(job);return {'outcome':'confirmed'}
    assert asyncio.run(CommandWorker(state,provider).once()) and observed==[]
    assert s.get_messenger_operation(key,queued['operation_id'])['status']=='cancelled'


class UploadRecordingStore:
    public_url='http://testserver'
    def __init__(self):self.calls=[]
    def authenticate(self,token):return 'synthetic-upload-principal'
    def permits(self,token,operation):return operation=='upload_file_chunk'
    def audit_boundary(self,code):pass
    def upload_file_chunk(self,token,**params):self.calls.append(params);return {'status':'uploading'}


def test_http_large_envelopes_allowed_only_for_upload_chunk():
    store=UploadRecordingStore();params={'upload_id':'up_'+'a'*32,'offset':0,
        'data_base64':base64.b64encode(b'x'*CHUNK_BYTES).decode()}
    headers={'Authorization':'Bearer synthetic','Accept':'application/json, text/event-stream'}
    with TestClient(create_app(store,['testserver'])) as client:
        assert client.post('/v1/upload_file_chunk',headers=headers,json=params).status_code==200
        rpc={'jsonrpc':'2.0','id':1,'method':'tools/call',
            'params':{'name':'upload_file_chunk','arguments':params}}
        assert not client.post('/mcp',headers=headers,json=rpc).json()['result']['isError']
        assert client.post('/v1/preview_messenger_operation',headers=headers,json=params).status_code==413
        rpc['params']['name']='preview_messenger_operation'
        assert client.post('/mcp',headers=headers,json=rpc).status_code==413
        rpc['method']='initialize'
        assert client.post('/mcp',headers=headers,json=rpc).status_code==413
    assert len(store.calls)==2
