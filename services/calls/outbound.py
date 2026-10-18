"""One durable outbound attempt per frozen brief. Provider media never receives archive credentials."""
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import re
import secrets
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from core import ACTIVE, BriefInput, ServiceError, now
import destination_policy as policy

E164=re.compile(r'\+[1-9][0-9]{7,14}')
RAISE_FIELDS=('id','created_at','expires_at','principal','owner_instruction','extra_units')
SIP=re.compile(r'sips?:[A-Za-z0-9_.+-]{1,80}@[A-Za-z0-9.-]{1,200}(?::[0-9]{1,5})?(?:;transport=(?:tcp|tls|udp))?(?:;secure=true)?')


class ExternalBrief(BriefInput):
    channel: str = Field(pattern=r'^(telegram|whatsapp|phone|sip)$')
    account: str = Field(pattern=r'^(telegram|twilio)$')
    destination: str = Field(min_length=1,max_length=300)

    @model_validator(mode='after')
    def route(self):
        try: policy.unsupported(self.channel,self.destination)
        except ServiceError as error: raise ValueError(error.code+': '+error.detail['message']) from None
        if self.channel=='telegram':
            valid=self.account=='telegram' and bool(re.fullmatch(r'[1-9][0-9]{0,17}',self.destination))
        else:
            valid=self.account=='twilio' and bool((SIP if self.channel=='sip' else E164).fullmatch(self.destination)) and (self.channel!='sip' or len(self.destination)<255)
        if not valid:
            raise ValueError('invalid_call_destination_or_account')
        return self


class CallingSettings(BaseModel):
    """Operator switch. `allowed` restricts SIP only; other channels have no recipient list (ANYM-288),
    so their keys may still be stored from older settings but are not read."""
    model_config=ConfigDict(extra='forbid')
    enabled: bool=False
    allowed: dict[str,list[str]]=Field(default_factory=dict)

    @model_validator(mode='after')
    def recipients(self):
        if set(self.allowed)-{'telegram','whatsapp','phone','sip'} or sum(map(len,self.allowed.values()))>50:
            raise ValueError('invalid_allowed_recipients')
        for channel,values in self.allowed.items():
            for value in values:
                if not isinstance(value,str) or len(value)>300:
                    raise ValueError('invalid_allowed_recipient')
                valid=re.fullmatch(r'[1-9][0-9]{0,17}',value) if channel=='telegram' else (SIP if channel=='sip' else E164).fullmatch(value)
                if not valid or (channel=='sip' and len(value)>=255): raise ValueError('invalid_allowed_recipient')
        return self


