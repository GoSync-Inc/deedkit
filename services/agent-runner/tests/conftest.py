import pytest
from starlette.testclient import TestClient

from deedkit_agent_runner import config as cfg
from deedkit_agent_runner.app import PeerGuard, create_app

from support import PEER, Clock, make_runner


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def runner(tmp_path, clock):
    value = make_runner(tmp_path, clock=clock)
    yield value
    for container in list(value.docker.containers.values()):
        if container.proc and container.proc.poll() is None:
            value.docker.kill(container.name)
    value.join(30)


@pytest.fixture
def client(runner):
    app = PeerGuard(create_app(runner, cfg.Keys(runner.config_path)), [PEER])
    with TestClient(app, client=(PEER, 50000)) as value:
        yield value

