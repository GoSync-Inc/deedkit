"""Repository app-navigation skills install as ordinary Codex skills that BareRun exposes unchanged."""

from pathlib import Path
import re
import shutil

from agent_harness import Service, recorded
from test_agent_bare import bare_host

SKILLS = Path(__file__).resolve().parents[3] / 'skills'
APPS = ('gojek', 'grab', 'halodoc', 'tokopedia')


def frontmatter(text):
    match = re.match(r'---\n(.*?)\n---\n(.+)', text, re.S)
    assert match, 'SKILL.md must open with YAML frontmatter and carry a body'
    fields = dict(line.split(': ', 1) for line in match.group(1).splitlines())
    return fields, match.group(2)


# spec: ANYM-993
def test_app_navigation_skills_install_into_codex_home_and_bare_run_discovers_them_without_injection(tmp_path):
    names = [f'android-{app}-navigation' for app in APPS]
    for name in names:
        fields, body = frontmatter((SKILLS / name / 'SKILL.md').read_text())
        assert set(fields) == {'name', 'description'} and fields['name'] == name  # Codex loader keys
        assert len(name) <= 64 and 20 <= len(fields['description']) <= 1024 and body.strip()
    config, record = bare_host(tmp_path, native_skill=False)
    installed = tmp_path / 'codex-home' / 'skills'
    for name in names:  # the documented root install: copy each directory under <codex_home>/skills/
        shutil.copytree(SKILLS / name, installed / name)
    with Service(config) as service:
        run = service.finished(service.start(service.lease())['request_id'])
        assert run['state'] == 'succeeded' and run['skills_updated'] == []
        seen = recorded(record)
        for name in names:
            text = (SKILLS / name / 'SKILL.md').read_text()
            assert seen['known_skills'][f'{name}/SKILL.md'] == text
            assert frontmatter(text)[1].strip() not in seen['stdin']  # discovered, never injected
