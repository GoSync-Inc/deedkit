"""The whatsmeow fork pin must equal the recorded upstream plus fork.patch (ANYM-441); synthetic trees, no network."""
import json
from pathlib import Path
import re
import subprocess

import pytest

from scripts import check_whatsmeow_fork as fork_check

ROOT = Path(__file__).resolve().parents[1]
FORK = 'github.com/GoSync-Inc/whatsmeow'
FORK_VERSION = 'v0.0.0-20990101000000-0123456789ab'
UPSTREAM = {'request.go':'package whatsmeow\n\nfunc a() {}\n\nfunc retryFrame() {\n\treturn\n}\n','send.go':'package whatsmeow\n\nfunc send() {}\n','binary/node.go':'package binary\n'}
FORKED = {'request.go':'package whatsmeow\n\nfunc a() {}\n\nfunc retryFrame() {\n\tif marked() {\n\t\treturn\n\t}\n\treturn\n}\n','deedkit_fork_test.go':'package whatsmeow\n\nfunc TestFork() {}'}


def git_diff(tmp_path,before,after):
    repo = tmp_path/'repo'
    def write(files):
        for name in list(before)+list(after):(repo/name).unlink(missing_ok=True)
        for name,text in files.items():
            (repo/name).parent.mkdir(parents=True,exist_ok=True)
            (repo/name).write_text(text)
    git = lambda *args:subprocess.run(['git','-C',str(repo),*args],check=True,capture_output=True,text=True).stdout
    repo.mkdir()
    git('init','-q')
    write(before)
    git('add','-A')
    git('-c','user.name=t','-c','user.email=t@example.invalid','commit','-qm','upstream')
    write(after)
    git('add','-A')
    return git('diff','--cached','--no-color','--no-renames','--no-ext-diff')


def trees(tmp_path,patched=FORKED):
    upstream = {k:v.encode() for k,v in UPSTREAM.items()}
    fork = dict(upstream)|{k:v.encode() for k,v in patched.items()}
    patch = git_diff(tmp_path,UPSTREAM,{k:v.decode() for k,v in fork.items()})
    return upstream,fork,patch


# spec: ANYM-441
def test_fork_equal_to_upstream_plus_patch_passes(tmp_path):
    upstream,fork,patch = trees(tmp_path)
    assert '\\ No newline at end of file' in patch
    fork_check.compare(upstream,fork,patch)


# spec: ANYM-441
@pytest.mark.parametrize('case,error',[
    ('extra file','adds files'),
    ('removed file','removes files'),
    ('changed line outside the patched files','differs'),
    ('changed line inside a patched file','differs'),
])
def test_fork_differing_from_patch_fails(tmp_path,case,error):
    upstream,fork,patch = trees(tmp_path)
    if case=='extra file':fork['init.go'] = b'package whatsmeow\nfunc init() {}\n'
    if case=='removed file':del fork['binary/node.go']
    if case=='changed line outside the patched files':fork['send.go'] = fork['send.go'].replace(b'send()',b'send() { leak() }')
    if case=='changed line inside a patched file':fork['request.go'] = fork['request.go'].replace(b'func a() {}',b'func a() { leak() }')
    with pytest.raises(fork_check.ForkError,match=error):fork_check.compare(upstream,fork,patch)


# spec: ANYM-441
def test_patch_touching_other_files_fails(tmp_path):
    upstream,fork,patch = trees(tmp_path,FORKED|{'send.go':'package whatsmeow\n\nfunc send() { leak() }\n'})
    with pytest.raises(fork_check.ForkError,match=r"touches \['send.go'\]"):fork_check.compare(upstream,fork,patch)


# spec: ANYM-441
def test_patch_that_does_not_apply_fails(tmp_path):
    upstream,fork,patch = trees(tmp_path)
    upstream['request.go'] = upstream['request.go'].replace(b'func a() {}',b'func b() {}')
    with pytest.raises(fork_check.ForkError,match='does not apply'):fork_check.compare(upstream,fork,patch)


