"""The network the module event router shares with recordings-api (ANYM-732): Docker on Linux, synthetic only.

Set DEEDKIT_TEST_PYTHON_IMAGE to the immutable Python image (the CI `python` step does); then missing Docker is a
failure. The network is created with the options of the runbook's own command (docs/operations/production.md,
"Module event router", step 6b), read from that file, under a test name and bridge name. Two containers play the
router and recordings-api: the feed answers on `recordings-api:13234`; the router stand-in has no listener; neither
reaches a public address, a name resolver, the host's own gateway addresses or a container on another bridge.
"""
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[1] / 'docs/operations/production.md'
LISTEN = 'import socket, sys\ns = socket.create_server(("", int(sys.argv[1])))\nwhile True: s.accept()[0].close()\n'
TARGETS = r'''
import json, socket, sys
def connect(host, port):
    try:
        socket.create_connection((host, int(port)), timeout=3).close()
        return 'connected'
    except ConnectionRefusedError:
        return 'refused'
    except TimeoutError:
        return 'timeout'
    except OSError:
        return 'unreachable'
print(json.dumps({target: connect(*target.rsplit(':', 1)) for target in sys.argv[1:]}))
'''


def docker(*args, check=True, timeout=120):
    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=timeout)
    assert not check or result.returncode == 0, f"docker {' '.join(args)}\n{result.stdout}\n{result.stderr}"
    return result


def runbook_options():
    """The `docker network create` options of the runbook, and the bridge and network names it uses."""
    found = re.search(r'`docker network create (--internal (?:-o \S+ )+)(\S+)`', DOCS.read_text())
    assert found, 'the runbook names no network create command'
    options = found[1].split()
    assert options[0] == '--internal' and found[2] == 'deedkit-recordings-router'
    return options, found[2]


# spec: ANYM-732
def test_the_runbook_network_is_internal_isolated_and_lets_only_the_two_containers_talk():
    image = os.environ.get('DEEDKIT_TEST_PYTHON_IMAGE')
    if not image:
        pytest.skip('set DEEDKIT_TEST_PYTHON_IMAGE to run the Docker network proof')
    assert re.search(r'@sha256:[0-9a-f]{64}$', image), 'the Python test image must be immutable'
    assert shutil.which('docker'), 'Docker is required when the network proof is enabled'
    options, _ = runbook_options()
    assert 'com.docker.network.bridge.gateway_mode_ipv4=isolated' in options
    tag = uuid.uuid4().hex[:12]
    port = str(20000 + int(tag[:4], 16) % 20000)
    network, other = f'deedkit_t732_rr_{tag}', f'deedkit_t732_br_{tag}'
    feed, router, host_listener, elsewhere = (f'deedkit_t732_{role}_{tag}' for role in ('feed', 'router', 'host', 'else'))
    # The test's own bridge name (at most 15 characters), the runbook's other options exactly.
    created = [option.replace('dk-recrouter0', 'dkt' + tag[:8]) for option in options]
    try:
        docker('network', 'create', *created, network)
        docker('network', 'create', other)
        inspected = json.loads(docker('network', 'inspect', network).stdout)[0]
        assert inspected['Internal'] is True and inspected['Options']['com.docker.network.bridge.gateway_mode_ipv4'] == 'isolated'
        docker('run', '-d', '--rm', '--name', feed, '--network', network, '--network-alias', 'recordings-api', image,
               'python', '-c', LISTEN, '13234')
        docker('run', '-d', '--rm', '--name', router, '--network', network, image, 'python', '-c', 'import time; time.sleep(120)')
        docker('run', '-d', '--rm', '--name', host_listener, '--network', 'host', image, 'python', '-c', LISTEN, port)
        docker('run', '-d', '--rm', '--name', elsewhere, '--network', other, image, 'python', '-c', LISTEN, '8080')
        members = json.loads(docker('network', 'inspect', network, '--format', '{{json .Containers}}').stdout)
        assert sorted(item['Name'] for item in members.values()) == sorted([feed, router])
        subnet = json.loads(docker('network', 'inspect', network, '--format', '{{json .IPAM.Config}}').stdout)[0]['Subnet']
        gateway = '.'.join(subnet.split('/')[0].split('.')[:3] + ['1'])
        default_gateway = docker('network', 'inspect', 'bridge', '--format', '{{(index .IPAM.Config 0).Gateway}}').stdout.strip()
        elsewhere_ip = docker('inspect', elsewhere, '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}').stdout.strip()
        router_ip = docker('inspect', router, '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}').stdout.strip()
        # The positive control: the host listener answers a container on the default bridge at its gateway.
        control = docker('run', '--rm', '--network', 'bridge', image, 'python', '-c', TARGETS, f'{default_gateway}:{port}')
        assert json.loads(control.stdout) == {f'{default_gateway}:{port}': 'connected'}
        # The router stand-in: the feed answers by its alias; nothing outside the network does.
        outside = [f'{gateway}:{port}', f'{default_gateway}:{port}', f'{elsewhere_ip}:8080', '1.1.1.1:443', 'example.com:443']
        result = json.loads(docker('exec', router, 'python', '-c', TARGETS, 'recordings-api:13234', *outside).stdout)
        assert result.pop('recordings-api:13234') == 'connected'
        assert 'connected' not in result.values(), result
        # Recordings-api reaches no more: the router stand-in listens on nothing (refused), the rest as above.
        back = json.loads(docker('exec', feed, 'python', '-c', TARGETS, f'{router_ip}:13234', *outside).stdout)
        assert back.pop(f'{router_ip}:13234') == 'refused'
        assert 'connected' not in back.values(), back
    finally:
        docker('stop', feed, router, host_listener, elsewhere, check=False)
        docker('network', 'rm', network, other, check=False)
