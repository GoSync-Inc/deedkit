import importlib

from fastapi.testclient import TestClient
from test_tasks import owner_headers, app, prepared, started, HEADERS


def test_audio_health_result_accepts_bounded_counters_and_rejects_unrelated_data(tmp_path):
    with TestClient(app(tmp_path), headers=owner_headers(tmp_path)) as c:
        job=started(c,prepared(c))
        path=f'/api/calls/{job["id"]}/result'
        metrics={'audio_health':{'availability':'supported','samples':2,'packets_received':100,
            'packets_lost':0,'concealed_samples':480,'total_samples_received':48000,'concealed_percent':1}}
        r=c.post(path,json={'metrics':metrics},headers=HEADERS)
        assert r.status_code==200,r.text
        assert r.json()['metrics']['audio_health']['concealed_samples']==480
        for invalid in ({'address':'127.0.0.1'},{'packets_lost':-1},{'jitter_ms':1000000}):
            r=c.post(path,json={'metrics':{'audio_health':invalid}},headers=HEADERS)
            assert r.status_code==422


def test_provider_probe_counts_pcm_duration_and_excludes_pauses_between_turns():
    probe=importlib.import_module('audio_health').ProviderAudioHealth()
    probe.start()
    probe.chunk(4800,24000,1,at=1)
    probe.chunk(4800,24000,1,at=1.05)
    probe.stop()
    probe.start()
    probe.chunk(4800,24000,1,at=10)
    result=probe.snapshot()
    assert result['audio_ms']==300
    assert result['chunks']==3
    assert result['sample_rates']==[24000]
    assert round(result['max_chunk_gap_ms'])==50
    assert round(result['generation_span_ms'])==50


def test_runtime_probe_distinguishes_server_stall_from_conversation_pause():
    probe=importlib.import_module('audio_health').RuntimeAudioHealth()
    probe.tick(1)
    probe.tick(1.02)
    probe.tick(1.22)
    probe.audio(960,24000,1,at=1)
    probe.audio(960,24000,1,at=1.02)
    probe.audio(960,24000,1,at=1.22)
    probe.stop_audio()
    probe.audio(960,24000,1,at=10)
    result=probe.snapshot()
    assert result['event_loop_stalls']==1
    assert round(result['max_event_loop_lag_ms'])==180
    assert round(result['max_delivery_lateness_ms'])==180
    assert result['delivery_frames']==4
    assert result['delivery_late_events']==1


def test_runtime_probe_keeps_a_bounded_trace_without_audio_content():
    probe=importlib.import_module('audio_health').RuntimeAudioHealth()
    for i in range(100):
        probe.tick(float(i))
        probe.audio(960,24000,1,at=float(i))
    value=probe.snapshot()
    assert len(value['late_observations'])<=12
    assert all(set(e)=={'kind','offset_ms','late_ms'} for e in value['late_observations'])


# spec: ANYM-409
async def test_the_runtime_health_stand_in_is_a_no_op_with_a_cancellable_coroutine_monitor():
    import asyncio
    stand_in=importlib.import_module('audio_health').NoRuntimeAudioHealth()
    stand_in.tick();stand_in.tick(1.5);stand_in.audio(960,24000,1);stand_in.audio(960,24000,1,at=2.0);stand_in.stop_audio()
    assert stand_in.snapshot()=={}
    before=asyncio.all_tasks()
    task=asyncio.create_task(stand_in.monitor())
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.gather(task,return_exceptions=True)
    assert task.cancelled() and asyncio.all_tasks()==before