class TwilioCalls:
    def __init__(self,config,http=None):
        self.config=config
        self.http=http or httpx.AsyncClient(timeout=20,trust_env=False,follow_redirects=False)
        self.owned=http is None

    def _auth(self):
        return (self.config['account_sid'],self.config['auth_token'])

    def _root(self):
        return 'https://api.twilio.com/2010-04-01/Accounts/'+self.config['account_sid']

    async def preflight(self,b):
        return self.configuration(b)

    def configuration(self,b):
        """Local validation only; never creates a provider connection."""
        cfg=self.config
        if not re.fullmatch(r'AC[0-9a-fA-F]{32}',cfg.get('account_sid','')) or not cfg.get('auth_token'):
            raise ServiceError('twilio_credentials_required')
        u=urlsplit(cfg.get('public_url',''))
        if u.scheme!='https' or not u.hostname or u.username or u.query or u.fragment or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*/?',u.path):
            raise ServiceError('public_https_url_required')
        number=cfg.get('sip_from','assistant') if b['channel']=='sip' else cfg.get('whatsapp_from' if b['channel']=='whatsapp' else 'phone_from','')
        if not (re.fullmatch(r'[A-Za-z0-9+_.-]{1,80}',number) if b['channel']=='sip' else E164.fullmatch(number)):
            raise ServiceError('twilio_sender_required')
        if b['channel']=='sip' and bool(cfg.get('sip_auth_username'))!=bool(cfg.get('sip_auth_password')):
            raise ServiceError('sip_auth_pair_required')
        return {'configured':True,'provider':'twilio','sender':number,'destination':b['destination'],
                'channel':b['channel'],'recipient_permission':'provider_enforced' if b['channel']=='whatsapp' else 'not_applicable'}

    async def probe(self):
        if not self.config.get('account_sid') or not self.config.get('auth_token'):
            return {'connected':False,'error':'twilio_credentials_required'}
        try:
            r=await self.http.get(self._root()+'.json',auth=self._auth())
            return {'connected':r.status_code==200,'account_status':r.json().get('status') if r.status_code==200 else None}
        except Exception:
            return {'connected':False,'error':'twilio_unavailable'}

    async def dial(self,call):
        b=call['brief']; await self.preflight(b)
        base=self.config['public_url'].rstrip('/')
        ws=base.replace('https://','wss://',1)+'/provider/twilio/media/'+call['id']
        twiml='<Response><Connect><Stream url="'+escape(ws,{'"':'&quot;'})+'"><Parameter name="attempt_token" value="'+escape(call['stream_token'],{'"':'&quot;'})+'"/></Stream></Connect><Hangup/></Response>'
        wa=b['channel']=='whatsapp'
        data={'To':('whatsapp:' if wa else '')+b['destination'],
              'From':self.config.get('sip_from','assistant') if b['channel']=='sip' else ('whatsapp:' if wa else '')+self.config['whatsapp_from' if wa else 'phone_from'],
              'Twiml':twiml,'Timeout':'30','TimeLimit':str(b['max_duration_secs']),
              'StatusCallback':base+'/provider/twilio/status/'+call['id'],
              'StatusCallbackMethod':'POST','StatusCallbackEvent':'completed'}
        if b['channel']=='sip' and self.config.get('sip_auth_username'):
            data.update(SipAuthUsername=self.config['sip_auth_username'],SipAuthPassword=self.config['sip_auth_password'])
        r=await self.http.post(self._root()+'/Calls.json',data=data,auth=self._auth())
        if r.status_code not in {200,201}:
            if 400<=r.status_code<500: raise ServiceError('provider_rejected')
            raise httpx.ReadError('ambiguous_provider_response')
        value=r.json()
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}',value.get('sid','')):
            raise httpx.ReadError('ambiguous_provider_receipt')
        return {'provider_call_id':value['sid'],'provider_status':value.get('status','queued')}

    async def status(self,sid):
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}',sid): raise ServiceError('invalid_provider_id')
        r=await self.http.get(self._root()+'/Calls/'+sid+'.json',auth=self._auth())
        if r.status_code!=200: raise ServiceError('provider_status_unavailable')
        value=r.json()
        return {'provider_call_id':sid,'provider_status':value.get('status','unknown')}

    async def hangup(self,sid):
        if not re.fullmatch(r'CA[0-9a-fA-F]{32}',sid): raise ServiceError('invalid_provider_id')
        r=await self.http.post(self._root()+'/Calls/'+sid+'.json',data={'Status':'completed'},auth=self._auth())
        if r.status_code not in {200,404}: raise ServiceError('provider_hangup_unconfirmed')

    def signature_valid(self,url,params,signature):
        import base64
        value=url+''.join(k+v for k in sorted(params) for v in sorted(set(params.getlist(k) if hasattr(params,'getlist') else [params[k]])))
        expected=base64.b64encode(hmac.new(self.config.get('auth_token','').encode(),value.encode(),hashlib.sha1).digest()).decode()
        return bool(self.config.get('auth_token')) and hmac.compare_digest(expected,signature or '')

    async def close(self):
        if self.owned: await self.http.aclose()


