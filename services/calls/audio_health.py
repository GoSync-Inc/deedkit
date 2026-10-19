"""Bounded in-memory source audio counters, without retaining PCM or conversation text."""
import time
import asyncio


class ProviderAudioHealth:
    def __init__(self):
        self.chunks=0
        self.audio_ms=0.0
        self.sample_rates=set()
        self.first=None
        self.last=None
        self.generation_span_ms=0.0
        self.max_chunk_gap_ms=0.0

    def start(self):
        self.stop()

    def stop(self):
        if self.first is not None:
            self.generation_span_ms+=(self.last-self.first)*1000
        self.first=self.last=None

    def chunk(self,size,rate,channels,at=None):
        if rate<=0 or channels<=0:
            return
        at=time.monotonic() if at is None else at
        self.chunks+=1
        self.audio_ms+=size*500/(rate*channels)  # signed PCM16
        self.sample_rates.add(rate)
        if self.last is not None:
            self.max_chunk_gap_ms=max(self.max_chunk_gap_ms,(at-self.last)*1000)
        if self.first is None:
            self.first=at
        self.last=at

    def snapshot(self):
        span=self.generation_span_ms+((self.last-self.first)*1000 if self.first is not None else 0)
        return {'chunks':self.chunks,'audio_ms':round(self.audio_ms,2),'sample_rates':sorted(self.sample_rates),
            'generation_span_ms':round(span,2),'max_chunk_gap_ms':round(self.max_chunk_gap_ms,2)}


class RuntimeAudioHealth:
    """Observe scheduling and completed output writes; these are not RTP timestamps."""
    def __init__(self):
        self.origin=None
        self.last_tick=None
        self.last_audio=None
        self.audio_duration=0.0
        self.event_loop_stalls=0
        self.max_event_loop_lag_ms=0.0
        self.delivery_frames=0
        self.delivery_late_events=0
        self.max_delivery_lateness_ms=0.0
        self.late_observations=[]

    def _late(self,kind,at,late_ms):
        if self.origin is None:self.origin=at
        if late_ms>80 and len(self.late_observations)<12:
            self.late_observations.append({'kind':kind,'offset_ms':round((at-self.origin)*1000,2),
                                           'late_ms':round(late_ms,2)})

    def tick(self,at=None):
        at=time.monotonic() if at is None else at
        if self.origin is None:self.origin=at
        if self.last_tick is not None:
            late=max(0.0,(at-self.last_tick-.02)*1000)
            self.max_event_loop_lag_ms=max(self.max_event_loop_lag_ms,late)
            self.event_loop_stalls+=late>80
            self._late('event_loop',at,late)
        self.last_tick=at

    async def monitor(self):
        while True:
            self.tick()
            await asyncio.sleep(.02)

    def audio(self,size,rate,channels,at=None):
        if rate<=0 or channels<=0:return
        at=time.monotonic() if at is None else at
        if self.origin is None:self.origin=at
        if self.last_audio is not None:
            late=max(0.0,(at-self.last_audio-self.audio_duration)*1000)
            self.max_delivery_lateness_ms=max(self.max_delivery_lateness_ms,late)
            self.delivery_late_events+=late>80
            self._late('output_write',at,late)
        self.delivery_frames+=1
        self.audio_duration=size/(2*rate*channels)
        self.last_audio=at

    def stop_audio(self):
        self.last_audio=None

    def snapshot(self):
        return {'event_loop_stalls':self.event_loop_stalls,
                'max_event_loop_lag_ms':round(self.max_event_loop_lag_ms,2),
                'delivery_frames':self.delivery_frames,'delivery_late_events':self.delivery_late_events,
                'max_delivery_lateness_ms':round(self.max_delivery_lateness_ms,2),
                'late_observations':[dict(e) for e in self.late_observations]}


class NoRuntimeAudioHealth:
    # Stands in when the RuntimeAudioHealth constructor fails: nothing is measured and the snapshot is empty.
    async def monitor(self):await asyncio.Event().wait()
    def tick(self,*args,**kwargs):pass
    def audio(self,*args,**kwargs):pass
    def stop_audio(self):pass
    def snapshot(self):return {}
