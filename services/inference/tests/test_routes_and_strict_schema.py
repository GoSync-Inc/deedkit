import base64
import json
import os
import re
import subprocess
import sys

import pytest
from deedkit_inference.cli import main
from deedkit_inference.schemas import ChatRequest, TranscriptionForm
from pydantic import ValidationError
from support import CONTRACT, chat_body, fk, multipart

pytestmark = pytest.mark.anyio


def fixture(name):
    return json.loads((CONTRACT / name).read_text())


# spec: ANYM-682
async def test_routes_and_strict_schema(env):
    async with env.running() as s:
        health = await s.get("/health", key=None)
        assert health.status == 200
        assert (await s.get("/v1/models", key=None)).status == 401
        # Every route is under /v1; only /health is outside it.
        for path in ("/models", "/audio/transcriptions", "/chat/completions", "/v1/health", "/v1/completions",
                     "/docs", "/openapi.json"):
            assert (await s.get(path)).status == 404, path
        chat = await s.chat(chat_body(image=fk(label="x"), max_tokens=100))
        assert chat.status == 200
        reply = chat.json()
        assert reply["object"] == "chat.completion" and reply["model"] == "vision-default"
        assert json.loads(reply["choices"][0]["message"]["content"])["title"]
        audio = await s.transcribe()
        assert audio.status == 200 and audio.json() == {"text": "fake transcript"}
        verbose = await s.transcribe(extra_fields={"response_format": "verbose_json", "language": "en"})
        assert verbose.json()["segments"][0]["text"] == "fake transcript"
        # A strict schema: an unknown field is 422, everywhere.
        assert (await s.chat({**chat_body(image=fk()), "temperature": 0.1})).status == 422
        assert (await s.chat({**chat_body(image=fk()), "stream": True})).status == 422
        bad_message = chat_body(image=fk())
        bad_message["messages"][0]["name"] = "x"
        assert (await s.chat(bad_message)).status == 422
        assert (await s.transcribe(extra_fields={"temperature": "0"})).status == 422
        assert (await s.transcribe(extra_fields={"response_format": "srt"})).status == 422
        # No outbound image fetch: only base64 data URLs.
        remote = chat_body(image=None)
        remote["messages"][0]["content"].append({"type": "image_url", "image_url": {"url": "http://x.invalid/a.png"}})
        assert (await s.chat(remote)).status == 422
        # Wrong method on a route.
        assert (await s.get("/v1/chat/completions")).status == 405


# spec: ANYM-682
async def test_unknown_alias_is_refused(env):
    async with env.running() as s:
        for name in ("meta-llama/Llama-3-8B", "/models/vision", "../vision-default", "vision-default ", "", "nope"):
            reply = await s.chat(chat_body(model=name, image=fk()))
            assert reply.status == 404 and reply.code == "unknown_model", name
        # An alias of the other kind is as unknown as a missing one.
        assert (await s.chat(chat_body(model="audio-default", image=fk()))).code == "unknown_model"
        assert (await s.transcribe(model="vision-default")).code == "unknown_model"
        assert (await s.transcribe(model="mlx-community/whisper-large")).code == "unknown_model"
        assert fake_spawned() == 0


def fake_spawned():
    from deedkit_inference.engines import fake

    return len(fake.SPAWNED)


# spec: ANYM-682
@pytest.mark.parametrize("bind", ["0.0.0.0", "::", "192.168.1.5", "100.64.0.1", "localhost", "", "example.com"])
def test_non_loopback_bind_refuses_to_start(env, bind):
    with pytest.raises(ValidationError):
        env.config(bind=bind)
    for good in ("127.0.0.1", "127.0.0.2", "::1"):
        assert env.config(bind=good).bind == good
    assert env.config().port == 8090


# spec: ANYM-682
def test_non_loopback_bind_refuses_to_start_as_a_process(env):
    env.write_policy()
    config = env.config_dict(bind="0.0.0.0")
    path = env.tmp / "config.json"
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    run = subprocess.run(
        [sys.executable, "-m", "deedkit_inference", "serve", "--config", str(path)],
        env={**os.environ, "DEEDKIT_INFERENCE_TESTING": "1"}, capture_output=True, text=True, timeout=30)
    assert run.returncode == 2
    assert "refusing to start" in run.stderr and "bind_must_be_a_loopback_ip" in run.stderr
    with pytest.raises(SystemExit) as exit_info:
        main(["serve", "--config", str(path)])
    assert exit_info.value.code == 2