class Outbound:
    def __init__(self,service,key,settings,providers,model_key=None):
        self.service,self.key,self.settings,self.providers=service,key,settings,providers
        self.model_key=model_key
        self.lock=asyncio.Lock(); self.tasks={};self.runtime_stop=None
        service.db.execute('CREATE TABLE IF NOT EXISTS outbound_attempts (call_id TEXT PRIMARY KEY, token TEXT NOT NULL)')
        service.db.execute('CREATE TABLE IF NOT EXISTS cap_raises (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, '
                           'principal TEXT NOT NULL, owner_instruction TEXT NOT NULL, extra_units INTEGER NOT NULL)')
        service.db.commit()

    def units_used(self):
        """Dial units of external attempts started in the last 24 hours, from durable call rows."""
        cutoff=(datetime.now(timezone.utc)-timedelta(hours=24)).isoformat()
        rows=self.service.db.execute("SELECT json_extract(body,'$.dial_units'),json_extract(body,'$.brief.channel'),"
            "json_extract(body,'$.brief.destination') FROM calls WHERE "
            "json_extract(body,'$.channel')!='browser' AND json_extract(body,'$.started_at')>=?",(cutoff,))
        total=0
        for units,channel,destination in rows:
            if units is None:
                # Rows from before ANYM-288 carry no charge: derive it from the frozen destination.
                try: units=policy.dial_units(channel,destination)
                except ServiceError: units=1
            total+=units
        return total

    def active_raises(self):
        """Unexpired owner raises of the daily cap (ANYM-353), oldest first, from durable rows."""
        rows=self.service.db.execute('SELECT id,created_at,expires_at,principal,owner_instruction,extra_units FROM cap_raises '
                                     'WHERE expires_at>? ORDER BY created_at',(now(),))
        return [dict(zip(RAISE_FIELDS,row)) for row in rows]

    def daily_call_units(self,raises=None):
        """Effective cap; pass one active_raises() snapshot so related fields agree across an expiry."""
        raises=self.active_raises() if raises is None else raises
        return min(policy.CAP_CEILING,policy.DAILY_UNITS+sum(r['extra_units'] for r in raises))

    def daily_calls_remaining(self,raises=None):
        return max(0,self.daily_call_units(raises)-self.units_used())

    def cap_status(self,principal=None):
        """Effective cap with its raises. Given a principal, other principals' raises omit the owner's words."""
        active=self.active_raises()
        raises=[r if principal is None or r['principal']==principal else
                {k:r[k] for k in ('id','created_at','expires_at','extra_units')} for r in active]
        return {'base_units':policy.DAILY_UNITS,'daily_call_units':self.daily_call_units(active),'call_cap_ceiling':policy.CAP_CEILING,
                'daily_calls_remaining':self.daily_calls_remaining(active),'raises':raises}

    async def raise_cap(self,owner_instruction,extra_units,principal,*,admission=None):
        """Raise the daily cap for 24 hours on the owner's quoted reply, which must state the unit count.
        The same words and count while that raise is active return it instead of adding another.
        `admission` rechecks the caller's authority after the lock, as for dial."""
        instruction=owner_instruction.strip() if isinstance(owner_instruction,str) else ''
        if not 3<=len(instruction)<=1500: raise ServiceError('owner_instruction_reference_required',422)
        if not isinstance(extra_units,int) or isinstance(extra_units,bool) or not 1<=extra_units<=policy.MAX_RAISE_UNITS:
            raise ServiceError('invalid_arguments',422)
        if not policy.units_named(instruction,extra_units): raise ServiceError('units_not_in_owner_instruction',422)
        async with self.lock:
            if admission is not None:
                admission()
            active=self.active_raises()
            same=next((r for r in active if (r['principal'],r['owner_instruction'],r['extra_units'])==(principal,instruction,extra_units)),None)
            repeated=same is not None
            if not repeated:
                room=policy.CAP_CEILING-self.daily_call_units(active)
                if extra_units>room: raise ServiceError('call_cap_ceiling',422,{'raisable_units':room,'call_cap_ceiling':policy.CAP_CEILING})
                created=datetime.now(timezone.utc)
                same=dict(zip(RAISE_FIELDS,(secrets.token_hex(8),created.isoformat(),
                    (created+timedelta(hours=policy.RAISE_HOURS)).isoformat(),principal,instruction,extra_units)))
                self.service.db.execute('INSERT INTO cap_raises VALUES (?,?,?,?,?,?)',tuple(same.values())); self.service.db.commit()
            return {'raise':same,'repeated':repeated,'call_cap':self.cap_status(principal)}

    async def preflight(self,brief_id):
        b=self.service.get_brief(brief_id)
        if b['channel']=='browser': raise ServiceError('external_brief_required')
        if not self.settings.enabled: raise ServiceError('calling_disabled')
        if b['channel']=='sip' and b['destination'] not in self.settings.allowed.get('sip',[]): raise ServiceError('recipient_not_allowed')
        units=policy.dial_units(b['channel'],b['destination'])
        if not (self.model_key(b) if self.model_key else self.key()): raise ServiceError('model_key_required')
        provider=self.providers.get(b['account'])
        if not provider: raise ServiceError('provider_not_configured')
        result=await provider.preflight(b)
        active=self.active_raises()
        return {**result,'recipient_policy':policy.recipient_policy(b['channel']),'dial_units':units,
                'daily_call_units':self.daily_call_units(active),'daily_calls_remaining':self.daily_calls_remaining(active)}

    async def dial(self,brief_id,authorization_reference,*,admission=None,origin=None):
        """Internal entry point. Agent-facing surfaces always pass `origin` (destination_source/evidence)."""
        if not isinstance(authorization_reference,str) or not 3<=len(authorization_reference.strip())<=1500:
            raise ServiceError('owner_instruction_reference_required',422)
        instruction=authorization_reference.strip()
        async with self.lock:
            # Check the durable record BEFORE readiness: retries cannot become another call,
            # including after restarts, changed settings, or missing credentials.
            row=self.service.db.execute('SELECT id FROM calls WHERE brief_id=?',(brief_id,)).fetchone()
            if row: return self.service.get_call(row[0])
            ready=await self.preflight(brief_id)
            bound={}
            if origin is not None:
                bound=policy.bind_origin(self.service.get_brief(brief_id),instruction,
                                         origin.get('destination_source'),origin.get('destination_evidence'))
            if ready['dial_units']>ready['daily_calls_remaining']: raise ServiceError('daily_call_limit',429)
            if admission is not None:
                admission()
            call=self.service.start(brief_id); cid=call['id']; token=secrets.token_urlsafe(32)
            self.service.db.execute('INSERT INTO outbound_attempts VALUES (?,?)',(cid,token)); self.service.db.commit()
            self.service.provider_update(cid,dial_state='requesting',authorization_reference=instruction,
                                         owner_instruction=instruction,dial_units=ready['dial_units'],**bound)
            try:
                receipt=await self.providers[call['brief']['account']].dial({**call,'stream_token':token})
                bound=self.service.get_call(cid).get('provider_call_id')
                if bound and bound!=receipt['provider_call_id']:raise httpx.ReadError('provider_receipt_mismatch')
                self.service.provider_update(cid,dial_state='accepted',**receipt)
                if self.service.get_call(cid)['status'] in ACTIVE:
                    self.tasks[cid]=asyncio.create_task(self._watch(cid),name='outbound-watch-'+cid)
                else:
                    await self.providers[call['brief']['account']].hangup(receipt['provider_call_id'])
            except ServiceError as exc:
                self.service.provider_update(cid,dial_state='rejected',provider_status=exc.code)
                await self._end_runtime(cid,'provider_rejected')
            except (Exception,asyncio.CancelledError) as exc:
                # A timeout/connection loss is not proof that the provider did not dial.
                self.service.provider_update(cid,dial_state='unknown',provider_status='reconciliation_required')
                await self._end_runtime(cid,'dial_unknown')
                if isinstance(exc,asyncio.CancelledError): raise
            return self.service.get_call(cid)

    def valid_stream(self,cid,token,sid):
        row=self.service.db.execute('SELECT token FROM outbound_attempts WHERE call_id=?',(cid,)).fetchone()
        call=self.service.get_call(cid)
        valid=bool(row and isinstance(token,str) and hmac.compare_digest(row[0],token) and
                   call['status'] in ACTIVE and call['brief'].get('account')=='twilio' and re.fullmatch(r'CA[0-9a-fA-F]{32}',sid))
        # A signed media handshake can race the REST response. The secret is
        # unique to the durable attempt; bind its SID without creating a call.
        if valid and not call.get('provider_call_id') and call.get('dial_state')=='requesting':
            self.service.provider_update(cid,provider_call_id=sid,provider_status='in-progress')
            return True
        return valid and call.get('provider_call_id')==sid

    async def _watch(self,cid):
        call=self.service.get_call(cid); provider=self.providers[call['brief']['account']]
        try:
            async with asyncio.timeout(call['brief']['max_duration_secs']+40):
                while self.service.get_call(cid)['status'] in ACTIVE:
                    await asyncio.sleep(3)
                    if hasattr(provider,'status'):
                        try: receipt=await provider.status(call['provider_call_id'])
                        except Exception: continue
                        self.service.provider_update(cid,**receipt)
                        if receipt['provider_status'] in {'completed','busy','failed','no-answer','canceled'}:
                            await self._end_runtime(cid,'provider_'+receipt['provider_status']); return
        except TimeoutError:
            await self._end_runtime(cid,'duration_limit')
        finally:
            self.tasks.pop(cid,None)

    async def _end_runtime(self,cid,reason):
        # Detach before awaiting the audio worker: its cleanup can call stop().
        # Otherwise the watchdog and worker would wait for one another.
        self.tasks.pop(cid,None)
        if self.runtime_stop:await self.runtime_stop(cid,reason)
        else:await self.stop(cid,reason)

    async def recover(self):
        # A restarted worker cannot resume a real conversation. Reconcile only
        # known provider IDs and terminate any still active call; never redial.
        for call in self.service.list_calls():
            provider=self.providers.get(call['brief'].get('account'))
            if call.get('end_reason')!='service_restart' or call.get('dial_state')!='accepted' or not call.get('provider_call_id') or not provider or not hasattr(provider,'status'):
                continue
            try:
                receipt=await provider.status(call['provider_call_id'])
                if receipt['provider_status'] not in {'completed','busy','failed','no-answer','canceled'}:
                    await provider.hangup(call['provider_call_id'])
                    receipt['provider_status']='completed_on_recovery'
                self.service.provider_update(call['id'],**receipt)
            except Exception:self.service.provider_update(call['id'],provider_status='reconciliation_required')

    async def stop(self,cid,reason='owner_stop'):
        call=self.service.get_call(cid); provider=self.providers.get(call['brief'].get('account'))
        if call['status'] in ACTIVE and provider and hasattr(provider,'cancel_attempt'):
            await provider.cancel_attempt(cid)
        if call['status'] in ACTIVE and call.get('provider_call_id') and provider and call.get('provider_status') not in {'completed','busy','failed','no-answer','canceled'}:
            try: await provider.hangup(call['provider_call_id'])
            except Exception:
                self.service.provider_update(cid,provider_status='hangup_unconfirmed')
        self.service.stop(cid,reason)
        task=self.tasks.get(cid)
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError): await task
        return self.service.get_call(cid)

    async def close(self):
        for call in self.service.list_calls():
            if call['channel']!='browser' and call['status'] in ACTIVE: await self.stop(call['id'],'service_shutdown')
        for provider in self.providers.values():
            if hasattr(provider,'close'): await provider.close()
