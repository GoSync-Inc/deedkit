"""Exact versioned app navigation skills offered to the persistent native thread (docs/android-app-skill-delivery.md).

Codex 0.166 app-server always registers its host skill provider, so `features.skip_host_skill_discovery` does not
empty the host catalog there; `skills/extraRoots/set` adds a read-only root to it. Nothing is copied into
CODEX_HOME, nothing reaches the turn input, and the reader may still decline the hint.
"""

import hashlib
import json
import os
from pathlib import Path

VERSION = 2  # v1 (PR 1007) stays shipped byte-identical beside it for rollback and lineage
ROOT = Path(__file__).parent / 'app_skills' / f'v{VERSION}'
SOURCE = {'pull_request': 1019, 'commit': '750e3988600455a099bb26e11d8bea069c6cca7a'}
SKILLS = {  # name: (package, sha256, git blob of skills/<name>/SKILL.md at SOURCE)
    'android-gojek-navigation': ('com.gojek.app',
        '542a0cb1af0533ee5988acf62bf6b88cd1163974ba4f69a9becf89f9e41ef894', '14a5fe87f2f3e998e26003eee178eb7d20a69e5e'),
    'android-grab-navigation': ('com.grabtaxi.passenger',
        'a92a67064b54e79ebef5c710bcea4d6578ac4534f9a92840664c89fccfe42078', 'b4e114a0ebca3c06ab41a406fe76545336186722'),
    'android-halodoc-navigation': ('com.linkdokter.halodoc.android',
        '5847c6abbfb360faa296c667805bd0693049d68f1bfe9a15ef69dc74a6b9a408', '0b8ff9aebb0f6fa06149d08ebb6a480686af06a7'),
    'android-tokopedia-navigation': ('com.tokopedia.tkpd',
        '44c28b36eea00e9cf1a215ddae3c642307b640a64c46311862bc03e0565fe663', '709461a3b58812aaab17a23133f92a1b5edccf76'),
}
DIGEST = hashlib.sha256(json.dumps({'version': VERSION, 'source': SOURCE, 'skills': SKILLS}, sort_keys=True).encode()).hexdigest()
SCAN_LIMIT = 5000


def verify(root):
    """None when root holds exactly the recorded files and nothing else; otherwise the refusal reason."""
    try:
        if root.is_symlink() or not root.is_dir():
            return 'bundle_unavailable'
        found = {}
        for directory, dirs, files in os.walk(root):
            for name in dirs + files:
                path = Path(directory) / name
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or not (relative in SKILLS or relative.endswith('/SKILL.md')
                                             and relative.removesuffix('/SKILL.md') in SKILLS and path.is_file()):
                    return 'bundle_foreign_asset'
                if name == 'SKILL.md':
                    with path.open('rb') as stream:
                        found[relative.removesuffix('/SKILL.md')] = hashlib.file_digest(stream, 'sha256').hexdigest()
    except OSError:
        return 'bundle_unavailable'
    if set(found) != set(SKILLS):
        return 'bundle_unavailable'
    if any(found[name] != digest for name, (_, digest, _) in SKILLS.items()):
        return 'bundle_digest_mismatch'
    return None


def declared_name(path):
    try:
        with path.open('rb') as stream:
            head = stream.read(4096).decode(errors='replace').splitlines()
    except OSError:
        return None
    if not head or head[0].strip() != '---':
        return None
    for line in head[1:]:
        if line.strip() == '---':
            return None
        if line.startswith('name:'):
            return line.removeprefix('name:').strip().strip('\'"')
    return None


def collision(codex_home, cwd):
    """A same-name skill in a root native 0.166 already discovers: $CODEX_HOME/skills, $HOME (= cwd) and its
    ancestors' .agents/skills, /etc/codex/skills. Read only; the user's or agent's file is never changed."""
    roots = [Path(codex_home) / 'skills'] if codex_home else []
    roots += [base / '.agents' / 'skills' for base in (cwd, *cwd.parents)] + [Path('/etc/codex/skills')]
    seen = 0
    for root in roots:
        if not root.is_dir():
            continue
        for directory, dirs, files in os.walk(root, followlinks=True):
            seen += len(dirs) + len(files)
            if seen > SCAN_LIMIT:
                return 'skill_scan_incomplete'
            if 'SKILL.md' in files and (Path(directory).name in SKILLS
                                        or declared_name(Path(directory) / 'SKILL.md') in SKILLS):
                return 'same_name_skill_present'
    return None


def offer(apps, codex_home, cwd, *, root=ROOT):
    """([extra root] or [], receipt); receipt None for empty or unrelated apps, which keep the prior behaviour."""
    matched = sorted({package for package, _, _ in SKILLS.values()} & set(apps or ()))
    if not matched:
        return [], None
    receipt = {'version': VERSION, 'source': SOURCE, 'skills': sorted(SKILLS), 'digest': DIGEST, 'apps': matched}
    resolved = Path(root).resolve()
    reason = verify(Path(root))
    if reason is None and resolved.is_relative_to(Path(cwd).resolve()):
        reason = 'bundle_native_writable'
    reason = reason or collision(codex_home, Path(cwd))
    if reason:
        return [], receipt | {'status': 'refused', 'reason': reason}
    return [str(resolved)], receipt | {'status': 'offered'}
