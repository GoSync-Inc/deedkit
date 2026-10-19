"""Bounded PCM transport diagnostics; retains counters and timings, never audio.

Energy thresholds approximate speech. Gaps are measured between the arrival of
the last active inbound packet and the first active outbound packet at the worker,
not between sounds at the recipient's ears. Noise/echo can affect the estimate.
"""
import math
import struct
import time


class NoDiagnostics:
    # Stands in when a diagnostics constructor fails: every method is a no-op and every snapshot is empty.
    def __getattr__(self,name): return lambda *args,**kwargs:{}


def diagnostics(factory):
    # Diagnostics are best effort: a constructor failure must never break call setup.
    try: return factory()
    except Exception: return NoDiagnostics()


class PCMProbe:
    def __init__(self,max_samples=100):
        self.max_samples=max_samples
        self.input_ms=self.output_ms=self.active_ms=0.0
        self.pending_ms=0.0
        self.input_frames=self.zero_frames=0
        self.max_rms=0.0
        self.first_input=self.first_active=None
        self.input_rates=set()
        self.last_input=self.last_output=None
        self.samples=[]
        self.samples_total=self.overlaps=0

    def _pcm(self,audio,rate=48000,channels=1):
        if not audio or len(audio)%2 or rate<=0 or channels<=0:return 0,0
        samples=struct.unpack('<'+'h'*(len(audio)//2),audio)
        return len(samples)*1000/(rate*channels),math.sqrt(sum(value*value for value in samples)/len(samples))

    def input(self,audio,at=None,rate=48000,channels=1):
        at=time.monotonic() if at is None else at
        duration,energy=self._pcm(audio,rate,channels)
        self.input_ms+=duration
        if duration:
            # All-zero frames are digital silence; a quiet microphone still has nonzero samples.
            self.input_frames+=1
            self.zero_frames+=energy==0
            self.max_rms=max(self.max_rms,energy)
            if len(self.input_rates)<4:self.input_rates.add(rate)
            if self.first_input is None:self.first_input=at
        if energy>=400:
            if self.first_active is None:self.first_active=(at-self.first_input)*1000
            self.active_ms+=duration
            self.pending_ms+=duration
            self.last_input=at

    def output(self,audio,at=None):
        at=time.monotonic() if at is None else at
        duration,energy=self._pcm(audio)
        self.output_ms+=duration
        if energy<100:return
        onset=self.last_output is None or at-self.last_output>.15
        self.last_output=at
        if not onset:return
        if self.pending_ms>=80 and self.last_input is not None:
            gap=at-self.last_input
            if gap>=.12:
                self.samples_total+=1
                if len(self.samples)<self.max_samples:self.samples.append(round(gap*1000,1))
            else:self.overlaps+=1
        self.pending_ms=0

    def snapshot(self):
        return {'measurement':'worker_pcm_energy_gap','sample_rate':(None if len(self.input_rates)>1 else next(iter(self.input_rates),48000)),
                'input_pcm_ms':round(self.input_ms,1),'output_pcm_ms':round(self.output_ms,1),
                'input_active_ms':round(self.active_ms,1),'input_frames':self.input_frames,
                'input_zero_frames':self.zero_frames,'input_max_rms':round(self.max_rms,1),
                'first_active_input_ms':None if self.first_active is None else round(self.first_active,1),
                'input_sample_rates':sorted(self.input_rates),'response_gap_ms':list(self.samples),
                'response_samples_total':self.samples_total,'overlapping_onsets':self.overlaps,
                'recipient_playback_verified':False,'audio_recorded':False}
