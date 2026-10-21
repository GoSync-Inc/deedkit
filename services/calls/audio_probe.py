"""Bounded PCM transport diagnostics; retains counters and timings, never audio.

Energy thresholds approximate speech. Gaps are measured between the arrival of
the last active inbound packet and the first active outbound packet at the worker,
not between sounds at the recipient's ears. Noise/echo can affect the estimate.
"""
import math
import struct
import time
from datetime import datetime, timezone


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

    @staticmethod
    def _pcm(audio,rate=48000,channels=1):
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


class StartTiming:
    """Telegram call start (ANYM-450): three boundaries and inbound energy timing seen before the input gate.

    Offsets are milliseconds from peer_connected; active is PCMProbe's RMS threshold, so this is the
    timing of observed audio energy, not of speech. Counters only, never audio.
    """
    BOUNDARIES=('peer_connected','input_ready','call_active')

    def __init__(self,clock=time.monotonic):
        self.clock=clock
        self.at=dict.fromkeys(self.BOUNDARIES)
        self.peer_connected_at=None
        self.pre_ready_frames=0
        self.first_frame=self.first_active=None
        self.failed=False

    def mark(self,boundary):
        # A boundary that could not be read leaves later offsets without an origin: unavailable, not "no frame".
        if self.at[boundary] is None:
            try:
                self.at[boundary]=self.clock()
                if boundary=='peer_connected':self.peer_connected_at=datetime.now(timezone.utc).isoformat()
            except Exception:self.failed=True

    def frame(self,audio,ready):
        # A failed measurement is recorded as unavailable, never read as "no frame".
        if self.failed:return
        try:
            at=self.clock()
            if not ready:self.pre_ready_frames+=1
            duration,energy=PCMProbe._pcm(audio)
            if duration and self.first_frame is None:self.first_frame=at
            if energy>=400 and self.first_active is None:self.first_active=at
        except Exception:self.failed=True

    def snapshot(self):
        peer=self.at['peer_connected']
        since=lambda at:None if at is None or peer is None else round((at-peer)*1000,1)
        value={'peer_connected_at':self.peer_connected_at,'input_ready_ms':since(self.at['input_ready']),
               'call_active_ms':since(self.at['call_active']),'pre_ready_frames':self.pre_ready_frames}
        if self.failed:return {**value,'inbound_start':None,'inbound_start_unavailable':True}
        return {**value,'inbound_start':{'first_frame_ms':since(self.first_frame),'first_active_ms':since(self.first_active)}}
