"""The dispatcher's networks fail closed with no host rule at all (ANYM-581): Docker on Linux, synthetic only.

Set DEEDKIT_TEST_PYTHON_IMAGE to the immutable Python image (the CI `python` step does); then missing Docker is a
failure. A container on an internal network alone, as the dispatcher is, cannot resolve a public name and cannot
reach a container on another bridge or a public address, so losing any host rule opens nothing. Measured on
Docker 29: an internal bridge still reaches the host's own gateway address, which is why the dispatcher's only
destinations are the proxy and PostgreSQL by code and the host INPUT rules stay as the second layer.
"""
import json
import os
import re
import shutil
import subprocess
import uuid

import pytest

PROBE = r'''
import json, socket, sys
def connect(address):
    try:
        socket.create_connection(address, timeout=3).close()
        return 'connected'
    except ConnectionRefusedError:
        return 'refused'
    except TimeoutError:
        return 'timeout'
    except OSError:
        return 'unreachable'
try:
    socket.getaddrinfo('example.com', 443)
    dns = 'resolved'
except OSError:
    dns = 'failed'
print(json.dumps({'dns': dns, 'bridge': connect((sys.argv[1], 8080)), 'public': connect(('1.1.1.1', 443))}))
'''
LISTEN = 'import socket\ns = socket.create_server(("", 8080))\nwhile True: s.accept()[0].close()\n'


def docker(*args, check=True, timeout=120):
    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=timeout)
    assert not check or result.returncode == 0, f"docker {' '.join(args)}\n{result.stdout}\n{result.stderr}"
    return result


# spec: ANYM-581
def test_an_internal_only_container_has_no_dns_no_other_bridge_and_no_internet_without_host_rules():
    image = os.environ.get('DEEDKIT_TEST_PYTHON_IMAGE')
    if not image:
        pytest.skip('set DEEDKIT_TEST_PYTHON_IMAGE to run the Docker network proof')
    assert re.search(r'@sha256:[0-9a-f]{64}$', image), 'the Python test image must be immutable'
    assert shutil.which('docker'), 'Docker is required when the network proof is enabled'
    tag = uuid.uuid4().hex[:12]
    internal, bridge, listener = f'deedkit_t581_int_{tag}', f'deedkit_t581_br_{tag}', f'deedkit_t581_listen_{tag}'
    try:
        docker('network', 'create', '--internal', internal)
        docker('network', 'create', bridge)
        docker('run', '-d', '--rm', '--name', listener, '--network', bridge, image, 'python', '-c', LISTEN)
        peer = docker('inspect', listener, '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}').stdout.strip()
        # The positive control: the listener answers on its own bridge, so silence below is the network, not it.
        control = docker('run', '--rm', '--network', bridge, image, 'python', '-c', PROBE, peer)
        assert json.loads(control.stdout)['bridge'] == 'connected'
        probe = docker('run', '--rm', '--network', internal, '--read-only', '--cap-drop', 'ALL', image,
                       'python', '-c', PROBE, peer)
        assert json.loads(probe.stdout) == {'dns': 'failed', 'bridge': 'unreachable', 'public': 'unreachable'}
    finally:
        docker('stop', listener, check=False)
        docker('network', 'rm', internal, bridge, check=False)
