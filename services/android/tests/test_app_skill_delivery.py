"""Versioned app skills reach the persistent native thread only through 0.166 skills/extraRoots/set."""

import hashlib
import json
import shutil

from agent_harness import Service
from test_codex_session import calls, live_host

from deedkit_android import app_skill_bundle as bundle

GOJEK = 'com.gojek.app'
NAMES = sorted(bundle.SKILLS)
EXTRA_ROOTS = '''    elif method == 'skills/extraRoots/set':
        found = sorted(os.path.relpath(os.path.join(d, n), r) for r in params['extraRoots']
                       for d, _, files in os.walk(r) for n in files if n == 'SKILL.md')
        with open({catalog!r}, 'a') as f:
            f.write(json.dumps(found) + '\\n')
        emit(id=message['id'], result={{}})
        emit(method='skills/changed', params={{}})
'''


def native(tmp_path):
    """The ANYM-993 synthetic app-server plus the native extra-roots request and a discovery record."""
    config, record = live_host(tmp_path)
    binary, marker = tmp_path / 'codex', "    elif method == 'turn/interrupt':"
    text = binary.read_text()
    assert marker in text
    binary.write_text(text.replace(marker, EXTRA_ROOTS.format(catalog=str(tmp_path / 'catalog.jsonl')) + marker))
    return config, record, tmp_path / 'catalog.jsonl'


def methods(record):
    return [r['message']['method'] for r in calls(record) if 'method' in r['message']]


def copied(tmp_path):
    root = tmp_path / 'bundle' / 'v1'
    shutil.copytree(bundle.ROOT, root)
    return root


def offer(tmp_path, root, apps=(GOJEK,)):
    home, cwd = tmp_path / 'codex-home', tmp_path / 'sessions' / 'run-live-x'
    (home / 'skills').mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)
    return bundle.offer(list(apps), str(home), cwd, root=root)


# spec: ANYM-993
def test_relevant_goal_offers_exact_bundle_through_native_extra_roots_without_changing_request(tmp_path):
    config, record, catalog = native(tmp_path)
    with Service(config) as service:
        session = service.lease()
        first = service.finished(service.start(session, apps=[GOJEK, 'example.food'])['request_id'])
        second = service.finished(service.start(session, apps=[GOJEK])['request_id'])
        third = service.finished(service.start(session)['request_id'])
    seen = calls(record)
    assert methods(record) == ['initialize', 'initialized', 'thread/start', 'model/list', 'skills/extraRoots/set', 'turn/start', 'model/list', 'turn/start', 'model/list',
                               'skills/extraRoots/set', 'turn/start']
    roots = [r['message']['params']['extraRoots'] for r in seen if r['message'].get('method') == 'skills/extraRoots/set']
    assert roots == [[str(bundle.ROOT.resolve())], []]
    assert [json.loads(line) for line in catalog.read_text().splitlines()] == [
        [name + '/SKILL.md' for name in NAMES], []]
    argv = seen[0]['argv']
    assert 'skills.bundled.enabled=false' in argv and 'features.skip_host_skill_discovery=true' in argv
    start = next(r['message']['params'] for r in seen if r['message'].get('method') == 'thread/start')
    assert set(start) == {'cwd', 'model', 'sandbox', 'approvalPolicy', 'ephemeral'}
    for turn in (r['message']['params'] for r in seen if r['message'].get('method') == 'turn/start'):
        assert [item['type'] for item in turn['input']] == ['text']
        assert 'GoFood' not in turn['input'][0]['text'] and 'SKILL' not in turn['input'][0]['text']
    receipt = {'version': bundle.VERSION, 'source': bundle.SOURCE, 'skills': NAMES, 'digest': bundle.DIGEST,
               'apps': [GOJEK], 'status': 'offered'}
    assert first['app_skills'] == second['app_skills'] == receipt
    assert 'app_skills' not in third and third['summary'] == 'Remembered turn 3'


# spec: ANYM-993
def test_empty_and_unrelated_apps_send_no_skill_request_and_keep_receipt_shape(tmp_path):
    config, record, catalog = native(tmp_path)
    with Service(config) as service:
        session = service.lease()
        unrelated = service.finished(service.start(session)['request_id'])
        empty = service.finished(service.start(session, apps=None)['request_id'])
    assert 'skills/extraRoots/set' not in methods(record) and not catalog.exists()
    assert 'app_skills' not in unrelated and 'app_skills' not in empty
    assert unrelated['skills_updated'] == empty['skills_updated'] == []


