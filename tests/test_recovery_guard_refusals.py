"""ANYM-122: the recovery config guard refuses each unsafe operator config before a snapshot exists.

Synthetic fixtures only: no provider session, no provider connection. Each case runs the real
create_snapshot against the disposable test database and checks the exact refusal code, that no
snapshot or temporary directory remains (also inside a source the destination overlapped), that the
source bytes are untouched and that no backup fence stays active.
"""
import os
from pathlib import Path
import sys

import pytest

from messaging_service.archive import Archive
from messaging_service.backup import create_snapshot
from messaging_service.collector_state import session_lock
from messaging_service.file_lifecycle import backup_fence_active

DUMP=[sys.executable,'-c','print("UNIT_DUMP_NOT_A_DATABASE")']


def fixture(store,path):
    Archive(store).register('guard-fixture','telegram','Synthetic guard fixture',synthetic=False,include_chats=['1'])
    session=path/'session';session.mkdir(mode=0o700);(session/'example.session').write_bytes(b'FAKE_SESSION_BYTES')
    with session_lock(session/'example.session'):pass
    spool=path/'spool';spool.mkdir();(spool/'pending.json').write_text('{"test":"pending"}')
    settings=path/'settings.json';settings.write_text('{"enabled":true}')
    return {'accounts':[{'account':'guard-fixture','session_path':str(session/'example.session')}],
        'sources':[{'name':'telegram-session','kind':'session','path':str(session),'account':'guard-fixture'},
                   {'name':'telegram-spool','kind':'spool','path':str(spool),'account':'guard-fixture'},
                   {'name':'settings','kind':'settings','path':str(settings),'account':None}]}


def tree(path):
    return sorted((str(p.relative_to(path)),p.read_bytes() if p.is_file() else None) for p in path.rglob('*'))


def refused(store,tmp_path,cfg,code,destination=None):
    before=tree(tmp_path);destination=destination or tmp_path/'snapshot'
    with pytest.raises(ValueError) as error:create_snapshot(store,destination,DUMP,cfg)
    assert str(error.value)==code
    assert not destination.exists() and not list(tmp_path.rglob('.backup-*'))
    assert tree(tmp_path)==before
    with store.pool.connection() as c:assert not backup_fence_active(c)


# spec: ANYM-122
@pytest.mark.parametrize('change',['command','endpoint','accounts_not_a_list'])
def test_a_config_that_names_a_command_or_an_endpoint_is_refused_before_any_snapshot(seeded,tmp_path,change):
    s,*_=seeded;cfg=fixture(s,tmp_path)
    if change=='command':cfg['command']=['sh','-c','true'];code='invalid_recovery_config'
    elif change=='endpoint':cfg['sources'][1]['endpoint']='https://example.invalid/upload';code='invalid_recovery_source'
    else:cfg['accounts']={'guard-fixture':cfg['accounts'][0]['session_path']};code='invalid_recovery_config'
    refused(s,tmp_path,cfg,code)


# spec: ANYM-122
@pytest.mark.parametrize('change',['settings_with_account','spool_of_unknown_account','unknown_kind','duplicate_account'])
def test_a_source_or_account_outside_the_kind_rules_is_refused(seeded,tmp_path,change):
    s,*_=seeded;cfg=fixture(s,tmp_path);code='invalid_recovery_source'
    if change=='settings_with_account':cfg['sources'][2]['account']='guard-fixture'
    elif change=='spool_of_unknown_account':cfg['sources'][1]['account']='not-configured'
    elif change=='unknown_kind':cfg['sources'][1]['kind']='database'
    else:cfg['accounts'].append(dict(cfg['accounts'][0]));code='invalid_recovery_account'
    refused(s,tmp_path,cfg,code)


# spec: ANYM-122
def test_a_config_without_a_settings_source_is_refused(seeded,tmp_path):
    s,*_=seeded;cfg=fixture(s,tmp_path)
    cfg['sources']=[x for x in cfg['sources'] if x['kind']!='settings']
    refused(s,tmp_path,cfg,'recovery_settings_required')


# spec: ANYM-122
@pytest.mark.parametrize('change',['root_elsewhere','two_roots','no_root'])
def test_each_account_needs_exactly_one_session_root_covering_its_session(seeded,tmp_path,change):
    s,*_=seeded;cfg=fixture(s,tmp_path)
    other=tmp_path/'other-session';other.mkdir(mode=0o700);(other/'unrelated.session').write_bytes(b'OTHER')
    if change=='root_elsewhere':cfg['sources'][0]['path']=str(other)
    elif change=='two_roots':cfg['sources'].append({'name':'second-session','kind':'session','path':str(other),'account':'guard-fixture'})
    else:cfg['sources']=[x for x in cfg['sources'] if x['kind']!='session']
    refused(s,tmp_path,cfg,'recovery_session_scope')


# spec: ANYM-122
def test_a_destination_inside_a_source_is_refused_and_leaves_the_source_as_it_was(seeded,tmp_path):
    # A destination must not exist yet and a source must, so the only reachable overlap is a destination
    # inside a source; create_snapshot's temporary directory then sits inside that source too.
    s,*_=seeded;cfg=fixture(s,tmp_path)
    refused(s,tmp_path,cfg,'recovery_destination_overlap',tmp_path/'spool'/'snapshot')


# spec: ANYM-122
@pytest.mark.parametrize('change',['relative','dotdot','symlinked_ancestor','missing'])
def test_a_source_path_must_be_absolute_existing_and_free_of_symlinks_in_every_ancestor(seeded,tmp_path,change):
    s,*_=seeded;cfg=fixture(s,tmp_path)
    if change=='relative':cfg['sources'][1]['path']=os.path.relpath(tmp_path/'spool');code='recovery_absolute_path_required'
    elif change=='dotdot':cfg['sources'][1]['path']=str(tmp_path/'session'/'..'/'spool');code='recovery_absolute_path_required'
    elif change=='symlinked_ancestor':
        (tmp_path/'real').mkdir();(tmp_path/'real/spool').mkdir();(tmp_path/'link').symlink_to(tmp_path/'real')
        cfg['sources'][1]['path']=str(tmp_path/'link'/'spool');code='recovery_symlink'
    else:cfg['sources'][1]['path']=str(tmp_path/'absent-spool');code='recovery_source_missing'
    refused(s,tmp_path,cfg,code)


# spec: ANYM-122
def test_the_same_config_without_the_refused_change_creates_its_snapshot(seeded,tmp_path):
    s,*_=seeded;cfg=fixture(s,tmp_path)
    result=create_snapshot(s,tmp_path/'snapshot',DUMP,cfg)
    assert result['status']=='snapshot_created' and Path(tmp_path/'snapshot/manifest.json').exists()
