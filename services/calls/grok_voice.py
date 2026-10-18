"""Pinned Pipecat xAI realtime adapter. Only local task tools are registered."""
from pipecat.frames.frames import InputTextRawFrame, TTSAudioRawFrame, TTSStartedFrame, TTSStoppedFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.xai.realtime.llm import GrokRealtimeLLMService
from pipecat.services.xai.realtime import events

from audio_health import ProviderAudioHealth


class AuditedGrok(GrokRealtimeLLMService):
    def __init__(self, brief, key, recorder=None, *, input_rate=16000):
        self.recorder = recorder
        self.audio_health = ProviderAudioHealth()
        # xAI takes the frame bytes as they are, so the declared rate must equal the
        # transport's frames; undeclared, it assumes 24 kHz (ANYM-359).
        super().__init__(api_key=key, settings=self.Settings(
            model=brief['model'], system_instruction=brief['prompt'],
            session_properties=events.SessionProperties(
                voice=brief['grok_voice'],
                audio=events.AudioConfiguration(input=events.AudioInput(
                    format=events.PCMAudioFormat(rate=input_rate),
                    transcription=events.InputAudioTranscription(
                        language_hint=brief['language']))))))

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