# spec: ANYM-993
def test_shipped_bundle_matches_recorded_pr1007_blobs():
    assert sorted(path.relative_to(bundle.ROOT).as_posix() for path in bundle.ROOT.rglob('*') if path.is_file()) == [
        name + '/SKILL.md' for name in NAMES]
    for name, (package, digest, blob) in bundle.SKILLS.items():
        data = (bundle.ROOT / name / 'SKILL.md').read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest
        assert hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest() == blob
        assert data.startswith(b'---\nname: ' + name.encode() + b'\n') and package.encode() in data
    assert bundle.verify(bundle.ROOT) is None


# spec: ANYM-993
def test_copied_read_only_bundle_is_offered_and_codex_home_is_never_written(tmp_path):
    root = copied(tmp_path)
    for path in [root, *root.rglob('*')]:
        path.chmod(0o555 if path.is_dir() else 0o444)
    before = sorted(p.name for p in (tmp_path / 'codex-home').rglob('*')) if (tmp_path / 'codex-home').exists() else []
    roots, receipt = offer(tmp_path, root)
    assert roots == [str(root.resolve())] and receipt['status'] == 'offered' and receipt['digest'] == bundle.DIGEST
    assert before == [] and sorted(p.name for p in (tmp_path / 'codex-home').rglob('*')) == ['skills']
    assert offer(tmp_path, root, apps=('example.food',)) == ([], None)
    assert bundle.offer(None, None, tmp_path / 'sessions' / 'run-live-x', root=root) == ([], None)


# spec: ANYM-993
def test_unsafe_bundle_or_same_name_host_skill_refuses_without_touching_files(tmp_path):
    root = copied(tmp_path)
    roots, receipt = offer(tmp_path, tmp_path / 'missing')
    assert roots == [] and receipt == {'version': bundle.VERSION, 'source': bundle.SOURCE, 'skills': NAMES, 'digest': bundle.DIGEST,
                                       'apps': [GOJEK], 'status': 'refused', 'reason': 'bundle_unavailable'}

    skill = root / NAMES[0] / 'SKILL.md'
    original = skill.read_bytes()
    skill.write_bytes(original + b'\nextra\n')
    assert offer(tmp_path, root) == ([], {**receipt, 'reason': 'bundle_digest_mismatch'})
    skill.write_bytes(original)
    (root / NAMES[1] / 'notes.txt').write_text('foreign')
    assert offer(tmp_path, root)[1]['reason'] == 'bundle_foreign_asset'
    (root / NAMES[1] / 'notes.txt').unlink()
    (root / 'android-other-navigation').mkdir()
    assert offer(tmp_path, root)[1]['reason'] == 'bundle_foreign_asset'
    (root / 'android-other-navigation').rmdir()
    (root / 'link').symlink_to(root / NAMES[0])
    assert offer(tmp_path, root)[1]['reason'] == 'bundle_foreign_asset'
    (root / 'link').unlink()
    assert offer(tmp_path, root)[1]['status'] == 'offered'

    cwd = tmp_path / 'sessions' / 'run-live-x'
    inside = cwd / 'bundle'
    shutil.copytree(root, inside)
    assert offer(tmp_path, inside)[1]['reason'] == 'bundle_native_writable'

    user = tmp_path / 'codex-home' / 'skills' / NAMES[2] / 'SKILL.md'
    user.parent.mkdir(parents=True)
    user.write_text('my own notes')
    assert offer(tmp_path, root) == ([], {**receipt, 'reason': 'same_name_skill_present'})
    assert user.read_text() == 'my own notes'
    user.parent.rename(user.parent.with_name('mine'))
    assert offer(tmp_path, root)[1]['status'] == 'offered'
    (user.parent.with_name('mine') / 'SKILL.md').write_text('---\nname: ' + NAMES[3] + '\ndescription: x\n---\n')
    assert offer(tmp_path, root)[1]['reason'] == 'same_name_skill_present'
    shutil.rmtree(user.parent.with_name('mine'))
    agents = cwd / '.agents' / 'skills' / 'nested' / NAMES[0]
    agents.mkdir(parents=True)
    (agents / 'SKILL.md').write_text('agent-authored')
    assert offer(tmp_path, root)[1]['reason'] == 'same_name_skill_present'
    assert (agents / 'SKILL.md').read_text() == 'agent-authored'