# spec: ANYM-682
async def test_contract_requests_validate_against_the_schema(env):
    names = ("chat_image", "chat_document")
    for name in names:
        request = fixture(f"{name}.request.json")
        parsed = ChatRequest.model_validate(request["json"])
        assert parsed.max_tokens == 2000 and request["path"] == "/v1/chat/completions"
        assert request["headers"]["X-Request-Deadline-Seconds"] == "120"
        assert "Authorization" not in request["headers"]
    for name in ("audio_transcription", "audio_transcription_verbose"):
        request = fixture(f"{name}.request.json")
        form = TranscriptionForm.model_validate({"model": request["multipart"]["fields"]["model"],
                                                 "response_format": request["multipart"]["fields"]["response_format"]})
        assert form.model == "audio-default" and request["path"] == "/v1/audio/transcriptions"
        assert base64.b64decode(request["multipart"]["file"]["base64"], validate=True)
    # The exact requests, posted to the service, give the golden answers.
    async with env.running() as s:
        for name in names:
            request, golden = fixture(f"{name}.request.json"), fixture(f"{name}.response.json")
            reply = await s.request("POST", request["path"], body=json.dumps(request["json"]).encode(),
                                    headers=request["headers"])
            assert reply.status == golden["status"]
            body = reply.json()
            body["id"], body["created"] = "chatcmpl-<request id>", 0
            assert body == golden["body"]
        for name in ("audio_transcription", "audio_transcription_verbose"):
            request, golden = fixture(f"{name}.request.json"), fixture(f"{name}.response.json")
            part = request["multipart"]
            body, content_type = multipart(part["fields"], base64.b64decode(part["file"]["base64"]),
                                           part["file"]["filename"])
            reply = await s.request("POST", request["path"], body=body,
                                    headers={**request["headers"], "content-type": content_type})
            assert reply.status == golden["status"] and reply.json() == golden["body"]
        errors = fixture("errors.golden.json")
        assert (await s.get("/v1/models", key="wrong")).json() == errors["unauthorized"]["body"]
        assert (await s.chat(chat_body(model="nope", image=fk()))).json() == errors["unknown_model"]["body"]
        assert (await s.chat(chat_body(image=fk(), max_tokens=16001))).json() == errors["invalid_request"]["body"]


# spec: ANYM-682
async def test_max_tokens_is_bounded_and_no_other_generation_field_is_accepted(env):
    async with env.running() as s:
        for value in (0, -1, 16001, 10**9, "10", 1.5, True):
            assert (await s.chat(chat_body(image=fk(), max_tokens=value))).status == 422, value
        for value in (1, 16000):
            assert (await s.chat(chat_body(image=fk(), max_tokens=value))).status == 200
        assert (await s.chat(chat_body(image=fk()))).status == 200  # absent: the alias default
        for field in ("temperature", "top_p", "top_k", "n", "stop", "stream", "seed", "logprobs", "presence_penalty",
                      "frequency_penalty", "max_completion_tokens", "response_format", "tools", "user"):
            body = chat_body(image=fk(), **{field: 1})
            reply = await s.chat(body)
            assert reply.status == 422 and reply.code == "invalid_request", field


# spec: ANYM-682
async def test_every_service_error_has_the_error_envelope_with_a_code(env, monkeypatch):
    async with env.running() as s:
        seen = {}

        def note(reply):
            body = reply.json()
            assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
            assert re.fullmatch(r"[a-z_]+", body["error"]["code"]) and body["error"]["message"]
            seen[reply.status] = body["error"]["code"]

        note(await s.get("/v1/models", key=None))  # 401
        note(await s.get("/v1/models", key="nope"))  # 401
        note(await s.get("/nowhere"))  # 404 route
        note(await s.get("/v1/chat/completions"))  # 405
        note(await s.chat(chat_body(model="nope", image=fk())))  # 404 alias
        note(await s.chat(chat_body(image=fk(), temperature=1)))  # 422
        note(await s.request("POST", "/v1/chat/completions", body=b"{}", content_length=None))  # 411
        note(await s.request("POST", "/v1/chat/completions", body=b"x" * 10, content_length=10**9))  # 413
        note(await s.request("POST", "/v1/chat/completions", body=b"{}", headers={"content-type": "text/plain"}))  # 415
        note(await s.request("POST", "/v1/chat/completions", body=b"not json",
                             headers={"content-type": "application/json"}))  # 422
        s.service.draining = True
        note(await s.chat(chat_body(image=fk())))  # 503 draining
        s.service.draining = False

        def boom(*args, **kwargs):
            raise RuntimeError("SENTINEL-boom")

        monkeypatch.setattr(s.service.scheduler, "admit", boom)
        crash = await s.chat(chat_body(image=fk()))
        note(crash)  # 500
        assert b"SENTINEL" not in crash.body
        assert {401, 404, 405, 411, 413, 415, 422, 500, 503} <= set(seen), seen
