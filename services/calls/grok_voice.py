"""Pinned Pipecat xAI realtime adapter. Only local task tools are registered."""
from pipecat.frames.frames import InputTextRawFrame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService
from pipecat.services.xai.realtime import events

from audio_health import ProviderAudioHealth


class AuditedGrok(GrokRealtimeLLMService):
    def __init__(self, brief, key, recorder=None, *, input_rate=16000, silent_start=False, file_resolver=None):
        self.recorder = recorder
        # A session resumed after an out-of-band key press (ANYM-451) takes its history without answering it.
        self.silent_start = silent_start
        self.audio_health = ProviderAudioHealth()
        # A legacy brief language outside ru/en/id gets no hint; a local failure building the hint
        # keeps the unhinted setup and is reported by run_transport (ANYM-450).
        transcription = events.InputAudioTranscription()
        self.hint_unavailable = False
        if brief.get('language') in ('ru', 'en', 'id'):
            try:
                transcription = events.InputAudioTranscription(language_hint=brief['language'])
            except Exception:
                self.hint_unavailable = True
        # xAI takes the frame bytes as they are, so the declared rate must equal the
        # transport's frames; undeclared, it assumes 24 kHz (ANYM-359).
        super().__init__(api_key=key, file_resolver=file_resolver, settings=self.Settings(
            model=brief['model'], system_instruction=brief['prompt'],
            session_properties=events.SessionProperties(
                voice=brief['grok_voice'],
                audio=events.AudioConfiguration(input=events.AudioInput(
                    format=events.PCMAudioFormat(rate=input_rate),
                    transcription=transcription)))))

    async def push_frame(self, frame, direction=FrameDirection.DOWNSTREAM):
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, TTSStartedFrame):
                self.audio_health.start()
            elif isinstance(frame, TTSStoppedFrame):
                self.audio_health.stop()
            elif isinstance(frame, TTSAudioRawFrame):
                self.audio_health.chunk(len(frame.audio), frame.sample_rate, frame.num_channels)
        await super().push_frame(frame, direction)

    async def _handle_evt_input_audio_transcription_completed(self, event):
        if self.recorder and event.transcript and event.transcript.strip():
            self.recorder('user', event.transcript)
        await super()._handle_evt_input_audio_transcription_completed(event)

    async def _handle_evt_audio_transcript_delta(self, event):
        if self.recorder and event.delta:
            self.recorder('assistant', event.delta)
        await super()._handle_evt_audio_transcript_delta(event)

    async def _create_response(self):
        if not self.silent_start or not self._api_session_ready or not self._llm_needs_conversation_setup:
            return await super()._create_response()
        # Pinned Pipecat 1.14 conversation setup, without response.create: wait for speech.
        self.silent_start = False
        for item in (await self.get_llm_adapter().get_llm_invocation_params(self._context))['messages']:
            event = events.ConversationItemCreateEvent(item=item)
            self._messages_added_manually[event.item.id] = True
            await self.send_client_event(event)
        await self._send_session_update()
        self._llm_needs_conversation_setup = False

    async def process_frame(self, frame, direction):
        if isinstance(frame, InputTextRawFrame):
            # Owner decisions are trusted server control input, never assistant
            # speech or a counterparty's claim of approval. No response.create:
            # preserve the current speaking turn; use it at the next model turn.
            await self.send_client_event(events.ConversationItemCreateEvent(
                item=events.ConversationItem(type='message', role='system',
                    content=[events.ItemContent(type='input_text', text=frame.text)])))
            return
        await super().process_frame(frame, direction)
