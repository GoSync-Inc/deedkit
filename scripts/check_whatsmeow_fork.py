"""Fail closed unless the whatsmeow fork pin equals the recorded upstream commit plus fork.patch.

`go mod download -json` fetches both module versions through the Go proxy, checks them against
the checksum database and extracts them; it never compiles or runs module code. The fork tree must
equal the upstream tree with fork.patch applied exactly (no fuzz), file by file (ANYM-441).
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
adapter = root/'whatsapp/whatsmeow'
UPSTREAM = 'go.mau.fi/whatsmeow'
# The reviewed upstream pin; a bump changes these two values in the same reviewed PR.
UPSTREAM_VERSION = 'v0.0.0-20261014094007-f16f9ca30d6a'
UPSTREAM_SUM = 'h1:33FqqrtRrtYYieuzwp9tufGiPJJjrqES+DUvOHbQWZE='
PATCHABLE = {'request.go','deedkit_fork_test.go'}


class ForkError(Exception):
    pass


def pins(gomod,gosum):
    """Return (upstream version, fork path, fork version, fork go.sum hash) from go.mod and go.sum."""
    require = re.findall(rf'^\s*(?:require\s+)?{re.escape(UPSTREAM)} (v\S+)$',gomod,re.M)
    # Every replacement of the module, versioned or not, in a block or not, with or without a comment.
    replace = re.findall(rf'^\s*(?:replace\s+)?{re.escape(UPSTREAM)}(\s+v\S+)?\s*=>\s*(\S+)(\s+v\S+)?\s*(?://.*)?$',gomod,re.M)
    if len(require)!=1 or len(replace)!=1 or replace[0][0] or not replace[0][2]:
        raise ForkError('go.mod needs one require and exactly one unversioned module replace of '+UPSTREAM)
    path,version = replace[0][1],replace[0][2].strip()
    sums = re.findall(rf'^{re.escape(path)} {re.escape(version)} (h1:\S+)$',gosum,re.M)
    if len(sums)!=1:raise ForkError(f'go.sum has no single hash for {path}@{version}')
    return require[0],path,version,sums[0]


def split(text):
    """Lines with their endings, split on '\\n' only, as git counts them."""
    return re.findall(r'[^\n]*\n|[^\n]+$',text)


def parse_patch(text):
    """Return {path: (new_file, [(old_start, old_lines, new_lines)])} for a plain `git diff`."""
    files,current,path,lines,i = {},None,'',split(text),0
    while i<len(lines):
        line = lines[i]
        if line.startswith('diff --git '):
            names = re.fullmatch(r'diff --git a/(\S+) b/(\S+)\n',line)
            if not names or names[1]!=names[2] or names[1] in files:raise ForkError(f'unsupported diff header: {line!r}')
            path = names[1]
            current = files[path] = [False,[]]
        elif current is None:raise ForkError(f'text before the first diff: {line!r}')
        elif line=='new file mode 100644\n':current[0] = True
        elif re.fullmatch(r'index [0-9a-f]+\.\.[0-9a-f]+( 100644)?\n',line):pass
        elif line.startswith(('--- ','+++ ')):
            want = ('--- /dev/null\n' if current[0] else f'--- a/{path}\n') if line[0]=='-' else f'+++ b/{path}\n'
            if line!=want:raise ForkError(f'unexpected file line: {line!r}')
        elif line.startswith('@@ '):
            head = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@',line)
            if not head:raise ForkError(f'bad hunk header: {line!r}')
            old_count,new_count = int(head[2] or 1),int(head[4] or 1)
            old,new = [],[]
            while len(old)<old_count or len(new)<new_count or i+1<len(lines) and lines[i+1].startswith('\\'):
                i += 1
                if i>=len(lines):raise ForkError('truncated hunk')
                body = lines[i]
                if body.startswith('\\'):
                    # "No newline at end of file" applies to the side(s) of the line before it.
                    if lines[i-1][0] in ' -':old[-1] = old[-1].removesuffix('\n')
                    if lines[i-1][0] in ' +':new[-1] = new[-1].removesuffix('\n')
                    continue
                if body[:1] not in (' ','-','+'):raise ForkError(f'bad hunk line: {body!r}')
                if body[0]!='+':old.append(body[1:])
                if body[0]!='-':new.append(body[1:])
            if len(old)!=old_count or len(new)!=new_count:raise ForkError(f'hunk size mismatch: {line!r}')
            current[1].append((int(head[1]),old,new))
        else:raise ForkError(f'unsupported patch line: {line!r}')
        i += 1
    return {name:(new,hunks) for name,(new,hunks) in files.items()}


def apply(source,hunks):
    """Apply hunks to source exactly: every context and removed line must match at its stated line."""
    lines,out,at = split(source),[],0
    for start,old,new in hunks:
        begin = start-1 if old else start
        if begin<at or lines[begin:begin+len(old)]!=old:raise ForkError(f'hunk at line {start} does not apply')
        out += lines[at:begin]+new
        at = begin+len(old)
    return ''.join(out+lines[at:])


def tree(path):
    return {p.relative_to(path).as_posix():p.read_bytes() for p in sorted(Path(path).rglob('*')) if p.is_file()}


def h1(module,version,files):
    """Go's dirhash.Hash1 of an extracted module tree, so the compared bytes are the authenticated ones."""
    lines = [f'{hashlib.sha256(files[name]).hexdigest()}  {module}@{version}/{name}\n' for name in sorted(files)]
    return 'h1:'+base64.b64encode(hashlib.sha256(''.join(lines).encode()).digest()).decode()


