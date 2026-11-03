"""Recordings deployment contract (ANYM-711): repository files only, no Docker, provider or production access."""
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'services/recordings/src'))
sys.path.insert(0, str(ROOT))
OVERLAY = ROOT / 'deploy/compose.recordings.yaml'
GATEWAY_OVERLAY = ROOT / 'deploy/compose.gateway-recordings.yaml'
NETWORK = 'deedkit-recordings-backend'


def compose(path):
    return yaml.safe_load(path.read_text())


def final_stage(dockerfile):
    lines = []
    for line in (value.strip() for value in dockerfile.read_text().replace('\\\n', ' ').splitlines()):
        if not line or line.startswith('#'):
            continue
        if line.upper().startswith('FROM '):
            lines = []
        lines.append(line)
    return lines


# spec: ANYM-711
def test_the_recordings_image_runs_unprivileged_and_ends_with_the_source_revision():
    text = (ROOT / 'services/recordings/Dockerfile').read_text()
    assert len(re.findall(r'^FROM python:3\.13-slim-bookworm@sha256:[0-9a-f]{64}', text, re.M)) == 2
    assert 'uv sync --locked --no-dev --no-editable' in text
    final = final_stage(ROOT / 'services/recordings/Dockerfile')
    assert 'USER 10001:10001' in final
    assert any('chown 10001:10001 /data' in line and 'chmod 700 /data' in line for line in final)
    assert 'ENTRYPOINT ["deedkit-recordings"]' in final
    command = json.loads(next(line for line in final if line.startswith('CMD '))[4:])
    assert command == ['--config', '/run/deedkit/recordings/config.json', '--host', '0.0.0.0', '--port', '13234']
    pair = ['ARG DEEDKIT_SOURCE_SHA', 'ENV DEEDKIT_SOURCE_SHA=$DEEDKIT_SOURCE_SHA']
    assert any(final[i:i + 2] == pair for i in range(len(final))) and sum('DEEDKIT_SOURCE_SHA' in line for line in final) == 2
    assert final.index(pair[0]) > max(i for i, line in enumerate(final) if line.startswith(('COPY ', 'RUN ')))
    assert 'services/recordings/' in (ROOT / '.github/workflows/images.yml').read_text()


# spec: ANYM-711
def test_recordings_is_a_published_attested_build_target_that_imports_in_the_image_smoke():
    from scripts import production, publish_images
    context, dockerfile, stage, smoke = production.BUILDS['recordings']
    assert (context, dockerfile, stage) == ('services/recordings', 'services/recordings/Dockerfile', '')
    assert 'recordings' in production.PUBLISHED and 'recordings' in publish_images.TARGETS
    exec(compile(smoke, 'smoke', 'exec'), {})  # the smoke statement itself runs in the image; here with the source tree
    workflows = ROOT / '.github/workflows'
    assert 'recordings,' in (workflows / 'images.yml').read_text()
    ci = (workflows / 'ci.yml').read_text()
    assert ('steps.publish.outputs.attest_recordings' in ci and 'steps.publish.outputs.digest_recordings' in ci
            and 'ghcr.io/gosync-inc/deedkit-recordings' in ci)


# spec: ANYM-711
def test_recordings_api_is_confined_to_the_internal_backend_and_the_filtered_egress_bridge():
    document = compose(OVERLAY)
    assert set(document['services']) == {'recordings-api'}
    unit = document['services']['recordings-api']
    assert not unit.get('ports') and 'network_mode' not in unit
    assert set(unit['networks']) == {'recordings-backend', 'recordings-egress'}  # never `integration`
    assert unit['networks']['recordings-backend']['aliases'] == ['recordings-api']
    assert 'integration' not in OVERLAY.read_text().replace('`integration`', '')
    volumes = unit['volumes']
    config = [v for v in volumes if v.endswith(':/run/deedkit/recordings:ro')]
    assert len(config) == 1 and config[0].startswith('${RECORDINGS_STATE_DIR:?')
    assert not any(v.endswith('.json:ro') or '.json:' in v for v in volumes)  # a directory, never one file
    assert 'recordings-data:/data' in volumes and document['volumes'] == {'recordings-data': {}}
    backend = document['networks']['recordings-backend']
    assert backend['external'] is True and backend['name'].endswith(':-' + NETWORK + '}')
    egress = document['networks']['recordings-egress']
    assert egress['enable_ipv6'] is False and egress['driver_opts'] == {'com.docker.network.bridge.name': 'dk-recordings0'}
    assert 'internal' not in egress  # it must have a route out; the host rules (ANYM-712) filter it
    assert not re.search(r'(?i)(token|secret|password)\s*[:=]\s*\S{8,}', OVERLAY.read_text())


