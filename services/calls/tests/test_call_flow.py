"""One browser rehearsal call through audio counters, guarded goodbye, transport release and the post-call archive.

Only the provider connection is replaced: the real `run_voice`, `run_transport`, `SpeechSafety`,
`GeminiLiveFlowManager`, `guarded_end` and `AuditedGemini` run over a real aiortc loopback peer.
"""
import asyncio
import json
import math
import time

import httpx
import numpy as np
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from google.genai.types import (Blob, Content, FunctionCall, LiveServerContent, LiveServerMessage,
                                LiveServerToolCall, Part, Transcription)
from pipecat.frames.frames import EndFrame
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService

import voice
from app import create_app
from core import ACTIVE
from learning import ANALYZER_VERSION, LearningWorker
from test_tasks import owner_headers, Archive, BRIEF

KEY = 'fixture-not-an-api-key'
GOODBYE = 'Спасибо, до свидания.'
ANSWER = 'Доставка с 12 до 13.'
FAREWELL_SECONDS = 1.0


async def until(check, timeout=10):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, 'condition not reached in time'
        await asyncio.sleep(.02)


class SpeakingTrack(AudioStreamTrack):
    """Silence by default; a 440 Hz tone while the synthetic human is speaking."""
    def __init__(self):
        super().__init__()
        self.until, self.sent = 0.0, 0

    def speak(self, seconds):
        self.until = time.monotonic() + seconds

    async def recv(self):
        frame = await super().recv()
        if time.monotonic() < self.until:
            n = np.arange(frame.samples) + self.sent
            tone = (np.sin(n * 2 * math.pi * 440 / frame.sample_rate) * 8000).astype(np.int16)
            frame.planes[0].update(tone.tobytes())
        self.sent += frame.samples
        return frame


class ScriptedSession:
    """Stands in for the Gemini Live session: records what the client sends, replays scripted frames."""
    def __init__(self):
        self.script = asyncio.Queue()
        self.audio_chunks, self.tool_responses = 0, {}
        self.tool_answered = asyncio.Event()

    async def send_client_content(self, **kwargs): pass

    async def send_realtime_input(self, **kwargs):
        self.audio_chunks += 'audio' in kwargs

    async def send_tool_response(self, function_responses):
        self.tool_responses[function_responses.id] = function_responses.response
        self.tool_answered.set()

    async def close(self): pass

    async def receive(self):
        while True:
            yield await self.script.get()


class ScriptedConnection:
    def __init__(self, session): self.session = session
    async def __aenter__(self): return self.session
    async def __aexit__(self, *exc): return False


class ScriptedClient:
    """Only `aio.live.connect` is used by the service; no network client exists."""
    def __init__(self, session):
        connection = ScriptedConnection(session)
        live = type('Live', (), {'connect': lambda self, **kwargs: connection})()
        self.aio = type('Aio', (), {'live': live})()


