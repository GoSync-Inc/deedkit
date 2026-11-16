"""The packaged app-skill bundle is the canonical skill set, GrabMart hint included, offered whole (DK-48)."""

import hashlib
import json
from pathlib import Path

from agent_harness import Service
from test_app_skill_delivery import native
from test_codex_session import calls

from deedkit_android import app_skill_bundle as bundle

CANONICAL = Path(__file__).resolve().parents[3] / 'skills'
GRAB = 'com.grabtaxi.passenger'
V1_DIGEST = 'b225046a1a2aeb9f86a6c7a7b263b8d635204eea19c3671616817a6ede72f923'
V1 = {  # PR 1007 blobs; the v1 directory stays byte-identical for rollback and lineage
    'android-gojek-navigation': '14a5fe87f2f3e998e26003eee178eb7d20a69e5e',
    'android-grab-navigation': 'f5013385e14c5defd4596d3ac8291770ba8028c7',
    'android-halodoc-navigation': '0b8ff9aebb0f6fa06149d08ebb6a480686af06a7',
    'android-tokopedia-navigation': '709461a3b58812aaab17a23133f92a1b5edccf76',
}


def blob(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


# spec: ANYM-993
def test_current_bundle_is_the_canonical_skill_set_byte_for_byte():
    assert bundle.VERSION >= 2 and bundle.ROOT.name == f'v{bundle.VERSION}'
    assert sorted(bundle.SKILLS) == sorted(p.name for p in CANONICAL.glob('android-*-navigation'))
    for name, (package, digest, recorded) in bundle.SKILLS.items():
        data = (bundle.ROOT / name / 'SKILL.md').read_bytes()
        assert data == (CANONICAL / name / 'SKILL.md').read_bytes(), name
        assert hashlib.sha256(data).hexdigest() == digest and blob(data) == recorded
    assert set(bundle.SOURCE) == {'pull_request', 'commit'} and len(bundle.SOURCE['commit']) == 40
    assert bundle.SOURCE['pull_request'] != 1007 and bundle.DIGEST != V1_DIGEST
    assert bundle.DIGEST == hashlib.sha256(json.dumps(
        {'version': bundle.VERSION, 'source': bundle.SOURCE, 'skills': bundle.SKILLS}, sort_keys=True).encode()).hexdigest()


# spec: ANYM-993
def test_previous_bundle_stays_byte_identical_beside_the_current_one():
    v1 = bundle.ROOT.parent / 'v1'
    assert sorted(p.relative_to(v1).as_posix() for p in v1.rglob('*')) == sorted([*V1, *(n + '/SKILL.md' for n in V1)])
    assert {p.parent.name: blob(p.read_bytes()) for p in v1.glob('*/SKILL.md')} == V1


# spec: ANYM-993
def test_grab_goal_offers_the_canonical_grabmart_hint_through_native_extra_roots_only(tmp_path):
    config, record, catalog = native(tmp_path)
    with Service(config) as service:
        session = service.lease()
        grab = service.finished(service.start(session, apps=[GRAB])['request_id'])
        other = service.finished(service.start(session)['request_id'])
    seen = calls(record)
    roots = [r['message']['params']['extraRoots'] for r in seen if r['message'].get('method') == 'skills/extraRoots/set']
    assert roots == [[str(bundle.ROOT.resolve())], []]
    assert [json.loads(line) for line in catalog.read_text().splitlines()] == [
        [name + '/SKILL.md' for name in sorted(bundle.SKILLS)], []]
    offered = Path(roots[0][0]) / 'android-grab-navigation' / 'SKILL.md'
    assert offered.read_bytes() == (CANONICAL / 'android-grab-navigation' / 'SKILL.md').read_bytes()
    assert grab['app_skills'] == {'version': bundle.VERSION, 'source': bundle.SOURCE, 'skills': sorted(bundle.SKILLS),
                                  'digest': bundle.DIGEST, 'apps': [GRAB], 'status': 'offered'}
    assert grab['skills_updated'] == [] and 'app_skills' not in other
    turns = [r['message']['params'] for r in seen if r['message'].get('method') == 'turn/start']
    assert len(turns) == 2
    for turn in turns:  # offered as a catalog entry, never injected into the goal
        assert [item['type'] for item in turn['input']] == ['text']
        assert 'GrabMart' not in turn['input'][0]['text'] and 'Search this store' not in turn['input'][0]['text']