# spec: ANYM-711
def test_the_gateway_overlay_adds_only_the_recordings_network():
    document = compose(GATEWAY_OVERLAY)
    assert set(document) == {'services', 'networks'}
    assert document['services'] == {'gateway': {'networks': ['recordings-backend']}}
    assert document['networks']['recordings-backend']['external'] is True
    assert document['networks']['recordings-backend']['name'].endswith(':-' + NETWORK + '}')
    assert compose(OVERLAY)['networks']['recordings-backend'] == document['networks']['recordings-backend']
    base = compose(ROOT / 'deploy/compose.gateway.yaml')
    assert base['services']['gateway']['networks'] == ['integration']  # unchanged: no overlay, no new network
    assert 'recordings' not in (ROOT / 'deploy/compose.gateway.yaml').read_text()


# spec: ANYM-711
def test_the_examples_are_valid_synthetic_configurations_with_container_paths():
    from deedkit_recordings.security import Document
    plain = json.loads((ROOT / 'deploy/recordings.example.json').read_text())
    archived = json.loads((ROOT / 'deploy/recordings-archive.example.json').read_text())
    assert 'archive' not in plain and all('archive_namespace' not in account for account in plain['accounts'])
    for value in (plain, archived):
        Document.model_validate(value)
        assert value['cursor_secret_file'].startswith('/run/deedkit/recordings/')
        assert all(a['token_file'].startswith('/run/deedkit/recordings/') for a in value['accounts'])
        assert all(re.fullmatch(r'0{64}', p['key_sha256']) for p in value['principals'])
        assert 'recordings:audio' not in json.dumps(value)
    assert archived['archive']['directory'] == '/data/archive'
    assert {a['archive_namespace'] for a in archived['accounts']} == {'0' * 32, '1' * 32}


# spec: ANYM-711
def test_the_operator_documentation_names_the_recordings_section_and_its_four_operations():
    docs = (ROOT / 'docs/operations/production.md').read_text()
    section = docs[docs.index('## Recordings (ANYM-711)'):docs.index('## Image retention')]
    for needle in ('*Add an account:*', '*Rotate a token:*', '*Replace an account:*', '*Read archive health:*',
                   'deploy/recordings.example.json', 'deploy/recordings-archive.example.json',
                   'deploy/compose.gateway-recordings.yaml', '/etc/deedkit/http-production.json',
                   'seamless_release.py prepare', 'NEWER green `main` SHA', 'dk-recbackend0',
                   'gateway_mode_ipv4=isolated', 'ONLY (plan B accepts exactly two services'):
        assert needle in section, needle
    assert 'recordings' in docs[docs.index('Available build targets'):][:300]
    readme = (ROOT / 'services/recordings/README.md').read_text()
    assert '/data/archive' in readme and 'no recordings\nmanifest/image' not in readme


# spec: ANYM-711
def test_the_recordings_package_has_no_sql_or_migration_that_pauses_unattended_deploys():
    from scripts import production
    found = [path.relative_to(ROOT).as_posix() for path in (ROOT / 'services/recordings').rglob('*')
             if path.is_file() and '.venv' not in path.parts]
    assert found and not [path for path in found if production.SQL_INVENTORY.search(path)]
