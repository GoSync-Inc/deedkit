"""Automatic personal PLAUD text capture: pinned synthetic remote MCP only."""
import asyncio
import base64
import json
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2] / 'services/recordings/src'))
from test_recording_archive_ingest import fixture, save
from deedkit_recordings.archive import ArchiveRuntime
from deedkit_recordings.omi import Provider
from deedkit_recordings.plaud import Provider as PlaudProvider


class Remote:
    def __init__(self, *, mode=None, sse=False):
        self.mode, self.sse, self.calls = mode, sse, []
        self.observations = 0
        self.after_page = None

    async def __call__(self, request):
        assert str(request.url) == 'https://mcp.plaud.ai/mcp'
        assert request.headers['Authorization'] == 'Bearer omi_synthetic'
        if request.method == 'DELETE': return httpx.Response(204)
        body = json.loads(request.content)
        if body['method'] == 'notifications/initialized': return httpx.Response(202)
        headers = {}
        if body['method'] == 'initialize':
            value = {'protocolVersion':'2025-06-18','capabilities':{'tools':{}},'serverInfo':{'name':'synthetic','version':'0.3.14'}}
            headers = {'Mcp-Session-Id':'synthetic-session'}
        else:
            name, args = body['params']['name'], body['params']['arguments']
            self.calls.append((name,args))
            assert name in ('list_files','get_transcript','get_note')
            if name == 'list_files':
                value = {'data':[{'id':'file-a','name':'Synthetic meeting'}] if args['page'] == 1 else []}
            elif name == 'get_note':
                if self.mode == 'notes_error': return httpx.Response(503)
                value = [{'data_type':'summary','data_content':'A vendor summary about telescopes',
                          'data_link':'https://invalid.test/never-follow','private_field':'drop'}]
            else:
                offset = json.loads(base64.urlsafe_b64decode(args['cursor'] + '=' * (-len(args['cursor']) % 4)))['offset'] if args.get('cursor') else 0
                if offset == 0: self.observations += 1
                if self.mode == 'timeout': await asyncio.Event().wait()
                segments = [{'text':f'Synthetic telescope segment {n}', 'speaker':'SPEAKER_0','start':n,'end':n+1,
                             'download_url':'https://invalid.test/private'} for n in range(offset,min(offset+500,600))]
                if self.mode == 'unicode': segments[0]['text'] += '\u2028a valid Unicode separator'
                next_cursor = base64.urlsafe_b64encode(json.dumps({'offset':500}).encode()).decode().rstrip('=') if offset == 0 else None
                value = {'file_id':'file-a','block':'transaction','total':600,'offset':offset,'returned':len(segments),
                         'limit':500,'segments':segments,'next_cursor':next_cursor}
                if self.mode == 'changed' and self.observations == 2 and offset == 500: value['segments'][20]['text'] = 'mutated middle'
                if self.mode == 'offset' and offset: value['offset'] += 1
                if self.mode == 'total' and offset: value['total'] += 1
                if self.mode == 'loop' and offset: value['next_cursor'] = args['cursor']
                if self.mode == 'limit': value['total'] = 10001
                if self.mode == 'oversize': value['segments'] = [{'text':'z' * 2097152}]; value['returned'] = 1
                if self.after_page: self.after_page()
            value = {'structuredContent': value}
        response = {'jsonrpc':'2.0','id':body['id'],'result':value}
        if self.sse:
            return httpx.Response(200,text='event: message\ndata: '+json.dumps(response)+'\n\n',
                                 headers={**headers,'Content-Type':'text/event-stream'})
        return httpx.Response(200,json=response,headers=headers)


def configured(tmp_path, remote, *, timeout=12):
    path, doc = fixture(tmp_path)
    doc['accounts'][0]['provider'] = 'plaud'
    save(path,doc)
    transport = httpx.MockTransport(remote)
    budget = Provider(transport,timeout)
    r = ArchiveRuntime(path,budget,PlaudProvider(budget,transport,timeout))
    return r,path,doc