def compare(upstream,fork,patch):
    """Raise ForkError unless fork == upstream with patch applied; trees are {relative path: bytes}."""
    changes = parse_patch(patch)
    if not set(changes)<=PATCHABLE:raise ForkError(f'fork.patch touches {sorted(set(changes)-PATCHABLE)}')
    expected = dict(upstream)
    for path,(new,hunks) in changes.items():
        if new==(path in upstream):raise ForkError(f'{path}: new-file flag disagrees with upstream')
        source = '' if new else upstream[path].decode()
        expected[path] = apply(source,hunks).encode()
    if extra := sorted(set(fork)-set(expected)):raise ForkError(f'fork adds files outside fork.patch: {extra}')
    if missing := sorted(set(expected)-set(fork)):raise ForkError(f'fork removes files: {missing}')
    if changed := sorted(p for p in expected if expected[p]!=fork[p]):raise ForkError(f'fork differs from upstream + fork.patch: {changed}')


def download(module,version,workdir):
    # Checksum database on for both modules whatever the caller's Go environment says.
    env = dict(os.environ,GOFLAGS='',GOWORK='off',GOSUMDB='sum.golang.org',GONOSUMDB='',GOPRIVATE='',GONOPROXY='',GOINSECURE='')
    run = subprocess.run(['go','mod','download','-json',f'{module}@{version}'],cwd=workdir,env=env,capture_output=True,text=True)
    if run.returncode:raise ForkError(f'go mod download {module}@{version} failed: {run.stdout}{run.stderr}')
    info = json.loads(run.stdout)
    if info.get('Error'):raise ForkError(f'go mod download {module}@{version}: {info["Error"]}')
    files = tree(info['Dir'])
    if (found := h1(module,version,files))!=info['Sum']:raise ForkError(f'{module}@{version}: extracted tree hash {found} differs from {info["Sum"]}')
    return info['Sum'],files


def effective():
    """Ask Go which module the adapter build selects (go.mod, go.work and GOFLAGS as the build sees them)."""
    run = subprocess.run(['go','list','-m','-json',UPSTREAM],cwd=adapter,env=dict(os.environ),capture_output=True,text=True)
    if run.returncode:raise ForkError(f'go list -m {UPSTREAM} failed: {run.stdout}{run.stderr}')
    info = json.loads(run.stdout)
    return info.get('Version'),(info.get('Replace') or {}).get('Path'),(info.get('Replace') or {}).get('Version')


def main():
    version,fork_path,fork_version,fork_sum = pins((adapter/'go.mod').read_text(),(adapter/'go.sum').read_text())
    if version!=UPSTREAM_VERSION:raise ForkError(f'go.mod requires {version}; the recorded upstream pin is {UPSTREAM_VERSION}')
    # A vendor tree would be built instead of the module cache.
    if (adapter/'vendor').exists():raise ForkError('whatsapp/whatsmeow/vendor would bypass the checked module')
    if (found := effective())!=(version,fork_path,fork_version):raise ForkError(f'Go selects {found}, not the checked {(version,fork_path,fork_version)}')
    with tempfile.TemporaryDirectory() as workdir:
        upstream_sum,upstream = download(UPSTREAM,version,workdir)
        if upstream_sum!=UPSTREAM_SUM:raise ForkError(f'upstream hash {upstream_sum} differs from the recorded {UPSTREAM_SUM}')
        downloaded_sum,fork = download(fork_path,fork_version,workdir)
        if downloaded_sum!=fork_sum:raise ForkError(f'fork hash {downloaded_sum} differs from go.sum {fork_sum}')
        compare(upstream,fork,(adapter/'fork.patch').read_text())
    print(f'whatsmeow fork {fork_path}@{fork_version} = {UPSTREAM}@{version} + fork.patch')


if __name__=='__main__':
    try:main()
    except ForkError as error:sys.exit(f'whatsmeow fork check failed: {error}')
