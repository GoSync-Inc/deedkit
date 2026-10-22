"""Explicit root-run Docker drill: synthetic PG18 dump, real age and new helper.

CI builds the reviewed backup image and invokes only this file with sudo and the
2 explicit image variables. Ordinary pytest excludes this file. No provider/cloud
credentials or production mounts enter any container.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess

import pytest

from messaging_service import backup_r2_receiver as r2
from messaging_service import backup_r2_verifier as verifier

IMAGE=os.environ.get('DEEDKIT_TEST_BACKUP_IMAGE')
REVISION=os.environ.get('DEEDKIT_TEST_BACKUP_REVISION')
pytestmark=pytest.mark.skipif(not IMAGE or not REVISION or os.geteuid()!=0,
                            reason='explicit root-run immutable-image Docker drill only')

FIXTURE_SCRIPT=r'''
import hashlib,json,os,subprocess
from pathlib import Path
import psycopg
from psycopg.conninfo import make_conninfo
from messaging_service.store import Store
from messaging_service.backup import create_snapshot
from messaging_service.backup_crypto import encrypt_snapshot
root=Path('/fixture')
recipient=os.environ['DEEDKIT_SYNTHETIC_RECIPIENT']
password=os.environ['POSTGRES_PASSWORD']
base=dict(host='restore-postgres',user='restore_owner',password=password,port='5432')
with psycopg.connect(make_conninfo(**base,dbname='deedkit_verify_restore_test'),autocommit=True) as c:
    c.execute('CREATE DATABASE deedkit_fixture')
dsn=make_conninfo(**base,dbname='deedkit_fixture')
store=Store(dsn,root/'originals','http://synthetic.invalid')
try:
    store.migrate();store.seed()
    content=(b'SYNTHETIC_STREAMING_ORIGINAL_ONLY\n'*(6*1024**2//32+1))[:6*1024**2]
    digest=hashlib.sha256(content).hexdigest();(store.files/digest).write_bytes(content)
    with store.pool.connection() as c:
        c.execute('INSERT INTO account_policies(account,enabled,send_enabled) SELECT id,true,true FROM accounts ON CONFLICT(account) DO UPDATE SET enabled=true,send_enabled=true')
        c.execute("INSERT INTO attachments(id,message,digest,filename,mime,bytes) VALUES('native-six-mib','demo-tg-2',%s,'synthetic.bin','application/octet-stream',%s)",(digest,len(content)))
    store.issue_key('native-synthetic-agent',['telegram-demo'])
    os.environ['PGPASSWORD']=password
    public=make_conninfo(host='restore-postgres',user='restore_owner',port='5432',dbname='deedkit_fixture')
    create_snapshot(store,root/'snapshot',['pg_dump','--dbname='+public])
    recovery=b'{\"synthetic_recovery_setting\":true}\n';recovery_sha=hashlib.sha256(recovery).hexdigest()
    (root/'snapshot/recovery').mkdir(mode=0o700);(root/'snapshot/recovery'/recovery_sha).write_bytes(recovery)
    manifest=json.loads((root/'snapshot/manifest.json').read_text());assert len(manifest['blobs'])>=2
    manifest['format']=2;manifest['recovery']={'sources':[{'name':'synthetic-settings','kind':'settings','account':None}],
        'entries':[{'source':'synthetic-settings','path':'root','type':'directory'},
                   {'source':'synthetic-settings','path':'root/settings.json','type':'file','bytes':len(recovery),'sha256':recovery_sha}],
        'logical_bytes':len(recovery),'quiesced':True,'restore_policy':'quarantine_only_no_automatic_sessions_or_permits'}
    (root/'snapshot/manifest.json').write_text(json.dumps(manifest))
    encrypt_snapshot(root/'snapshot',root/'ciphertext',recipient)
    (root/'manifest-sha256').write_text(hashlib.sha256((root/'snapshot/manifest.json').read_bytes()).hexdigest())
finally:store.close()
'''


def test_real_pg18_age_stream_restore_runs_all_seven_checks_in_isolated_owned_containers():
    # /tmp is intentionally forbidden by production's trusted-root ancestry.
    # Create a unique root-private test leaf under real /root; never weaken checks.
    parent=Path('/root/deedkit-backup-native-tests');parent.mkdir(mode=0o700,exist_ok=True)
    with verifier.local.trusted_directory(parent) as fd:
        assert os.fstat(fd).st_mode&0o077==0
    root=parent/secrets.token_hex(16);root.mkdir(mode=0o700)
    source_run=secrets.token_hex(16);restore_run=secrets.token_hex(16)
    engine=verifier.Docker(900);fixture_name=verifier.names(source_run)['helper']+'-fixture'
    config={'helper_image':IMAGE,'helper_revision':REVISION,'postgres_image':verifier.POSTGRES_IMAGE,
            'identity_file':str(root/'fixture/identity')}
    fixture=root/'fixture';fixture.mkdir(mode=0o700)
    secrets_dir=root/'source-secrets';secrets_dir.mkdir(mode=0o700)
    payload=root/'source-payload';payload.mkdir(mode=0o700)
    password=secrets.token_hex(32)
    verifier.write_secret(secrets_dir/'postgres.env',('POSTGRES_USER=restore_owner\nPOSTGRES_DB='+verifier.DATABASE+'\nPOSTGRES_PASSWORD='+password+'\n').encode())
    verifier.write_secret(secrets_dir/'worker.json',b'{}\n')
    try:
        subprocess.run(['age-keygen','-o',str(fixture/'identity')],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True)
        recipient=subprocess.check_output(['age-keygen','-y',str(fixture/'identity')],stderr=subprocess.DEVNULL).decode().strip()
        engine.preflight(config)
        engine.setup(config,source_run,payload,secrets_dir)
        labels=['--label',verifier.LABEL+'='+source_run,'--label',verifier.LABEL+'.owned=1']
        engine.command('container','create','--name',fixture_name,*labels,'--network',verifier.names(source_run)['network'],
                       '--memory=512m','--memory-swap=512m','--cpus=1','--pids-limit=256','--log-driver=none',
                       '--tmpfs',verifier.PG_DATA_PATH+':'+verifier.HELPER_TMPFS[verifier.PG_DATA_PATH],
                       '--env-file',str(secrets_dir/'postgres.env'),'--env','DEEDKIT_SYNTHETIC_RECIPIENT='+recipient,'--mount','type=bind,src='+str(fixture)+',dst=/fixture',
                       '--entrypoint','python',IMAGE,'-c',FIXTURE_SCRIPT)
        engine.command('container','start','-a',fixture_name)
        result=r2.unique_json(engine.command('container','inspect',fixture_name))[0]
        assert result['State']['ExitCode']==0,'synthetic fixture generation failed'
        assert not any(m['Type']=='volume' for m in result['Mounts'])
        assert result['HostConfig']['Tmpfs'][verifier.PG_DATA_PATH]==verifier.HELPER_TMPFS[verifier.PG_DATA_PATH]
        engine.inspect_owned('helper',fixture_name,source_run);engine.remove('helper',fixture_name)
        verifier.cleanup_resources(engine,source_run)
        cipher=fixture/'ciphertext';snapshot=(fixture/'manifest-sha256').read_text()
        with cipher.open('rb') as source:digest=hashlib.file_digest(source,'sha256').hexdigest()
        receipt={'wire':{'id':'20261016T091703Z-fedcba9876543210','bytes':cipher.stat().st_size,
                         'sha256':digest,'snapshot_sha256':snapshot},'object_key':'synthetic-ciphertext'}
        class Cloud:
            def get_object(self,**kwargs):return {'ContentLength':cipher.stat().st_size,'Body':cipher.open('rb')}
        target_secrets=root/'restore-secrets';target_secrets.mkdir(mode=0o700)
        target_payload=root/'restore-payload';target_payload.mkdir(mode=0o700)
        verifier.write_secret(target_secrets/'postgres.env',(secrets_dir/'postgres.env').read_bytes())
        verifier.write_secret(target_secrets/'worker.json',r2.encoded({'v':1,'identity_file':'/run/restore/identity',
            'work_directory':'/work','database_host':'restore-postgres','database_password':password,
            'snapshot_sha256':snapshot,'max_plaintext_bytes':receipt['wire']['bytes']}))
        facts=engine.restore(config,{'bucket':'synthetic'},Cloud(),receipt,restore_run,target_payload,target_secrets)
        proof=verifier.make_proof(receipt,facts,restore_run)
        assert proof['verification']['schema_version']==42
        assert proof['verification']['checks']=={name:True for name in r2.CHECKS}
        helper=engine.inspect_owned('helper',verifier.names(restore_run)['helper'],restore_run)
        network=engine.inspect_owned('network',verifier.names(restore_run)['network'],restore_run)
        pg=engine.inspect_owned('postgres',verifier.names(restore_run)['postgres'],restore_run)
        assert network['Internal'] is True
        assert not helper['HostConfig']['PortBindings'] and not pg['HostConfig']['PortBindings']
        assert not any(m['Type']=='volume' for m in helper['Mounts'])
        assert helper['HostConfig']['Tmpfs']==verifier.HELPER_TMPFS
        mounts={m['Destination'] for m in helper['Mounts']}
        assert mounts=={'/work','/run/restore/identity','/run/restore/config.json'}
        assert helper['HostConfig']['Memory']==512*1024**2 and pg['HostConfig']['Memory']==1024**3
    finally:
        # Exact journal-derived resource names and labels; no prune or unrelated IDs.
        if engine.inspect_owned('helper',fixture_name,source_run) is not None:engine.remove('helper',fixture_name)
        verifier.cleanup_resources(engine,source_run);verifier.cleanup_resources(engine,restore_run)
        shutil.rmtree(root)
    # Every labelled explicit PG volume was removed. Inherited helper/fixture
    # paths were bounded tmpfs, so no anonymous data volume can outlive them.
    for run in (source_run,restore_run):
        assert all(engine.inspect_owned(kind,name,run) is None for kind,name in verifier.names(run).items())


def test_exact_owned_container_cleanup_removes_legacy_anonymous_volume_preserves_named_sentinel():
    """Actual rm --volumes semantics, including pre-fix interrupted containers."""
    run=secrets.token_hex(16);engine=verifier.Docker(120)
    name=verifier.names(run)['helper'];sentinel=verifier.names(run)['volume']+'-sentinel'
    labels=['--label',verifier.LABEL+'='+run,'--label',verifier.LABEL+'.owned=1']
    anonymous=None
    try:
        engine.command('volume','create',*labels,sentinel)
        engine.command('container','create','--name',name,*labels,'--network=none',
                       '--mount','type=volume,src='+sentinel+',dst=/sentinel','--entrypoint','true',verifier.POSTGRES_IMAGE)
        value=engine.inspect_owned('helper',name,run)
        mounts=[m for m in value['Mounts'] if m['Type']=='volume' and m['Destination']==verifier.PG_DATA_PATH]
        assert len(mounts)==1 and mounts[0]['Destination']==verifier.PG_DATA_PATH
        anonymous=mounts[0]['Name']
        engine.remove('helper',name)
        with pytest.raises(RuntimeError,match='restore_docker_failed'):engine.command('volume','inspect',anonymous)
        assert engine.inspect_owned('volume',sentinel,run) is not None
    finally:
        if engine.inspect_owned('helper',name,run) is not None:engine.remove('helper',name)
        if engine.inspect_owned('volume',sentinel,run) is not None:engine.remove('volume',sentinel)
        if anonymous and engine.command('volume','ls','--filter','name=^'+anonymous+'$','--format','{{.Name}}').strip():
            engine.command('volume','rm',anonymous)