# spec: ANYM-654
@pytest.mark.parametrize('sse',[False,True])
def test_long_transcript_uses_two_complete_observations_and_survives_restart(tmp_path,sse):
    remote = Remote(sse=sse); r,path,doc = configured(tmp_path,remote)
    async def run():
        await r.start(background=False); await r.run_once(now=100)
        row = await r.db('record',doc['accounts'][0],'file-a')
        assert row['parts']['transcript']['state'] == 'available'
        lines = row['parts']['transcript']['text'].splitlines()
        assert len(lines) == 600 and json.loads(lines[-1])['text'].endswith('599')
        assert row['parts']['transcript']['consistency'] == 'observed_equal_reads_not_atomic_snapshot'
        assert 'download_url' not in row['parts']['transcript']['text']
        assert 'data_link' not in row['parts']['summary']['text']
        assert len([c for c in remote.calls if c[0]=='get_transcript']) == 4
        before = await r.db('events',0,100)
        assert before
        ref = row['ref']; await r.close()
        transport = httpx.MockTransport(remote); budget = Provider(transport)
        again = ArchiveRuntime(path,budget,PlaudProvider(budget,transport))
        await again.start(background=False); await again.run_once(now=4000)
        after = await again.db('record',doc['accounts'][0],'file-a')
        assert after['ref'] == ref and after['revision'] == row['revision']
        assert await again.db('events',0,100) == before
        assert (await again.db('status',doc['accounts'][0]))['coverage'] == 'unknown'
        await again.close()
    asyncio.run(run())


# spec: ANYM-654
@pytest.mark.parametrize('mode',['changed','offset','total','loop','limit','oversize','timeout'])
def test_rejected_transcript_never_replaces_previous_text_and_does_not_block_notes(tmp_path,mode):
    remote = Remote(); r,_,doc = configured(tmp_path,remote,timeout=0.03 if mode=='timeout' else 12)
    async def run():
        await r.start(background=False); await r.run_once(now=100)
        old = await r.db('record',doc['accounts'][0],'file-a')
        remote.mode = mode; remote.observations = 0
        await r.run_once(now=4000)
        row = await r.db('record',doc['accounts'][0],'file-a')
        assert row['parts']['transcript']['attempt_state'] == 'error'
        assert row['parts']['transcript']['attempt_error']
        assert row['parts']['transcript']['digest'] == old['parts']['transcript']['digest']
        assert row['parts']['summary']['state'] == 'available'
        assert row['parts']['summary']['attempt_state'] == 'available'
        assert await r.db('events',0,100)  # accepted notes remain independently usable
        await r.close()
    asyncio.run(run())


# spec: ANYM-654
def test_summary_failure_keeps_long_transcript_available_and_attempt_states_separate(tmp_path):
    remote = Remote(mode='notes_error'); r,_,doc = configured(tmp_path,remote)
    async def run():
        await r.start(background=False); await r.run_once(now=100)
        row = await r.db('record',doc['accounts'][0],'file-a')
        assert row['parts']['transcript']['state'] == 'available'
        assert row['parts']['summary']['attempt_state'] == 'error'
        assert (await r.db('status',doc['accounts'][0]))['retry_count'] == 1
        await r.close()
    asyncio.run(run())


# spec: ANYM-654
def test_namespace_revocation_mid_page_discards_transcript_and_stops_further_egress(tmp_path):
    remote = Remote(); r,path,doc = configured(tmp_path,remote)
    async def run():
        await r.start(background=False)
        old_account = r.document().accounts[0]
        def revoke():
            doc['accounts'][0]['archive_namespace'] = 'b'*32
            save(path,doc)
        remote.after_page = revoke
        await r.run_once(now=100)
        row = await r.db('record',old_account,'file-a')
        assert row is not None and row['parts'].get('transcript',{}).get('state') != 'available'
        assert len([c for c in remote.calls if c[0]=='get_transcript']) == 1
        await r.close()
    asyncio.run(run())



# spec: ANYM-654
def test_unicode_separator_inside_utterance_is_not_a_segment_boundary(tmp_path):
    remote = Remote(mode='unicode'); r,_,doc = configured(tmp_path,remote)
    async def run():
        await r.start(background=False); await r.run_once(now=100)
        row = await r.db('record',doc['accounts'][0],'file-a')
        lines = row['parts']['transcript']['text'].split('\n')
        assert len(lines)==600 and '\u2028' in json.loads(lines[0])['text']
        await r.close()
    asyncio.run(run())
