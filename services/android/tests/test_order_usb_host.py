"""Host-owned USB forwarding and the ordinary native/order lifecycle, using synthetic transports."""

import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from agent_harness import Service
from deedkit_android.config import Refused
from test_native_dialogue import messages
from test_order_supervision import host, rows, until


class Listener:
    def __init__(self):
        self.socket = socket.socket()
        self.socket.bind(('127.0.0.1',0))
        self.socket.listen()
        self.socket.settimeout(.1)
        self.port = self.socket.getsockname()[1]
        self.watches, self.clients, self.closed = [], [], False
        self.observed = False
        self.thread = threading.Thread(target=self.serve,daemon=True)
        self.thread.start()

    def serve(self):
        while not self.closed:
            try:
                client,_ = self.socket.accept()
            except (socket.timeout,OSError):
                continue
            self.clients.append(client)
            stream = None
            try:
                stream = client.makefile('rwb')
                raw = stream.readline()
                if not raw:
                    continue
                watch = json.loads(raw)
                self.watches.append(watch)
                stream.write((json.dumps(dict(type='hello',version=1,source_epoch='epoch',oldest_sequence=1,
                    last_sequence=0,gap=False))+'\n').encode())
                if watch['packages'] and not self.observed:
                    self.observed = True
                    stream.write((json.dumps(dict(type='observation',source_epoch='epoch',sequence=1,
                        package='app.food',observed_at=time.time(),observation_ref='metadata-only'))+'\n').encode())
                stream.flush()
                stream.read()
            except (OSError,ValueError):
                pass
            finally:
                if stream is not None:
                    stream.close()
                client.close()

    def close(self):
        self.closed = True
        for client in self.clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        self.socket.close()
        self.thread.join(2)
        assert not self.thread.is_alive()


@pytest.fixture
def usb(tmp_path,monkeypatch):
    from deedkit_android import order_usb_host
    config,record = host(tmp_path,monkeypatch,delay=120)
    body = json.loads(config.read_text())
    body['devices']['home'] = 'serial-'+tmp_path.name
    config.write_text(json.dumps(body))
    binary = __import__('pathlib').Path(body['engines']['codex']['binary'])
    source = binary.read_text().replace('payload=dict(next_check_at=time.time()+',
        "payload=dict(package='app.food',next_check_at=time.time()+")
    binary.write_text(source)
    listener = Listener()
    calls,table = [],[]
    mode = [None]
    async def command(argv,timeout,**kwargs):
        calls.append(list(argv))
        serial = body['devices']['home']
        if argv[-1]=='--list':
            return 0,('\n'.join(' '.join(row) for row in table)+'\n').encode(),b''
        if '--remove' in argv:
            if mode[0]=='remove_lost':
                raise TimeoutError('synthetic lost remove')
            table.clear()
            return 0,b'',b''
        assert argv[1:3]==['-s',serial] and '--no-rebind' in argv
        table.append((serial,'tcp:'+str(listener.port),'localabstract:deedkit-order-events'))
        if mode[0]=='acquire_lost':
            raise TimeoutError('synthetic lost acquisition')
        if mode[0]=='revoke_after_acquire':
            changed=json.loads(config.read_text())
            changed['agent']['enabled']=False
            config.write_text(json.dumps(changed))
            await asyncio.sleep(0)
        return 0,str(listener.port).encode(),b''
    monkeypatch.setattr(order_usb_host,'run_process',command)
    yield SimpleNamespace(config=config,record=record,listener=listener,calls=calls,table=table,mode=mode,body=body)
    listener.close()


# spec: ANYM-993, ANYM-997
def test_usb_host_real_source_observation_reaches_original_native_turn_then_clears_owned_collection(usb):
    with Service(usb.config) as service:
        token = service.lease()
        request = service.start(token,goal='native-control',owner_instruction='Original active order')
        service.finished(request['request_id'])
        until(lambda: rows(service,"SELECT * FROM android_order_wakes WHERE kind='observation'"))
        until(lambda: rows(service,"SELECT * FROM android_order_native_inputs WHERE receipt LIKE '%accepted%'"))
        until(lambda: any(not w['packages'] for w in usb.listener.watches))
        until(lambda: not usb.table)
        wire = messages(usb.record)
        assert len({r['pid'] for r in wire})==1
        inputs=[r['message']['params']['input'][0]['text'] for r in wire if r['message'].get('method')=='turn/start']
        assert len(inputs)==2 and 'Original active order' in inputs[1]
        assert request['request_id'] in inputs[1]
        assert len([c for c in usb.calls if '--no-rebind' in c])==1
        assert len([c for c in usb.calls if '--remove' in c])==1
        receipt,=rows(service,'SELECT * FROM android_usb_forwards')
        assert receipt['state']=='closed' and receipt['collection']=='confirmed'
        view=service.get(request['request_id'])
        assert 'serial-' not in json.dumps(view['usb_source']) and token not in json.dumps(receipt)