# spec: ANYM-441
def test_reviewed_pin_and_patch_are_consistent():
    gomod,gosum = (ROOT/'whatsapp/whatsmeow/go.mod').read_text(),(ROOT/'whatsapp/whatsmeow/go.sum').read_text()
    version,path,fork_version,fork_sum = fork_check.pins(gomod,gosum)
    assert (version,path) == (fork_check.UPSTREAM_VERSION,FORK)
    assert re.fullmatch(r'v0\.0\.0-\d{14}-[0-9a-f]{12}',fork_version) and fork_sum.startswith('h1:')
    changes = fork_check.parse_patch((ROOT/'whatsapp/whatsmeow/fork.patch').read_text())
    assert set(changes) == fork_check.PATCHABLE and changes['deedkit_fork_test.go'][0] and not changes['request.go'][0]


def adapter(tmp_path,patch,fork_sum='h1:fork='):
    root = tmp_path/'adapter'
    root.mkdir()
    (root/'go.mod').write_text(f'module x\n\nrequire (\n\tgo.mau.fi/whatsmeow {fork_check.UPSTREAM_VERSION}\n)\n\nreplace go.mau.fi/whatsmeow => {FORK} {FORK_VERSION}\n')
    (root/'go.sum').write_text(f'{FORK} {FORK_VERSION} {fork_sum}\n{FORK} {FORK_VERSION}/go.mod h1:mod=\n')
    (root/'fork.patch').write_text(patch)
    return root


def materialize(base,files):
    for name,data in files.items():
        (base/name).parent.mkdir(parents=True,exist_ok=True)
        (base/name).write_bytes(data)
    return base


# spec: ANYM-441
@pytest.mark.parametrize('case,error',[
    ('reviewed pin',None),
    ('upstream hash differs from the recorded one','upstream hash'),
    ('fork hash differs from go.sum','fork hash'),
    ('extracted tree differs from its authenticated hash','extracted tree hash'),
])
def test_main_checks_hashes_and_only_downloads(tmp_path,monkeypatch,case,error):
    upstream,fork,patch = trees(tmp_path)
    upstream_key,fork_key = f'go.mau.fi/whatsmeow@{fork_check.UPSTREAM_VERSION}',f'{FORK}@{FORK_VERSION}'
    sums = {upstream_key:fork_check.h1('go.mau.fi/whatsmeow',fork_check.UPSTREAM_VERSION,upstream),fork_key:fork_check.h1(FORK,FORK_VERSION,fork)}
    dirs = {upstream_key:materialize(tmp_path/'up',upstream),fork_key:materialize(tmp_path/'fork',fork)}
    if case!='upstream hash differs from the recorded one':monkeypatch.setattr(fork_check,'UPSTREAM_SUM',sums[upstream_key])
    root = adapter(tmp_path,patch,'h1:other=' if case=='fork hash differs from go.sum' else sums[fork_key])
    if case=='extracted tree differs from its authenticated hash':(dirs[fork_key]/'send.go').write_text('package whatsmeow\n\nfunc send() { leak() }\n')
    commands = []
    def run(command,**kwargs):
        commands.append((command,kwargs['env']['GOSUMDB'],kwargs['env']['GOFLAGS']))
        return subprocess.CompletedProcess(command,0,json.dumps({'Sum':sums[command[-1]],'Dir':str(dirs[command[-1]])}),'')
    monkeypatch.setattr(fork_check,'adapter',root)
    monkeypatch.setattr(fork_check.subprocess,'run',run)
    if error:
        with pytest.raises(fork_check.ForkError,match=error):fork_check.main()
    else:fork_check.main()
    # Only `go mod download` with the checksum database on: nothing from the candidate is built or run.
    assert commands and all(c[:4]==['go','mod','download','-json'] and db=='sum.golang.org' and flags=='' for c,db,flags in commands)


# spec: ANYM-441
def test_main_rejects_unrecorded_upstream_bump(tmp_path,monkeypatch):
    root = adapter(tmp_path,'')
    (root/'go.mod').write_text((root/'go.mod').read_text().replace(fork_check.UPSTREAM_VERSION,'v0.0.0-20991231000000-ffffffffffff'))
    monkeypatch.setattr(fork_check,'adapter',root)
    monkeypatch.setattr(fork_check.subprocess,'run',lambda *_,**__:pytest.fail('downloaded before the pin check'))
    with pytest.raises(fork_check.ForkError,match='recorded upstream pin'):fork_check.main()
