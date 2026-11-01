import pytest
from deedkit_inference.engines import fake
from support import Env


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def testing_mode(monkeypatch):
    monkeypatch.setenv("DEEDKIT_INFERENCE_TESTING", "1")
    monkeypatch.delenv("DEEDKIT_INFERENCE_RELEASE", raising=False)
    fake.EVENTS.clear()
    fake.SPAWNED.clear()
    fake.SEEN_PATHS.clear()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)
