"""ANYM-937 criterion 8: the `whatsapp-holder` image and the holder Compose overlay.

The image builds from whatsapp/whatsmeow alone, so ANYM-895's input digest keeps the holders through a release that
changes only messaging_service; the rendered overlay passes ANYM-914's session-locks check with the holder as the only
unit that binds the session and lock directories. Rendering runs `docker compose config` on a synthetic copy of deploy/.
"""
import os
import re
import shutil
from pathlib import Path

import pytest

from messaging_service import whatsapp_holder
from scripts import production as p

ROOT = Path(__file__).resolve().parents[1]
HOLDER = 'compose.whatsapp-holder.yaml'


def go_image(dockerfile):
    return re.search(r'^ARG GO_IMAGE=(\S+)$', (ROOT / dockerfile).read_text(), re.M).group(1)


def labels(dockerfile, kind):
    text = (ROOT / dockerfile).read_text()
    return tuple(int(re.search(rf'io\.deedkit\.{kind}\.protocol\.{end}="([0-9]+)"', text).group(1))
                 for end in ('min', 'max'))


# spec: ANYM-937
def test_the_holder_image_is_a_python_free_build_of_whatsmeow_with_its_protocol_range_labels():
    context, dockerfile, stage, smoke = p.BUILDS['whatsapp-holder']
    assert (context, dockerfile, stage, smoke) == ('whatsapp/whatsmeow', 'whatsapp/whatsmeow/Dockerfile',
                                                    'whatsapp-holder', '--version')
    chain = p.stage_chain(p.dockerfile_stages((ROOT / dockerfile).read_text()), stage)
    copied = {path for item in chain for path in item['copies']}
    assert p.context_paths(context, sorted(copied)) == ['whatsapp/whatsmeow', 'whatsapp/whatsmeow/go.mod',
                                                        'whatsapp/whatsmeow/go.sum']
    assert go_image(dockerfile) == go_image('services/messaging/Dockerfile')  # one Go pin
    serve = (ROOT / 'whatsapp/whatsmeow/serve.go').read_text()
    holder_range = tuple(int(v) for v in re.search(r'holderSupported = \[2\]int\{(\d+), (\d+)\}', serve).groups())
    assert labels(dockerfile, 'holder') == holder_range
    assert labels('services/messaging/Dockerfile', 'collector') == whatsapp_holder.COLLECTOR_SUPPORTED
    assert 'whatsapp-holder' not in p.PUBLISHED and 'whatsapp-holder' not in p.MESSAGING_TARGETS


# spec: ANYM-937
def test_a_messaging_service_only_change_keeps_the_holder_digest_and_changes_the_collectors():
    entries = p.tree_entries(ROOT)
    changed = dict(entries)
    path = 'services/messaging/messaging_service/whatsapp_holder.py'
    assert path in changed
    changed[path] = changed[path].replace(changed[path].split()[1], '0' * 40)
    assert p.build_inputs(ROOT, 'whatsapp-holder', changed) == p.build_inputs(ROOT, 'whatsapp-holder', entries)
    assert p.build_inputs(ROOT, 'whatsapp-native', changed) != p.build_inputs(ROOT, 'whatsapp-native', entries)
    go = dict(entries)
    source = 'whatsapp/whatsmeow/serve.go'
    go[source] = go[source].replace(go[source].split()[1], '0' * 40)
    assert p.build_inputs(ROOT, 'whatsapp-holder', go) != p.build_inputs(ROOT, 'whatsapp-holder', entries)


@pytest.fixture()
def rendered(tmp_path):
    if not shutil.which('docker'):
        if os.environ.get('DEEDKIT_TEST_PYTHON_IMAGE'):
            pytest.fail('Docker Compose is required where DEEDKIT_TEST_PYTHON_IMAGE is set')
        pytest.skip('needs the docker compose CLI')
    from test_compose_runtime import BASE, NATIVE, Stack
    stack = Stack(tmp_path, 'unused')
    private = stack.deploy / 'private'
    for name in ('session-locks', 'whatsapp1-holder', 'whatsapp2-holder'):
        (private / name).mkdir(mode=0o700)
    for name in ('whatsapp1-holder.dsn', 'whatsapp2-holder.dsn'):
        (private / name).write_text('synthetic\n')
    stack.new_project()
    return stack, stack.render(BASE + [NATIVE, HOLDER])


# spec: ANYM-937
def test_the_rendered_holder_overlay_passes_the_session_locks_check_and_the_collectors_mount_no_session(rendered,
                                                                                                       monkeypatch):
    stack, config = rendered
    services = config['services']
    monkeypatch.setattr(p, 'SESSION_OWNER', (os.getuid(), os.getgid()))
    named = sorted(services.items())
    p.provide_session_locks(named, required=True)
    for account in ('whatsapp1', 'whatsapp2'):
        holder, collector = services[account + '-holder'], services[account]
        targets = {target for _, target in p.binds(holder)}
        assert {'/sessions', p.SESSION_LOCKS, '/run/deedkit/holder', '/run/secrets/holder_dsn'} <= targets
        assert p.grace_seconds(holder['stop_grace_period']) >= p.SESSION_GRACE_S
        assert holder['user'] == '10001:10001' and holder['read_only'] is True and holder['cap_drop'] == ['ALL']
        mounted = p.binds(collector)
        assert not any(os.path.basename(source).endswith('-session') for source, _ in mounted)
        assert all(target != p.SESSION_LOCKS for _, target in mounted)
        assert ('/run/deedkit/holder' in {target for _, target in mounted}
                and any(v.get('target') == '/spool' for v in collector['volumes']))
        assert [v['source'] for v in holder['volumes'] if v['target'] == '/spool'] == [
            v['source'] for v in collector['volumes'] if v['target'] == '/spool']  # one events spool
    # A holder that lost the lock bind is refused before anything stops: the check covers the holder unit.
    broken = dict(services['whatsapp1-holder'])
    broken['volumes'] = [v for v in broken['volumes'] if v['target'] != p.SESSION_LOCKS]
    with pytest.raises(p.Refused, match='session_locks_unmounted'):
        p.provide_session_locks([('whatsapp1-holder', broken)], required=True)