class ScriptedGemini(voice.AuditedGemini):
    """The real AuditedGemini (recorder, audio counters, flow tool path) over a scripted provider."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session = ScriptedSession()
        self._client = ScriptedClient(self.session)
        self.end_frame_at, self.scripted_audio_ms = None, 0.0

    async def process_frame(self, frame, direction):
        if isinstance(frame, EndFrame) and self.end_frame_at is None:
            self.end_frame_at = time.monotonic()
        await super().process_frame(frame, direction)

    def _put(self, **content):
        self.session.script.put_nowait(LiveServerMessage(server_content=LiveServerContent(**content)))

    def hear(self, text):
        self._put(input_transcription=Transcription(text=text))

    def say(self, text, seconds):
        rate, chunk = self._sample_rate, int(self._sample_rate / 10)
        tone = (np.sin(np.arange(int(rate * seconds)) * 2 * math.pi * 330 / rate) * 6000).astype(np.int16).tobytes()
        self._put(output_transcription=Transcription(text=text))
        for start in range(0, len(tone), chunk * 2):
            part = Part(inline_data=Blob(data=tone[start:start + chunk * 2], mime_type=f'audio/pcm;rate={rate}'))
            self._put(model_turn=Content(role='model', parts=[part]))
        self._put(turn_complete=True)
        self.scripted_audio_ms += seconds * 1000

    async def call(self, name, args, identity):
        self.session.tool_answered.clear()
        self.session.script.put_nowait(LiveServerMessage(tool_call=LiveServerToolCall(
            function_calls=[FunctionCall(id=identity, name=name, args=args)])))
        await until(lambda: identity in self.session.tool_responses)
        return self.session.tool_responses[identity]


class StubReviewer:
    def __init__(self): self.snapshots = []

    async def review(self, snapshot, key):
        assert key == KEY
        self.snapshots.append(snapshot)
        cited = [e['id'] for e in snapshot['events'] if e['kind'] == 'transcript_chunk' and e['role'] == 'user']
        return {'summary': 'Звонок закончился после прощания собеседника.', 'findings': [{
            'category': 'closing', 'severity': 'low', 'title': 'Корректное завершение',
            'explanation': 'Трубку положили после прощания и озвученного ответа.',
            'evidence_event_ids': cited[-1:], 'expected_behavior': 'Завершать только после нового прощания.'}],
            'candidate_instruction': ''}


# spec: ANYM-419
async def test_rehearsal_call_flows_from_measured_audio_through_guarded_goodbye_to_archive_and_analysis(monkeypatch, tmp_path):
    models = []

    def create_model(brief, key, recorder=None, input_rate=None):
        assert brief['dialog_engine'] == 'gemini_live_flows' and key == KEY
        llm = ScriptedGemini(api_key=key, recorder=recorder, live_flows=True, settings=GeminiLiveLLMService.Settings(
            model=brief['model'], system_instruction=brief['prompt'], voice=brief['voice']), transcription_language='ru-RU')
        models.append(llm)
        return llm
    monkeypatch.setattr(voice, 'create_model', create_model)
    reviewer = StubReviewer()
    app = create_app(tmp_path, Archive(), reviewer=reviewer)
    track = SpeakingTrack()
    peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    peer.addTrack(track)
    channel = peer.createDataChannel('rtvi-ai')
    ready, heard, readers = asyncio.Event(), [], []

    @channel.on('open')
    def opened():
        channel.send(json.dumps({'label': 'rtvi-ai', 'type': 'client-ready', 'id': 'fixture-ready', 'data': {'version': '1.0.0'}}))

    @channel.on('message')
    def message(value):
        if json.loads(value).get('type') == 'bot-ready': ready.set()

    @peer.on('track')
    def track_started(remote):
        async def read():
            while True:
                frame = await remote.recv()
                if np.max(np.abs(frame.to_ndarray().astype(np.int32))) > 100:
                    heard.append((time.monotonic(), frame.samples * 1000 / frame.sample_rate))
        readers.append(asyncio.create_task(read()))
    try:
        async with app.router.lifespan_context(app):
            # The post-call worker is driven by hand below; the app's own loop must not race it.
            app.state.learning_task.cancel()
            service = app.state.service
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://testserver',
                    headers={**owner_headers(tmp_path), 'Origin': 'http://testserver', 'X-Voice-Service': '1'}) as c:
                await c.post('/api/credentials', json={'gemini': KEY})
                brief = (await c.post('/api/briefs', json={**BRIEF, 'dialog_engine': 'gemini_live_flows'})).json()
                job = (await c.post('/api/calls/start', json={'brief_id': brief['id']})).json()
                identity = job['id']
                await peer.setLocalDescription(await peer.createOffer())
                payload = {'sdp': peer.localDescription.sdp, 'type': 'offer', 'requestData': {'call_id': identity}}
                response = await c.post('/api/offer', json=payload)
                assert response.status_code == 200, response.text
                answer = response.json()
                await peer.setRemoteDescription(RTCSessionDescription(sdp=answer['sdp'], type=answer['type']))
                await asyncio.wait_for(ready.wait(), 10)
                await until(lambda: models)
                llm = models[0]
                def events(kind):
                    return [e for e in service.get_call(identity)['events'] if e['kind'] == kind]

                # Audio: a tone, then silence, then a scripted answer that the peer actually receives.
                track.speak(.6)
                await until(lambda: [e for e in events('audio_activity') if e['speaking']])
                llm.hear(ANSWER)
                await until(lambda: [e for e in events('audio_activity') if not e['speaking']])
                llm.say('Здравствуйте. Уточните время доставки.', .5)
                await until(lambda: sum(ms for _, ms in heard) >= 300)
                saved = (await llm.call('record_answer', {'field': 'delivery_window', 'value': '12–13', 'evidence': ANSWER}, 'answer-1'))
                assert saved.get('evidence_found'), saved

                # Closing: no goodbye yet, so the end request is refused and nothing hangs up.
                refused = await llm.call('end_conversation', {'reason': 'completed', 'summary': 'Окно доставки 12–13',
                                                                'evidence': ANSWER}, 'end-early')
                assert refused.get('error') == 'closing_confirmation_required' and not refused.get('end_requested')
                assert service.get_call(identity)['status'] == 'active' and llm.end_frame_at is None

                # A fresh goodbye grants permission, but the hangup waits for the farewell to be played.
                # The answer turn commits at a bot-turn flush (deferred after response start, or at response end
                # after playback); this fixture keeps its two human turns separate by speaking only after that.
                await until(lambda: [e for e in events('transcript') if e['role'] == 'user'])
                track.speak(.5)
                await until(lambda: len([e for e in events('audio_activity') if e['speaking']]) >= 2)
                llm.hear(GOODBYE)
                await until(lambda: len([e for e in events('audio_activity') if not e['speaking']]) >= 2)
                granted = await llm.call('end_conversation', {'reason': 'completed', 'summary': 'Окно доставки 12–13',
                                                                'evidence': GOODBYE}, 'end-final')
                assert granted.get('end_requested') is True, granted
                assert llm.end_frame_at is None
                farewell_started, heard_before = time.monotonic(), len(heard)
                llm.say('Всего доброго.', FAREWELL_SECONDS)
                await until(lambda: service.get_call(identity)['status'] not in ACTIVE, 20)
                assert llm.end_frame_at is not None and llm.end_frame_at - farewell_started >= FAREWELL_SECONDS
                farewell_heard = sum(ms for _, ms in heard[heard_before:])
                assert farewell_heard >= FAREWELL_SECONDS * 1000 * .8, farewell_heard

                call = (await c.get(f'/api/calls/{identity}')).json()
                assert call['status'] == 'finished' and call['end_reason'] == 'task_completed'
                assert call['answers']['delivery_window']['value'] == '12–13'

                # Counters survive the guarded hangup and hold no audio.
                inbound, provider = events('inbound_audio'), events('provider_audio_health')
                assert len(inbound) == 1 and len(provider) == 1
                heard_in = inbound[0]['audio']
                assert heard_in['input_frames'] > 0 and heard_in['input_active_ms'] > 0 and heard_in['input_zero_frames'] > 0
                assert heard_in['audio_recorded'] is False and 'audio' not in json.dumps(heard_in).replace('audio_recorded', '')
                sent = provider[0]['audio']
                assert sent['chunks'] == round(llm.scripted_audio_ms / 100)
                assert sent['audio_ms'] == round(llm.scripted_audio_ms, 2) and sent['sample_rates'] == [llm._sample_rate]
                assert provider[0]['runtime']['delivery_frames'] > 0
                assert all(isinstance(v, (int, float, bool, str, type(None), list, dict)) for v in heard_in.values())
                assert set(sent) == {'chunks', 'audio_ms', 'sample_rates', 'generation_span_ms', 'max_chunk_gap_ms'}

                # Transport: the peer is released and the finished call cannot be joined again.
                await until(lambda: not app.state.session and not app.state.rtc._pcs_map, 5)
                again = await c.post('/api/offer', json=payload)
                assert again.status_code == 409 and again.json() == {'detail': 'call_already_joined'}

                # Learning: the archive is complete and one versioned analysis report is created once.
                archived = service.get_call(identity)['events']
                chunks = {role: ''.join(e['text'] for e in archived if e['kind'] == 'transcript_chunk' and e['role'] == role)
                          for role in ('user', 'assistant')}
                assert chunks['user'] == ANSWER + GOODBYE and 'Всего доброго.' in chunks['assistant']
                queued = service.learning.report(identity)
                assert queued['status'] == 'waiting_key' or queued['status'] == 'queued'
                assert [e['id'] for e in queued['snapshot']['events']] == [e['id'] for e in archived]
                assert queued['snapshot']['end_reason'] == 'task_completed'
                assert queued['coverage']['audio_recorded'] is False
                worker = LearningWorker(service, lambda: KEY, reviewer)
                assert await worker.run_once()
                report = service.learning.report(identity)
                assert report['status'] == 'complete' and report['id'] == queued['id']
                last_goodbye = [e['id'] for e in archived if e['kind'] == 'transcript_chunk' and e['role'] == 'user'][-1:]
                semantic = [f for f in report['findings'] if f['origin'] == 'semantic']
                assert len(semantic) == 1 and semantic[0]['evidence_event_ids'] == last_goodbye
                assert report['semantic']['rejected_findings'] == 0 and report['analyzer_version'] == ANALYZER_VERSION
                assert len(reviewer.snapshots) == 1
                assert not await worker.run_once()
                assert service.learning.report(identity)['id'] == report['id'] and len(reviewer.snapshots) == 1
                assert [r['id'] for r in service.learning.history(identity)] == [report['id']]
    finally:
        for reader in readers: reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await peer.close()