# spec: ANYM-997
@pytest.mark.parametrize('failure',['acquire_lost','remove_lost'])
def test_usb_host_unknown_mutation_is_durable_and_never_reacquired_after_restart(usb,failure):
    usb.mode[0]=failure
    with Service(usb.config) as service:
        token=service.lease()
        request=service.start(token,goal='native-control')
        service.finished(request['request_id'])
        until(lambda: rows(service,"SELECT * FROM android_usb_forwards WHERE state='held'"))
        original,=rows(service,'SELECT * FROM android_usb_forwards')
        count=len([c for c in usb.calls if '--no-rebind' in c])
        assert count==1
    with Service(usb.config) as service:
        before=len(usb.calls)
        service.finished(service.start(token,goal='native-control')['request_id'])
        until(lambda: service.get(request['request_id'])['usb_source']['state']=='held')
        assert not any('--no-rebind' in c or '--remove' in c for c in usb.calls[before:])
        assert len([c for c in usb.calls if '--no-rebind' in c])==count
        recovered,=rows(service,'SELECT * FROM android_usb_forwards')
        assert recovered['id']==original['id'] and recovered['reason']==original['reason']


# spec: ANYM-997
def test_usb_host_never_adopts_or_removes_a_preexisting_foreign_forward(usb):
    usb.table.append((usb.body['devices']['home'],'tcp:12345','localabstract:deedkit-order-events'))
    with Service(usb.config) as service:
        request=service.start(service.lease(),goal='native-control')
        service.finished(request['request_id'])
        until(lambda: service.get(request['request_id'])['usb_source']['state']=='held')
        assert not any('--no-rebind' in c or '--remove' in c for c in usb.calls)
        assert usb.table[0][1]=='tcp:12345' and usb.listener.watches==[]


# spec: ANYM-997
def test_usb_host_same_principal_strings_cannot_replace_original_authority_generation(usb,monkeypatch):
    usb.listener.observed=True
    with Service(usb.config) as service:
        request=service.start(service.lease(),goal='native-control')
        service.finished(request['request_id'])
        until(lambda: usb.listener.watches)
        agents=service.app.state.agents
        entry=next(iter(agents.usb.entries.values()))
        async def replace_generation():
            handle=agents.supervision.handles.get(('one',request['request_id']))
            assert handle is not None
            entry.admit()
            agents.supervision.handles[('one',request['request_id'])]=SimpleNamespace(**handle.__dict__)
            with pytest.raises(Refused,match='supervision_unavailable'):
                entry.admit()
        asyncio.run_coroutine_threadsafe(replace_generation(),entry.session.reader.get_loop()).result(3)
        assert len([c for c in usb.calls if '--no-rebind' in c])==1


# spec: ANYM-997
def test_usb_host_release_clears_owned_collection_before_releasing_original_lease(usb):
    usb.listener.observed=True
    with Service(usb.config) as service:
        token=service.lease()
        request=service.start(token,goal='native-control')
        service.finished(request['request_id'])
        until(lambda: usb.listener.watches)
        result=service.post('session',dict(action='release',device_id='home',session_id=token))
        assert result=={'device_id':'home','released':True}
        receipt,=rows(service,'SELECT * FROM android_usb_forwards')
        assert receipt['state']=='closed' and receipt['collection']=='confirmed'
        assert not usb.listener.watches[-1]['packages'] and not usb.table
        assert not rows(service,'SELECT * FROM reservations')
        assert len([r for r in messages(usb.record) if r['message'].get('method')=='turn/start'])==1


# spec: ANYM-993, ANYM-997
def test_usb_host_post_acquisition_revocation_refuses_socket_and_watch_then_undoes_only_owned_forward(usb):
    usb.mode[0]='revoke_after_acquire'
    with Service(usb.config) as service:
        request=service.start(service.lease(),goal='native-control')
        service.finished(request['request_id'])
        until(lambda: rows(service,"SELECT * FROM android_usb_forwards WHERE state='closed'"))
        receipt,=rows(service,'SELECT * FROM android_usb_forwards')
        assert receipt['collection']=='not_started'
        assert usb.listener.watches==[] and not usb.table
        assert len([c for c in usb.calls if '--no-rebind' in c])==1
        assert len([c for c in usb.calls if '--remove' in c])==1
        assert len([r for r in messages(usb.record) if r['message'].get('method')=='turn/start'])==1
