"""Mandatory, typed app learning. Persistent knowledge contains identifiers and enums only."""

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Literal

from pydantic import Field, model_validator

from .config import Strict

# Static Android resource identifiers only, never text selectors or dynamic user IDs.
PACKAGE = r'^[a-z][a-z_]*(?:\.[a-z][a-z_]*)+$'
RESOURCE = r'^[a-z][a-z_]*(?:\.[a-z][a-z_]*)+:id/[a-z][a-z_]*$'


class Step(Strict):
    action: Literal['tap', 'read', 'wait', 'scroll']
    resource_id: str = Field(pattern=RESOURCE, max_length=256)


class Lesson(Strict):
    package: str = Field(pattern=PACKAGE, max_length=128)
    steps: list[Step] = Field(max_length=40)
    pitfalls: list[Literal['refresh_hierarchy', 'wait_for_content', 'scroll_to_target',
                         'avoid_stale_coordinates', 'dismiss_keyboard']] = Field(max_length=5)

    @model_validator(mode='after')
    def belongs_to_app(self):
        if not self.steps and not self.pitfalls:
            raise ValueError('no_observed_learning')
        if any(not step.resource_id.startswith(self.package + ':id/') for step in self.steps):
            raise ValueError('foreign_resource')
        return self


class Outcome(Strict):
    summary: str = Field(max_length=4000)
    data: dict[str, str | int | bool | None] = Field(max_length=10)
    lessons: list[Lesson] = Field(max_length=5)


INSTRUCTION = '''---
name: android-learning
description: Complete a phone task and return reusable per-app navigation knowledge.
---
Read the supplied app skills before acting. At completion return the structured result schema:
summary, data, and lessons for apps actually used. Learn from what you observed: static Android
resource IDs with tap/read/wait/scroll steps, and the schema's navigation pitfalls. Omit dynamic
IDs, screen text, personal data, secrets and task values. If nothing reusable was observed, return
no lessons. The service writes validated app skills; do not write CODEX_HOME yourself.
'''


def atomic(path, content):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as out:
            out.write(content)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def setup(home, run_dir, outputs=()):
    root = Path(home) / 'skills'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    atomic(root / 'android-learning' / 'SKILL.md', INSTRUCTION)
    schema = Outcome.model_json_schema()
    schema['properties']['data'] = {'type': 'object', 'additionalProperties': False,
        'properties': {key: {'type': ['string', 'integer', 'boolean', 'null']} for key in outputs},
        'required': list(outputs)}
    atomic(Path(run_dir) / 'result-schema.json', json.dumps(schema))
    return root


def skill_name(package):
    slug = re.sub('[^a-z0-9-]', '-', package)[:40]
    return 'android-' + slug + '-' + hashlib.sha256(package.encode()).hexdigest()[:8]


def publish(home, lessons):
    validated = [Lesson.model_validate(item) for item in lessons]
    root = Path(home) / 'skills'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    updated = []
    for lesson in validated:
        prior = load(home, [lesson.package])
        if prior:
            steps = list(dict.fromkeys((s['action'], s['resource_id']) for s in prior[0]['steps']
                                      + [step.model_dump() for step in lesson.steps]))[-40:]
            lesson = Lesson(package=lesson.package, steps=[{'action': a, 'resource_id': r} for a, r in steps],
                            pitfalls=list(dict.fromkeys(prior[0]['pitfalls'] + lesson.pitfalls)))
        path = Path(home) / 'skills' / skill_name(lesson.package) / 'SKILL.md'
        content = (f'---\nname: {skill_name(lesson.package)}\ndescription: Observed navigation in {lesson.package}.\n'
                   '---\nUse only when it matches the current goal and screen.\n'
                   + '\n'.join(f'- {step.action}: `{step.resource_id}`' for step in lesson.steps)
                   + '\n' + '\n'.join('- ' + item for item in lesson.pitfalls) + '\n')
        atomic(path, content)
        updated.append(lesson.package)
    return updated


def load(home, apps):
    """Load only our validated format; arbitrary model-written files are never reused."""
    result = []
    root = Path(home) / 'skills'
    for path in sorted(root.glob('android-*/SKILL.md'))[:100]:
        if path.is_symlink() or path.parent.is_symlink() or path.stat().st_size > 16384:
            continue
        text = path.read_text()
        match = re.search(r'^description: Observed navigation in ([a-z_.]+)\.$', text, re.MULTILINE)
        package = match[1] if match else ''
        if not re.fullmatch(PACKAGE, package) or path.parent.name != skill_name(package) or (apps and package not in apps):
            continue
        # Reconstruct from the constrained lines, dropping all other content.
        steps, pitfalls = [], []
        for line in text.splitlines():
            match = re.fullmatch(r'- (tap|read|wait|scroll): `([^`]+)`', line)
            if match:
                steps.append({'action': match[1], 'resource_id': match[2]})
            elif line.startswith('- '):
                pitfalls.append(line[2:])
        try:
            lesson = Lesson(package=package, steps=steps, pitfalls=pitfalls)
        except ValueError:
            continue
        result.append(lesson.model_dump())
    return result
