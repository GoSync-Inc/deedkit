import pytest
from deedkit_inference import models as pins
from engine_support import TINY_PNG, engines
from support import chat_body, fk

pytestmark = pytest.mark.anyio


# spec: ANYM-683
async def test_language_routing_ru_to_gigaam_others_to_whisper(env):
    async with env.running(**engines(env)) as s:
        russian = await s.transcribe(fk(language="ru"))
        english = await s.transcribe(fk(language="en"), extra_fields={"response_format": "verbose_json"})
        assert russian.status == 200 and russian.json()["text"] == "gigaam transcript 0"
        assert english.status == 200 and english.json()["text"] == "whisper transcript 0"
        assert english.json()["language"] == "en"
        # The identification phase ran for both (nothing named the language), on the small model.
        assert set(s.service.engines["audio-default"].workers) == {"langid", "gigaam", "whisper"}


# spec: ANYM-683
async def test_named_language_skips_identification(env):
    async with env.running(**engines(env)) as s:
        reply = await s.transcribe(fk(), extra_fields={"language": "ru-RU"})
        assert reply.status == 200 and reply.json()["text"] == "gigaam transcript 0"
        assert set(s.service.engines["audio-default"].workers) == {"gigaam"}  # langid was never started
        reply = await s.transcribe(fk(), extra_fields={"language": "de"})
        assert reply.json()["text"] == "whisper transcript 0"
        assert set(s.service.engines["audio-default"].workers) == {"gigaam", "whisper"}


# spec: ANYM-683
async def test_each_phase_is_admitted_on_its_own_slot_and_memory(env):
    async with env.running(**engines(env)) as s:
        slots = s.service.scheduler.slots

        def running(slot):
            return slots[slot].running.key[1] if slots[slot].running else None

        task = s.spawn(s.transcribe(fk(language="ru", sleep=0.8)))
        await s.until(lambda: running("gpu") == "langid")
        await s.until(lambda: running("cpu") == "gigaam" and running("gpu") is None)
        assert (await task).status == 200
        book = s.service.admission.loaded
        assert book[("audio-default", "langid")].gb == 0.3 and book[("audio-default", "gigaam")].gb == 1.6
        assert not s.service.admission.pending
        # Memory is checked per phase: 3.5 GB free admits the 0.3 GB identification (needs 3.3) but not the 2.7 GB
        # Whisper phase after it (needs 5.7), so a request for another language is refused at that phase.
        s.probe.available = 3.5
        refused = await s.transcribe(fk(language="en"))
        assert refused.status == 503 and refused.code == "admission_closed"
        assert refused.headers["x-admission-reason"] == "memory_low"


# spec: ANYM-683
async def test_missing_engine_is_503(env, monkeypatch):
    from importlib import metadata

    from deedkit_inference.engines import pinned

    def absent(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(pinned.metadata, "version", absent)
    kwargs = engines(env)
    for spec in kwargs["aliases"].values():
        del spec["engine_options"]["runtime"]  # the real runtime needs the pinned packages
    async with env.running(**kwargs) as s:
        reply = await s.transcribe(fk())
        assert reply.status == 503 and reply.code == "engine_unavailable"
        assert "retry-after" in reply.headers
        reply = await s.chat(chat_body(image=TINY_PNG))
        assert reply.status == 503 and reply.code == "engine_unavailable"
        assert s.service.engines["audio-default"].workers == {}


# spec: ANYM-683
async def test_shipped_aliases_load_and_every_phase_is_pinned(env):
    from deedkit_inference.config import Config

    config = Config.model_validate({**env.config_dict(), "aliases": {}})
    assert set(config.aliases) == {"audio-default", "vision-default", "document-structure"}  # ANYM-850
    assert config.aliases["vision-default"].scopes == ["image", "document"]
    models = pins.load_models()
    for alias, spec in config.aliases.items():
        for phase in spec.phases:
            assert f"{alias}/{phase.name}" in models["phases"], (alias, phase.name)
    assert [p.name for p in config.aliases["audio-default"].phases] == ["langid", "gigaam", "whisper"]
    assert {p.route for p in config.aliases["audio-default"].phases} == {None, "ru", "other"}
