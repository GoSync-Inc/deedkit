"""Existing-lease USB source admission and private, once-only host-forward receipts."""

import asyncio
import uuid

from .config import Refused
from .device import run_process
from .order_events_usb import SourceAccess, UsbForward, UsbOrderConnector

REMOTE = 'localabstract:deedkit-order-events'


class ForwardJournal:
    def __init__(self, agents):
        self.agents, self.db = agents, agents.db.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS android_usb_forwards
            (id TEXT PRIMARY KEY,owner TEXT,device TEXT,request_id TEXT,serial TEXT,source_id TEXT,
             auth_epoch INTEGER,port INTEGER,state TEXT,collection TEXT,reason TEXT)''')
        if 'recovery' not in {r['name'] for r in self.db.execute('PRAGMA table_info(android_usb_forwards)')}:
            self.db.execute('ALTER TABLE android_usb_forwards ADD COLUMN recovery TEXT')
        # Keep the interrupted phase: only a never-attempted removal of a proved forward may run later.
        self.db.execute("""UPDATE android_usb_forwards SET recovery='after_'||state,state='held',
            reason=COALESCE(reason,'restart_reconciliation_required') WHERE state IN ('sending','owned','closing')""")
        self.db.commit()  # never adopt a port/token/authority from these receipts

    @staticmethod
    def same(entry, row):
        return (row['owner'],row['device'],row['serial'],row['source_id'],row['auth_epoch'])==(
            entry.owner,entry.device,entry.serial,entry.source.source_id,entry.source.auth_epoch)

    @staticmethod
    def settled(row):
        """The host forward is positively gone; only the phone's persisted watch may remain unknown."""
        return row['state']=='closed' or row['reason']=='collection_cleanup_unconfirmed' or row['recovery']=='forward_closed'

    def blocked(self, entry, settled=False):
        """A row this exact owner/device/source/epoch generation can neither reconcile nor supersede."""
        for row in self.db.execute("SELECT * FROM android_usb_forwards WHERE serial=? AND state!='closed'",(entry.serial,)):
            if row['state']!='held' or not self.same(entry,row) or (settled and not self.settled(row)):
                return row
        return None

    def supersede(self, entry, ident):
        """A positive hello for this receipt's own watch replaced the phone's whole persisted set."""
        self.db.execute("""UPDATE android_usb_forwards SET state='closed',recovery='collection_superseded'
            WHERE serial=? AND owner=? AND device=? AND source_id=? AND auth_epoch=? AND state='held' AND id!=?
            AND (reason='collection_cleanup_unconfirmed' OR recovery='forward_closed')""",
            (entry.serial,entry.owner,entry.device,entry.source.source_id,entry.source.auth_epoch,ident))
        self.db.commit()

    async def reconcile(self, entry):
        """Read-only proof of absence, or the first removal of a proved owned forward; never a retry."""
        pending=[dict(r) for r in self.db.execute("SELECT * FROM android_usb_forwards WHERE serial=? AND state='held'",
                                                  (entry.serial,)) if self.same(entry,r) and not self.settled(r)]
        if not pending:
            return
        actual=await self.listing(entry)
        entry.admit()
        for row in pending:
            port,ident='tcp:'+str(row['port']),row['id']
            if row['port'] is None:
                if any(serial==entry.serial and remote==REMOTE for serial,_,remote in actual):
                    continue  # an unported acquisition may own any of these; never guess or remove
                outcome='forward_absent'
            elif (entry.serial,port,REMOTE) not in actual:
                outcome='forward_absent'
            elif row['recovery']!='after_owned' or [r for r in actual if r[1]==port]!=[(entry.serial,port,REMOTE)]:
                continue  # an attempted/unknown removal is never retried
            else:
                self.save(ident,recovery='removing')  # intent before the one removal attempt
                try:
                    code,_,_=await run_process([entry.adb,'-s',entry.serial,'forward','--remove',port],5,stdout_limit=64)
                    actual=await self.listing(entry)
                    if code or any(r[1]==port for r in actual):
                        raise Refused('usb_forward_cleanup_unknown')
                except BaseException:
                    self.save(ident,recovery='removal_unknown')
                    raise
                entry.admit()
                outcome='forward_removed'
            # Legacy restart rows did not record watch bytes, so their not_started is not proof.
            known=row['collection']=='confirmed' or row['collection']=='not_started' and (
                row['port'] is None or row['recovery'] is not None or row['reason']!='restart_reconciliation_required')
            self.save(ident,state='closed',recovery=outcome) if known else self.save(ident,recovery='forward_closed')

    def save(self, ident, **values):
        self.db.execute('UPDATE android_usb_forwards SET '+','.join(k+'=?' for k in values)+' WHERE id=?',
                        (*values.values(),ident))
        self.db.commit()

    async def listing(self, entry):
        code,out,_ = await run_process([entry.adb,'-s',entry.serial,'forward','--list'],5,stdout_limit=8192)
        if code:
            raise Refused('usb_forward_list_unconfirmed')
        result=[]
        for line in out.decode('ascii').splitlines():
            if not line.strip():
                continue
            parts=line.split()
            if len(parts)!=3:
                raise Refused('usb_forward_list_unconfirmed')
            result.append(tuple(parts))
        return result

    async def bind(self, entry, access):
        entry.admit()
        if access.serial!=entry.serial or self.blocked(entry):
            raise Refused('usb_forward_reconciliation_required')
        await self.reconcile(entry)
        if self.blocked(entry,settled=True):
            raise Refused('usb_forward_reconciliation_required')
        before=await self.listing(entry)
        entry.admit()
        if any(serial==entry.serial and remote==REMOTE for serial,_,remote in before):
            raise Refused('usb_forward_not_owned')
        ident=str(uuid.uuid4())
        self.db.execute('''INSERT INTO android_usb_forwards(id,owner,device,request_id,serial,source_id,auth_epoch,state,
            collection) VALUES(?,?,?,?,?,?,?,?,?)''',
            (ident,entry.owner,entry.device,entry.task,entry.serial,entry.source.source_id,entry.source.auth_epoch,
             'sending','not_started'))
        self.db.commit()  # original acquisition intent before any mutation bytes
        entry.receipt=ident
        try:
            code,out,_=await run_process([entry.adb,'-s',entry.serial,'forward','--no-rebind','tcp:0',REMOTE],5,stdout_limit=64)
            if code or not out.strip().isdigit() or not 1<=int(out.strip())<=65535:
                raise Refused('usb_forward_acquisition_unknown')
            port=int(out.strip())
            self.save(ident,port=port,state='owned')  # retain the positive receipt before another await/auth check
            actual=await self.listing(entry)
            if (entry.serial,'tcp:'+str(port),REMOTE) not in actual:
                raise Refused('usb_forward_acquisition_unknown')
        except BaseException:
            self.save(ident,state='held',reason='usb_forward_acquisition_unknown')
            raise
        async def close_owned():
            return await self.close(entry,ident,port)
        def collection(event):
            row=self.db.execute('SELECT state FROM android_usb_forwards WHERE id=?',(ident,)).fetchone()
            if row is not None and row['state']=='owned':
                if event=='watching':
                    self.save(ident,collection='unconfirmed')  # durable before the phone may persist a watch
                elif event=='accepted':
                    self.supersede(entry,ident)
        # B rechecks current admission before opening its socket or sending any watch.
        return UsbForward(entry.serial,port,close_owned,receipt=ident,collection=collection)

    async def close(self, entry, ident, port):
        row=self.db.execute('SELECT * FROM android_usb_forwards WHERE id=?',(ident,)).fetchone()
        if row is None or row['state']!='owned' or row['port']!=port or row['serial']!=entry.serial:
            return False  # no adoption/retry of a held or previously claimed cleanup
        collection='confirmed' if entry.connector.cleanup=='confirmed' else (
            'not_started' if entry.connector.state=='starting' and row['collection']=='not_started' else 'unconfirmed')
        self.save(ident,state='closing',collection=collection)
        try:
            actual=await self.listing(entry)
            same_port=[r for r in actual if r[1]=='tcp:'+str(port)]
            if same_port and same_port!=[(entry.serial,'tcp:'+str(port),REMOTE)]:
                raise Refused('usb_forward_not_owned')
            if same_port:
                code,_,_=await run_process([entry.adb,'-s',entry.serial,'forward','--remove','tcp:'+str(port)],5,stdout_limit=64)
                if code:
                    raise Refused('usb_forward_cleanup_unknown')
                actual=await self.listing(entry)
                if any(r[1]=='tcp:'+str(port) for r in actual):
                    raise Refused('usb_forward_cleanup_unknown')
            self.save(ident,state='closed' if collection in ('confirmed','not_started') else 'held',
                      reason=None if collection in ('confirmed','not_started') else 'collection_cleanup_unconfirmed')
            if collection=='confirmed':
                self.supersede(entry,ident)  # the empty watch's hello cleared the whole persisted set
            return True
        except BaseException:
            self.save(ident,state='held',reason='usb_forward_cleanup_unknown')
            return False


class Entry:
    def __init__(self, manager, origin, *, handle=False):
        self.manager,self.agents,self.origin=manager,manager.agents,origin
        self.handle=origin if handle else None
        self.owner,self.device,self.task=origin.owner,origin.device,origin.task if handle else origin.id
        self.request,self.authorize=origin.request,origin.authorize
        self.session,self.process=origin.session,origin.process
        self.thread=self.session.thread_id
        config=self.agents.read_config()
        self.source=config.agent.order_events
        self.serial,self.adb=config.devices[self.device],config.adb
        self.boundary=self.agents.supervision.boundary(self.session,self.owner,self.device)
        self.connector=self.close_task=None
        self.receipt=None
        self.closing=False

    def packages(self):
        return self.agents.db.db.execute('''SELECT 1 FROM android_order_obligations WHERE owner=?
            AND device_id=? AND source_id=? AND auth_epoch=? AND lifecycle='active' AND package IS NOT NULL
            AND task_only=0 LIMIT 1''',(self.owner,self.device,self.source.source_id,self.source.auth_epoch)).fetchone()

    def admit(self):
        if self.manager.entries.get((self.owner,self.device)) is not self:
            raise Refused('usb_source_generation_changed')
        config=self.agents.read_config()
        principal=self.authorize()
        if (principal.id!=self.owner or self.device not in principal.devices or 'android:control' not in principal.permissions
                or not config.agent.enabled or config.agent.guarded or not config.agent.persistent
                or config.agent.order_events!=self.source or config.devices.get(self.device)!=self.serial
                or config.adb!=self.adb):
            raise Refused('usb_source_access_changed')
        self.agents.store.check_reservation(self.owner,self.device,self.request.session_id)
        if (self.agents.sessions.get((self.owner,self.device)) is not self.session
                or self.session.process is not self.process or self.process is None or self.process.returncode is not None
                or self.session.thread_id!=self.thread or self.session.disconnected or not self.session.policy_proven
                or self.agents.supervision.boundary(self.session,self.owner,self.device)!=self.boundary):
            raise Refused('usb_source_conversation_changed')
        # Cleanup is an owned undo, never a renewed subscription/read authority. B cancels its
        # reader before close(); an all-empty package set also refuses every old nonempty watch.
        cleanup=self.connector is not None and (self.connector.closing or (
            self.connector.reason=='no_active_packages' and not self.packages()))
        if cleanup:
            return SourceAccess(principal,self.serial)
        if self.closing:
            raise Refused('usb_source_closing')
        if self.handle is None:
            current=self.agents.supervision.handles.get((self.owner,self.task))
            if (self.origin.state=='succeeded' and self.origin.native_completed_turn is not None
                    and self.origin.native_completed_turn==self.origin.native_admitted_turn and current is not None
                    and current.authorize is self.authorize and current.session is self.session
                    and current.process is self.process and current.request.session_id==self.request.session_id
                    and current.boundary==self.boundary):
                self.handle=current  # actual same-task successful retention, not DB reconstruction
            elif (self.origin.state=='succeeded' and current is None
                    and self.agents.runs.get((self.owner,self.task)) is self.origin
                    and self.origin.native_completed_turn
                    and self.origin.native_completed_turn==self.origin.native_admitted_turn
                    and self.agents.devices.locks[self.device].locked()):
                pass  # its own completed turn still holds the device until Supervision.finished: same generation
            elif (self.origin.state!='running' or self.agents.runs.get((self.owner,self.task)) is not self.origin
                    or self.session.request_id!=self.task or not self.origin.native_admitted_turn
                    or self.origin.native_admitted_turn!=self.session.turn_id
                    or not self.agents.devices.locks[self.device].locked()):
                raise Refused('usb_source_generation_changed')
        if self.handle is not None:
            self.agents.supervision.authority(self.handle)  # exact handle identity/current original key and budget
        return SourceAccess(principal,self.serial)

    def stop(self):
        self.closing=True
        if self.close_task is None:
            self.close_task=asyncio.create_task(self.connector.close())


class UsbLifecycle:
    def __init__(self, agents):
        self.agents=agents
        self.journal=ForwardJournal(agents)
        self.entries={}
        self.closing=False

    def refresh(self):
        if self.closing:
            return
        for key,entry in list(self.entries.items()):
            if entry.connector.task.done():
                continue  # one acquisition per generation, including terminal/unknown cleanup
            try:
                entry.admit()
                if not entry.packages():
                    entry.stop()
            except Refused:
                entry.stop()
        control=self.agents.order_control()
        if control is None:
            return
        candidates=[(r,False) for r in self.agents.runs.values() if r.state=='running' and getattr(r,'session',None)]
        candidates += [(h,True) for h in self.agents.supervision.handles.values()]
        for origin,handle in candidates[:32]:
            key,kept=(origin.owner,origin.device),None
            if key in self.entries:
                previous=self.entries[key]
                receipt=self.journal.db.execute('SELECT * FROM android_usb_forwards WHERE id=?',
                                                (previous.receipt,)).fetchone()
                # Retention can arrive after connector finish, before Entry.admit attached its handle.
                # That handle is still the original request, never authority for another acquisition.
                if not previous.connector.task.done():
                    continue
                # Ended before claiming the serial or calling bind: no receipt, forward or watch to settle.
                # Only a different request replaces it; the ended request itself is never admitted again.
                if previous.receipt is None and previous.connector.cleanup=='not_needed' and (
                        previous.close_task is None or previous.close_task.done()):
                    if (origin is previous.origin or origin is previous.handle
                            or (origin.task if handle else origin.id)==previous.task):
                        continue
                    kept=previous  # stays until a new request below is actually admitted in its place
                elif (receipt is None or not self.journal.settled(receipt)
                        or origin is previous.origin or origin is previous.handle
                        or handle and origin.task==previous.task):
                    continue
                else:
                    del self.entries[key]
            try:
                entry=Entry(self,origin,handle=handle)
                if not entry.packages() or self.journal.blocked(entry):
                    continue
                self.entries[key]=entry
                entry.admit()
                entry.connector=UsbOrderConnector(control.events,owner=entry.owner,device_id=entry.device,
                    source_id=entry.source.source_id,auth_epoch=entry.source.auth_epoch,admit=entry.admit,
                    bind_usb=lambda access,entry=entry:self.journal.bind(entry,access))
                entry.connector.start()
            except Refused:
                self.entries.pop(key,None)
                if kept is not None:
                    self.entries[key]=kept

    def stop(self, owner, task):
        for entry in self.entries.values():
            if entry.owner==owner and entry.task==task:
                entry.stop()

    async def release(self, owner, device, token):
        entries=[e for e in self.entries.values() if e.owner==owner and e.device==device and e.request.session_id==token]
        for entry in entries:
            entry.stop()
        await asyncio.gather(*(e.close_task for e in entries))

    async def close(self):
        self.stop_all()
        await asyncio.gather(*(e.close_task for e in self.entries.values()))

    def stop_all(self):
        self.closing=True
        for entry in self.entries.values():
            entry.stop()

    def status(self, owner, device):
        entry=self.entries.get((owner,device))
        if entry is not None:
            return entry.connector.status()
        row=self.journal.db.execute("SELECT reason FROM android_usb_forwards WHERE owner=? AND device=? AND state='held' LIMIT 1",
                                    (owner,device)).fetchone()
        return {'state':'held' if row else 'unavailable','reason':row['reason'] if row else 'no_live_authority'}
