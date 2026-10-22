"""The native group-operation deadline stays inside the command worker's timeout."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


# spec: ANYM-431
def test_native_group_deadline_below_worker_timeout():
    go = (ROOT/'whatsapp/whatsmeow/group_iq.go').read_text()
    deadline = re.search(r'^var groupOperationDeadline = (\d+) \* time\.Second$', go, re.M)
    worker = (ROOT/'services/messaging/messaging_service/commands.py').read_text()
    timeout = re.search(r"timeout=\d+ if job\['operation'\]=='send_file' else (\d+)\n", worker)
    assert deadline and timeout
    # Room for the last-moment preflight and IPC before the worker cancels the call.
    assert int(deadline.group(1)) + 10 <= int(timeout.group(1))


# spec: ANYM-441
def test_native_deadlines_below_ipc_and_worker():
    def go(path,name):
        found=re.search(rf'^var {name} = (\d+) \* time\.Second$',(ROOT/'whatsapp/whatsmeow'/path).read_text(),re.M)
        assert found,name
        return int(found.group(1))
    package=ROOT/'services/messaging/messaging_service'
    ipc=re.search(r"timeout=300 if method=='start' else (\d+),",(package/'whatsapp_collector.py').read_text())
    worker=re.search(r"timeout=(\d+) if job\['operation'\]=='send_file' else (\d+)\n",(package/'commands.py').read_text())
    send=re.search(r'await dispatch\(job,self\.send,(\d+),',(package/'outgoing.py').read_text())
    assert ipc and worker and send
    ipc,file_worker,worker,send=int(ipc.group(1)),int(worker.group(1)),int(worker.group(2)),int(send.group(1))
    # A late provider answer comes back as a mapped error before the caller gives up on the IPC reply.
    assert go('group_iq.go','groupOperationDeadline')+10<=min(ipc,worker)
    assert go('operations.go','fileOperationDeadline')+10<=min(ipc,file_worker)
    assert go('send.go','sendTextDeadline')+10<=min(ipc,send)
