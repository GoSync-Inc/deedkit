"""Durable ingestion and worker leases. Used only by operator/collector DB roles."""
from contextlib import contextmanager
from datetime import datetime,timedelta
import hashlib
import os
import stat
import re
import threading
from time import monotonic

from psycopg.types.json import Jsonb

from .envelope import canonical, fingerprint, stable_id, validate
from .store import serial as serial
from .incoming_formatting import known as known_formatting, unknown as unknown_formatting, wire_digest

# The retry-exhausted sweep has no selective index: one Archive (a collector)
# runs it on its first media claim for an account, then at most once per this.
MEDIA_SWEEP_SECONDS = 60
# Out-of-window originals the background backfill fetches by itself: voice notes, then
# small files. Larger ones stay available on an explicit request.
BACKFILL_VOICE_MIMES = ('audio/ogg','audio/opus')
BACKFILL_VOICE_BYTES = 32*1024**2
BACKFILL_SMALL_BYTES = 1024**2
BACKFILL_CANDIDATES = 16
# 0 voice note, 1 small file, 2 any other original.
MEDIA_TIER = '''CASE WHEN j.descriptor->>'mime'=ANY(%(voice_mimes)s)
        AND (j.descriptor->>'bytes')::bigint BETWEEN 0 AND %(voice_max)s THEN 0
    WHEN (j.descriptor->>'bytes')::bigint BETWEEN 0 AND %(small_max)s THEN 1 ELSE 2 END'''
# A WhatsApp user identity with and without its device number (`<user>:<device>@lid`) is one
# sender, as whatsmeow's JID.ToNonAD() reads it. Used for comparison only; senders stay raw.
WHATSAPP_USER=re.compile(r'([0-9]+)(?::[0-9]+)?@(lid|c\.us)')


def whatsapp_user(sender):
    match=WHATSAPP_USER.fullmatch(sender); return match.groups() if match else None


# WhatsApp states message times in whole seconds (metadata `timestamp_resolution`), and its live,
# history and reconcile paths can read one message's time a few seconds apart (ANYM-530 class A).
WHATSAPP_SENT_AT_TOLERANCE=timedelta(seconds=6)
REVISION_FIELDS=('message','provider_chat','chat','revision')


def whatsapp_sent_at_jitter(retained,candidate):
    """True when a WhatsApp observation differs from the retained revision only by a sent time at
    most WHATSAPP_SENT_AT_TOLERANCE apart, both sides stating second resolution. `retained` is the
    revision snapshot, `candidate` a validated envelope. Pure, so the conflict re-evaluation can call
    it; evidence it cannot read is no tolerance, never an exception inside ingest."""
    try:
        if candidate['platform']!='whatsapp' or retained['platform']!='whatsapp' or retained['deleted'] or candidate['deleted']:
            return False
        if any(side.get('metadata',{}).get('timestamp_resolution')!='second' for side in (retained,candidate)):return False
        gap=abs(datetime.fromisoformat(candidate['sent_at'])-datetime.fromisoformat(retained['sent_at']))
        if not timedelta(0)<gap<=WHATSAPP_SENT_AT_TOLERANCE:return False
        # Formatting the candidate does not know is inherited, as for an exact re-read; known formatting must agree.
        if known_formatting(candidate.get('formatting')) and candidate.get('formatting')!=retained.get('formatting'):return False
        envelope={**{k:v for k,v in retained.items() if k not in REVISION_FIELDS},'chat':retained['provider_chat']}
        return fingerprint({**candidate,'sent_at':retained['sent_at']})==fingerprint(envelope)
    except (KeyError,TypeError,ValueError,AttributeError):
        return False


# WhatsApp business messages whose visible text the adapters now return (adapters.py whatsapp_message,
# whatsmeow capture.go). A revision captured before that has an unknown kind and no text (ANYM-530 class C).
# Never `document`: an empty caption is not missing text (ANYM-560 criterion 6).
WHATSAPP_TEXT_TYPES=('template','interactive','buttons','list','product','group_invite')


def whatsapp_body_completion(retained,candidate):
    """True when a WhatsApp business message's text completes a retained revision of unknown or no kind
    with an empty body: same identity, media and provider time, neither deleted. `retained` is the revision
    snapshot, `candidate` a validated envelope. Pure, so the conflict re-evaluation can call it; evidence it
    cannot read is no completion, never an exception inside ingest."""
    try:
        if candidate['platform']!='whatsapp' or retained['platform']!='whatsapp' or retained['deleted'] or candidate['deleted']:
            return False
        if candidate.get('metadata',{}).get('provider_message_type') not in WHATSAPP_TEXT_TYPES:return False
        if retained.get('metadata',{}).get('message_kind') not in (None,'unknown') or retained['body'] or not candidate['body']:
            return False
        if datetime.fromisoformat(candidate['updated_at'])!=datetime.fromisoformat(retained['updated_at']):return False
        # WhatsApp's known formatting is the digest of its own body: a retained one must state no text.
        if known_formatting(retained.get('formatting')) and retained['formatting'].get('wire_sha256')!=wire_digest(''):return False
        envelope={**{k:v for k,v in retained.items() if k not in REVISION_FIELDS},'chat':retained['provider_chat']}
        return fingerprint({**candidate,'body':''})==fingerprint(envelope)
    except (KeyError,TypeError,ValueError,AttributeError):
        return False


def telegram_web_preview_refresh(retained,candidate,version):
    """True when a Telegram link preview differs from the retained revision only by its attachments, both
    sides naming the same preview page ID: Telegram re-crawled the page (updateWebPage, no edit). Same
    identity, text and formatting, neither deleted, the candidate at the retained provider `version`.
    `retained` is the revision snapshot, `candidate` a validated envelope, `version` the message's stored
    provider time (an identical re-read can advance it past the snapshot's). Pure, so the conflict
    re-evaluation can call it; evidence it cannot read is no refresh, never an exception inside ingest."""
    try:
        if candidate['platform']!='telegram' or retained['platform']!='telegram' or retained['deleted'] or candidate['deleted']:
            return False
        pages=[side.get('metadata',{}) for side in (retained,candidate)]
        if any(page.get('provider_message_type')!='webpage' for page in pages):return False
        if pages[0].get('web_preview_id') is None or pages[0].get('web_preview_id')!=pages[1].get('web_preview_id'):return False
        if version is None or datetime.fromisoformat(candidate['updated_at'])!=version:return False
        if candidate.get('formatting')!=retained.get('formatting') or not isinstance(retained['media'],list):return False
        if candidate['media']==retained['media']:return False
        envelope={**{k:v for k,v in retained.items() if k not in REVISION_FIELDS},'chat':retained['provider_chat']}
        return fingerprint({**candidate,'media':retained['media']})==fingerprint(envelope)
    except (KeyError,TypeError,ValueError,AttributeError):
        return False


def whatsapp_unreadable_shape(e):
    """A WhatsApp observation that could not be read: kind `unknown`, no text, no attachment."""
    return (e['platform']=='whatsapp' and e.get('metadata',{}).get('message_kind')=='unknown'
        and not e['body'] and not e['media'] and not e['deleted'])


def ingest_outcome(old,e,retained_snapshot,sender_alias=False):
    """What ingest does with the validated candidate `e` against the retained message row `old` (None: a new message)
    and the retained revision's snapshot: (outcome, sender_completed, sender_device_only, unreadable, sent_at_jitter,
    jitter_version, body_completed, web_preview_refresh, known_candidate, known_retained, old_format). The version a
    rule compares with is `old['provider_updated_at']`. `sender_alias` is ingest's ANYM-559 lookup, a database read:
    the caller makes it and has already put the retained sender into `e`. Pure, so conflict re-evaluation asks the
    same rule whether it would still raise a stored conflict (ANYM-621); it passes no alias, which keeps a row raised
    (open) wherever the alias would lift it."""
    content=fingerprint(e); updated=datetime.fromisoformat(e['updated_at']); outcome='created'
    old_format=retained_snapshot.get('formatting') if old and retained_snapshot else None
    candidate_format=e.get('formatting')
    known_candidate=known_formatting(candidate_format);known_retained=known_formatting(old_format)
    formatting_changed=known_candidate and known_retained and candidate_format!=old_format
    formatting_completed=known_candidate and not known_retained
    sender_completed=bool(old and not old['sender'] and e['sender'] and not old['deleted']
        and not e['deleted'] and e['platform']=='telegram' and e['direction']=='outgoing'
        and e.get('metadata',{}).get('chat_type')=='private'
        and e.get('metadata',{}).get('sender_identity_basis')=='authenticated_self'
        and old['provider_updated_at']==updated
        and fingerprint({**e,'sender':''})==old['content_hash']
        and not formatting_changed)
    # A group sender is never inferred (it may be anonymous): it completes only when Telegram
    # itself named the owner, which the adapter marks with the authenticated basis. A retained
    # revision observed as private or channel never qualifies (ANYM-620).
    group_sender_completed=bool(old and retained_snapshot and not old['sender'] and e['sender']
        and not old['deleted'] and not e['deleted'] and e['platform']=='telegram' and e['direction']=='outgoing'
        and e.get('metadata',{}).get('chat_type')=='group'
        and (retained_snapshot.get('metadata') or {}).get('chat_type') in (None,'unknown','group')
        and e.get('metadata',{}).get('sender_identity_basis')=='authenticated_self'
        and old['provider_updated_at']==updated
        and fingerprint({**e,'sender':''})==old['content_hash']
        and not formatting_changed)
    sender_completed=sender_completed or group_sender_completed
    # The same WhatsApp sender with and without a device number, everything else equal,
    # contradicts nothing: the retained sender, revision and sender profiles stay, and it
    # is no later evidence for an open conflict.
    sender_device_only=bool(old and e['platform']=='whatsapp' and old['sender']!=e['sender']
        and not old['deleted'] and not e['deleted'] and whatsapp_user(old['sender']) is not None
        and whatsapp_user(old['sender'])==whatsapp_user(e['sender'])
        and fingerprint({**e,'sender':old['sender']})==old['content_hash']
        and (candidate_format==old_format or not (known_candidate or known_retained)))
    # An observation WhatsApp could not read (kind `unknown`, no text, no attachment) at the
    # retained provider version carries nothing: the retained revision, its metadata and
    # sender profiles stay, and it resolves no conflict.
    unreadable=bool(whatsapp_unreadable_shape(e) and old and retained_snapshot and retained_snapshot.get('body')
        and not old['deleted'] and old['provider_updated_at']==updated and content!=old['content_hash']
        and fingerprint({**e,'body':retained_snapshot.get('body',''),
            'media':retained_snapshot.get('media',[])})==old['content_hash'])
    # A WhatsApp re-read whose sent time is a few seconds off, everything else equal, is the
    # retained revision. An unedited message's provider version is its sent time, so a version
    # no later than the retained one plus that offset is no later evidence: the stored version
    # stays and it resolves no conflict. A later version (an edit time) counts as for an exact re-read.
    sent_at_jitter=bool(old and retained_snapshot and whatsapp_sent_at_jitter(retained_snapshot,e)
        and fingerprint({**e,'sent_at':retained_snapshot['sent_at']})==old['content_hash'])
    jitter_version=bool(sent_at_jitter and old['provider_updated_at'] is not None and updated<=old['provider_updated_at']
        +max(datetime.fromisoformat(e['sent_at'])-old['sent_at'],timedelta(0)))
    # A WhatsApp business message's text at the retained provider version completes a revision
    # stored without text: a new revision, the empty one stays readable. It is no later
    # evidence, so it resolves no open conflict.
    body_completed=bool(old and retained_snapshot and whatsapp_body_completion(retained_snapshot,e)
        and not old['body'] and old['provider_updated_at']==updated
        and fingerprint({**e,'body':''})==old['content_hash'])
    # Telegram's re-crawl of the same link preview page at the retained provider version changes
    # only the attachment: the retained revision, its metadata, media and attachments stay, and it
    # is no later evidence for an open conflict (ANYM-611).
    web_preview_refresh=bool(old and retained_snapshot and not old['deleted']
        and telegram_web_preview_refresh(retained_snapshot,e,old['provider_updated_at'])
        and fingerprint({**e,'media':retained_snapshot['media']})==old['content_hash'])
    if old:
        if old['deleted'] and not e['deleted']: outcome='tombstoned'
        elif sender_completed:outcome='edited'
        elif sender_device_only:outcome='unchanged'
        elif unreadable:outcome='unchanged'
        elif sent_at_jitter:outcome='stale' if old['provider_updated_at'] and updated<old['provider_updated_at'] else 'unchanged'
        elif body_completed:outcome='edited'
        elif web_preview_refresh:outcome='unchanged'
        elif (old['sent_at']!=datetime.fromisoformat(e['sent_at']) or old['direction']!=e['direction']
              or old['sender']!=e['sender']): outcome='conflict'
        elif old['content_hash']==content and not (formatting_changed or formatting_completed): outcome='unchanged'
        elif old['provider_updated_at'] and updated<old['provider_updated_at']: outcome='stale'
        elif old['content_hash']==content and formatting_completed:outcome='edited'
        elif (old['provider_updated_at']==updated and not e['deleted']): outcome='conflict'
        else: outcome='deleted' if e['deleted'] else 'edited'
        if sender_alias and outcome not in ('unchanged','stale') and old['provider_updated_at']==updated:outcome='conflict'
    elif e['deleted']: outcome='deleted'
    return (outcome,sender_completed,sender_device_only,unreadable,sent_at_jitter,jitter_version,body_completed,
            web_preview_refresh,known_candidate,known_retained,old_format)


class Archive:
    def __init__(self,store):
        self.store=store; self._media_sweeps={}; self._media_sweep_lock=threading.Lock()

    def set_sending(self,account,enabled):
        """Operator-only opt-in; never changes account discovery or agent scopes."""
        if type(enabled) is not bool:raise ValueError('invalid_send_policy')
        from .policy import audit
        with self.store.pool.connection() as c:
            changed=c.execute('UPDATE account_policies SET send_enabled=%s WHERE account=%s RETURNING account',
                (enabled,account)).fetchone()
            if not changed:raise ValueError('unknown_account')
            audit(c,None,'set_sending','ok')

    def register(self,account,platform,label,*,synthetic=True,include_chats=(),exclude_chats=(),discover=False):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',account) or platform not in ('telegram','whatsapp'):
            raise ValueError('invalid_account')
        if not discover and not include_chats: raise ValueError('empty_collection_policy')
        with self.store.pool.connection() as c:
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+account,))
            existing=c.execute('SELECT * FROM accounts WHERE id=%s FOR UPDATE',(account,)).fetchone()
            if existing and (existing['platform']!=platform or existing['synthetic']!=synthetic):
                raise ValueError('account_identity_change')
            c.execute('INSERT INTO accounts(id,platform,label,synthetic) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING',
                      (account,platform,label,synthetic))
            c.execute('''INSERT INTO account_policies(account,enabled,include_chats,exclude_chats,discover_chats)
                VALUES(%s,true,%s,%s,%s) ON CONFLICT(account) DO UPDATE SET enabled=true,
                include_chats=excluded.include_chats,exclude_chats=excluded.exclude_chats,
                discover_chats=excluded.discover_chats''',(account,list(include_chats),list(exclude_chats),discover))
            c.execute('INSERT INTO source_status(account) VALUES(%s) ON CONFLICT DO NOTHING',(account,))

    def acquire(self,account,owner,ttl=60):
        if not 10<=ttl<=300 or not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',owner):
            raise ValueError('invalid_lease')
        with self.store.pool.connection() as c:
            row=c.execute('''INSERT INTO collector_leases(account,owner,epoch,expires_at)
                VALUES(%s,%s,1,clock_timestamp()+%s*interval '1 second') ON CONFLICT(account)
                DO UPDATE SET owner=excluded.owner,epoch=collector_leases.epoch+1,expires_at=excluded.expires_at
                WHERE collector_leases.expires_at<clock_timestamp() RETURNING *''',(account,owner,ttl)).fetchone()
            if not row: raise RuntimeError('collector_already_owned')
            return row['epoch']

    def renew(self,account,owner,epoch,ttl=60):
        if not 10<=ttl<=300: raise ValueError('invalid_lease')
        with self.store.pool.connection() as c:
            changed=c.execute('''UPDATE collector_leases SET expires_at=clock_timestamp()+%s*interval '1 second'
                WHERE account=%s AND owner=%s AND epoch=%s AND expires_at>clock_timestamp()''',
                (ttl,account,owner,epoch)).rowcount
            if not changed: raise RuntimeError('collector_lease_lost')

    def release(self,account,owner,epoch):
        with self.store.pool.connection() as c:
            c.execute('''UPDATE collector_leases SET expires_at=clock_timestamp()
                WHERE account=%s AND owner=%s AND epoch=%s''',(account,owner,epoch))

    @staticmethod
    def fence(c,account,lease):
        if lease is None: return  # Trusted offline replay; never exposed via agent HTTP/MCP.
        owner,epoch=lease
        row=c.execute('''SELECT 1 FROM collector_leases WHERE account=%s AND owner=%s AND epoch=%s
            AND expires_at>clock_timestamp() FOR UPDATE''',(account,owner,epoch)).fetchone()
        if not row: raise RuntimeError('collector_lease_lost')

    def ingest(self,raw,*,lease=None,identity_observations=(),replay=False):
        e=validate(raw); captured=e; receipt=hashlib.sha256(canonical(e)).hexdigest()
        aid=e['account']; cid=stable_id('chat',aid,e['chat']); mid=stable_id('msg',aid,e['chat'],e['provider_id'])
        with self.store.pool.connection() as c:
            from .file_lifecycle import lifecycle_lock
            lifecycle_lock(c)
            # One bounded transaction per account; no provider/network/file work while holding it.
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+aid,))
            self.fence(c,aid,lease)
            policy=c.execute('''SELECT p.*,a.platform FROM account_policies p JOIN accounts a ON a.id=p.account
                WHERE account=%s''',(aid,)).fetchone()
            if not policy or not policy['enabled'] or policy['platform']!=e['platform']:
                raise ValueError('collection_disabled')
            if e['chat'] in policy['exclude_chats'] or (not policy['discover_chats'] and e['chat'] not in policy['include_chats']):
                return {'outcome':'excluded'}
            # Delete events can arrive before history imports. A chat-less
            # Telegram deletion applies only to its non-channel ID namespace.
            tombstone=c.execute('''SELECT max(t.observed_at) AS at FROM provider_tombstones t
                WHERE t.account=%s AND t.provider_id=%s AND (t.provider_chat=%s OR
                (t.provider_chat='' AND EXISTS(SELECT 1 FROM collector_chats cc
                    WHERE cc.account=t.account AND cc.provider_chat=%s AND cc.peer_kind IN ('user','chat'))))''',
                (aid,e['provider_id'],e['chat'],e['chat'])).fetchone()['at']
            if tombstone and not e['deleted']:
                e={**e,'deleted':True,'body':'','media':[],
                   'updated_at':max(tombstone,datetime.fromisoformat(e['updated_at'])).isoformat()}
                if 'formatting' in e:e['formatting']=unknown_formatting()
                if 'contact' in e.get('metadata',{}):e['metadata']={k:v for k,v in e['metadata'].items() if k!='contact'}
            if (identity_observations and not e['deleted']
                    and c.execute('SELECT 1 FROM messages WHERE id=%s AND NOT deleted',(mid,)).fetchone()):
                # A capture's identity evidence that contradicts a recorded pair is kept as a fence
                # before anything else, also for a duplicate receipt (ANYM-559). As for `record`, a
                # deleted or tombstoned message supplies no new evidence.
                from .whatsapp_identity import fence as fence_identity
                fence_identity(c,aid,mid,identity_observations)
            previous=c.execute('SELECT outcome FROM ingest_receipts WHERE digest=%s',(receipt,)).fetchone()
            if previous and not replay: return {'outcome':'duplicate','message':mid}
            chat_type=e.get('metadata',{}).get('chat_type')
            if chat_type=='unknown':chat_type=None
            # A WhatsApp observation that could not be read (kind `unknown`, no text, no attachment)
            # may create its chat but never rewrites the chat's title or type.
            unreadable_shape=whatsapp_unreadable_shape(e)
            if unreadable_shape:c.execute('''INSERT INTO chats(id,account,title,provider_chat,chat_type)
                VALUES(%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING''',(cid,aid,e['chat_title'],e['chat'],chat_type))
            else:c.execute('''INSERT INTO chats(id,account,title,provider_chat,chat_type) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(id) DO UPDATE SET
                title=CASE WHEN excluded.title<>%s AND excluded.title<>'' THEN excluded.title ELSE chats.title END,
                provider_chat=excluded.provider_chat,chat_type=coalesce(excluded.chat_type,chats.chat_type)
                WHERE (excluded.title<>%s AND excluded.title<>'' AND chats.title<>excluded.title)
                    OR chats.provider_chat IS DISTINCT FROM excluded.provider_chat
                    OR (excluded.chat_type IS NOT NULL AND chats.chat_type IS DISTINCT FROM excluded.chat_type)''',
                      (cid,aid,e['chat_title'],e['chat'],chat_type,e['chat'],e['chat']))
            old=c.execute('SELECT * FROM messages WHERE id=%s FOR UPDATE',(mid,)).fetchone()
            # One WhatsApp sender in its phone-number and LID forms (a fenced pair this account recorded)
            # or an unknown (empty) candidate sender is the retained sender for every comparison below:
            # the receipt keeps the raw value, a revision the retained one. At the retained provider
            # version it lifts only the sender difference (ANYM-559). One rule per field: with another
            # sent time as well (even ANYM-558's jitter) the observation stays a conflict.
            sender_alias=bool(old and e['platform']=='whatsapp' and old['sender'] and e['sender']!=old['sender']
                and not old['deleted'] and old['sent_at']==datetime.fromisoformat(e['sent_at']))
            if sender_alias and e['sender']:
                from .whatsapp_identity import same_sender
                sender_alias=same_sender(c,aid,old['sender'],e['sender'])
            if sender_alias:e={**e,'sender':old['sender']}
            content=fingerprint(e); updated=datetime.fromisoformat(e['updated_at'])
            retained=None;retained_snapshot=None
            if old:
                retained=c.execute('SELECT snapshot FROM message_revisions WHERE message=%s AND revision=%s',
                    (mid,old['revision'])).fetchone()
                retained_snapshot=retained['snapshot'] if retained else None
            (outcome,sender_completed,sender_device_only,unreadable,sent_at_jitter,jitter_version,body_completed,
             web_preview_refresh,known_candidate,known_retained,old_format)=ingest_outcome(old,e,retained_snapshot,sender_alias)
            if replay and outcome!='edited':raise ValueError('replay_not_accepted')
            # An incomplete observation of the same retained provider body must
            # not erase known formatting, including authenticated sender repair.
            inherited_format=bool(old and known_retained and not known_candidate and not e['deleted']
                and (old['content_hash']==content or sender_completed or sent_at_jitter))
            if inherited_format:e={**e,'formatting':old_format}
            if outcome in ('created','edited','deleted'):
                revision=old['revision']+1 if old else 1
                c.execute('''INSERT INTO messages(id,account,chat,provider_id,sender,body,sent_at,revision,
                    deleted,direction,provider_updated_at,content_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT(id) DO UPDATE SET sender=excluded.sender,body=excluded.body,revision=excluded.revision,
                    deleted=excluded.deleted,provider_updated_at=excluded.provider_updated_at,content_hash=excluded.content_hash''',
                    (mid,aid,cid,e['provider_id'],e['sender'],e['body'],e['sent_at'],revision,e['deleted'],
                     e['direction'],updated,content))
                snapshot={**e,'message':mid,'provider_chat':e['chat'],'chat':cid,'revision':revision}
                c.execute('INSERT INTO message_revisions(message,revision,snapshot) VALUES(%s,%s,%s)',
                          (mid,revision,Jsonb(snapshot)))
                media_ids=[]
                for descriptor in e['media']:
                    jid=stable_id('file',mid,hashlib.sha256(canonical(descriptor)).hexdigest())
                    media_ids.append(jid)
                    c.execute('''INSERT INTO media_jobs(id,account,message,descriptor) VALUES(%s,%s,%s,%s)
                        ON CONFLICT(id) DO UPDATE SET state=CASE WHEN media_jobs.state='superseded'
                        THEN 'queued' ELSE media_jobs.state END''',(jid,aid,mid,Jsonb(descriptor)))
                c.execute("UPDATE media_jobs SET state='superseded',lease_until=NULL WHERE message=%s AND NOT(id=ANY(%s))",
                          (mid,media_ids))
                # A formatting-only revision retains the same originals. Avoid
                # no-op active updates which otherwise enqueue extraction again.
                c.execute('''UPDATE attachments SET active=(id=ANY(%s)) WHERE message=%s
                    AND active IS DISTINCT FROM (id=ANY(%s))''',(media_ids,mid,media_ids))
            elif outcome=='unchanged' and not jitter_version:
                c.execute('UPDATE messages SET provider_updated_at=greatest(provider_updated_at,%s) WHERE id=%s',(updated,mid))
            if (outcome in ('created','edited','unchanged') and not e['deleted'] and e['sender'] and not sender_device_only
                    and not unreadable and not sender_alias):
                # Identity hints come only from this message's provider payload,
                # never a cross-chat contact book. Unknown metadata stays unknown.
                profile=e.get('sender_profile',{})
                if profile.get('display_name') or profile.get('username'):
                    c.execute('''INSERT INTO sender_profiles(id,account,chat,sender,display_name,username,
                        source_message,source_sent_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT(account,chat,sender) DO UPDATE SET
                        display_name=excluded.display_name,username=excluded.username,
                        source_message=excluded.source_message,source_sent_at=excluded.source_sent_at,
                        observed_at=clock_timestamp()
                        WHERE excluded.source_sent_at>=sender_profiles.source_sent_at''',
                        (stable_id('person',aid,cid,e['sender']),aid,cid,e['sender'],
                         profile.get('display_name',''),profile.get('username',''),mid,e['sent_at']))
            if (outcome in ('created','edited','unchanged') and not e['deleted'] and e.get('metadata') and not unreadable
                    and not web_preview_refresh):
                # Observations do not rewrite immutable revisions or create content conflicts.
                c.execute('''INSERT INTO message_metadata(message,revision,metadata) SELECT id,revision,%s
                    FROM messages WHERE id=%s AND (provider_updated_at IS NULL OR provider_updated_at<=%s)
                    ON CONFLICT(message) DO UPDATE SET
                    revision=excluded.revision,
                    metadata=CASE WHEN message_metadata.revision=excluded.revision
                        THEN message_metadata.metadata || jsonb_strip_nulls(excluded.metadata) ||
                            -- A sparser re-read of the same stated origin keeps the fields it already named;
                            -- a conflicting ID or time is another origin and replaces it.
                            CASE WHEN message_metadata.metadata->'forward_origin'->'kind'=excluded.metadata->'forward_origin'->'kind'
                                AND NOT EXISTS(SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(excluded.metadata->'forward_origin')='object'
                                        THEN excluded.metadata->'forward_origin' ELSE '{}'::jsonb END) o
                                    WHERE o.key IN ('sender_id','chat_id','provider_message_id','sent_at')
                                    AND message_metadata.metadata->'forward_origin'->o.key IS DISTINCT FROM o.value
                                    AND message_metadata.metadata->'forward_origin' ? o.key)
                                THEN jsonb_build_object('forward_origin',message_metadata.metadata->'forward_origin' ||
                                    jsonb_strip_nulls(excluded.metadata->'forward_origin'))
                                ELSE '{}'::jsonb END
                        ELSE excluded.metadata END,
                    observed_at=clock_timestamp()''',
                    (Jsonb(e['metadata']),mid,updated))
            # Publish only after this observation's validated native metadata is
            # present. The event trigger snapshots this exact revision once.
            if outcome in ('created','edited','deleted'):
                from .reply_evidence import capture as capture_reply
                reply = capture_reply(c,e,cid) if outcome=='created' else None
                self.store.append_event(c,aid,mid,'message.'+outcome,e['origin'],receipt,
                    snapshot_metadata={'reply_evidence':reply} if reply else None)
            if not previous:c.execute('INSERT INTO ingest_receipts(digest,account,envelope,outcome) VALUES(%s,%s,%s,%s)',
                (receipt,aid,Jsonb(captured),outcome))
            if outcome=='conflict':
                # The class compares the receipt with the contradicted revision, as the backfill does (ANYM-557).
                from .conflict_classes import REASON_VERSION,classify,stored
                if stored(c):
                    c.execute('''INSERT INTO message_conflicts(receipt_digest,message,observed_revision,
                        candidate_updated_at,observed_at,reason,reason_version) VALUES(%s,%s,%s,%s,clock_timestamp(),%s,%s)''',
                        (receipt,mid,old['revision'],updated,
                         classify(retained['snapshot'] if retained else None,captured,old['provider_updated_at']),
                         REASON_VERSION))
                else:c.execute('''INSERT INTO message_conflicts(receipt_digest,message,observed_revision,
                    candidate_updated_at,observed_at) VALUES(%s,%s,%s,%s,clock_timestamp())''',
                    (receipt,mid,old['revision'],updated))
            elif (outcome in ('edited','deleted','unchanged') and not sender_device_only and not unreadable and not jitter_version
                    and not body_completed and not web_preview_refresh and not (sender_alias and outcome=='unchanged')):
                # A repeated copy of the retained revision does not resolve an
                # equal/newer conflicting observation. Only later evidence does.
                # From schema 57 each resolve names how it was resolved (ANYM-621); N-1 rows keep NULL, unknown.
                from .conflict_reevaluation import resolution_set
                c.execute('''UPDATE message_conflicts mc SET resolved_at=clock_timestamp(),'''+resolution_set(c,'newer_observation')+'''
                    resolved_revision=(SELECT revision FROM messages WHERE id=%s)
                    FROM ingest_receipts r WHERE r.digest=mc.receipt_digest
                    AND mc.message=%s AND mc.resolved_at IS NULL AND mc.candidate_updated_at<%s
                    AND (NOT %s OR coalesce(r.envelope->'formatting'->>'state','unknown')='unknown'
                        OR r.envelope->'formatting'=%s::jsonb)''',
                    (mid,mid,updated,inherited_format,Jsonb(old_format)))
                if sender_completed:
                    # Only competing copies of this exact content are resolved.
                    # Unrelated equal-version contradictions stay visible.
                    candidates=c.execute('''SELECT mc.receipt_digest,mc.candidate_updated_at,r.envelope FROM message_conflicts mc
                        JOIN ingest_receipts r ON r.digest=mc.receipt_digest
                        WHERE mc.message=%s AND mc.resolved_at IS NULL
                        ORDER BY mc.observed_at,mc.receipt_digest LIMIT 100''',(mid,)).fetchall()
                    equivalent=[r['receipt_digest'] for r in candidates
                        if r['candidate_updated_at']<=updated
                        and fingerprint(validate(r['envelope'])) in (content,old['content_hash'])
                        and (not known_formatting(r['envelope'].get('formatting'))
                            or not known_formatting(e.get('formatting'))
                            or r['envelope']['formatting']==e['formatting'])]
                    if equivalent:c.execute('''UPDATE message_conflicts SET resolved_at=clock_timestamp(),'''+resolution_set(c,'sender_completed')+'''
                        resolved_revision=%s WHERE message=%s AND receipt_digest=ANY(%s) AND resolved_at IS NULL''',
                        (revision,mid,equivalent))
            c.execute("""UPDATE source_status SET last_ingest_at=clock_timestamp(),status='receiving',
                detail_code=CASE WHEN detail_code='version_conflict' THEN detail_code ELSE %s END WHERE account=%s""",
                      ('version_conflict' if outcome=='conflict' else 'coverage_not_reconciled',aid))
            return {'outcome':outcome,'message':mid}

    def claim_media(self,owner,*,account=None,ttl=120,collector_lease=None,since=None,writer=None,
                    live_writer=None,live_window_seconds=86400,media_prefetch_days=None,backfill=False):
        from .storage_admission import StorageAdmission
        from .media_policy import validate_prefetch_days
        validate_prefetch_days(media_prefetch_days)
        if type(backfill) is not bool: raise ValueError('invalid_backfill')
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,64}',owner) or not 10<=ttl<=600: raise ValueError('invalid_worker')
        if live_writer is not None and (writer is None or
                live_writer.root.resolve()!=writer.root.resolve() or
                not 0<=live_writer.reserve_bytes<=writer.reserve_bytes or
                not 0<live_writer.max_bytes<=writer.max_bytes or
                type(live_window_seconds) is not int or not 600<=live_window_seconds<=7*86400):
            raise ValueError('invalid_live_media_policy')
        live_max=live_writer.max_bytes if live_writer is not None else None
        tiers={'voice_mimes':list(BACKFILL_VOICE_MIMES),'voice_max':BACKFILL_VOICE_BYTES,'small_max':BACKFILL_SMALL_BYTES}
        # Backfill reaches out-of-window voice notes and small files of a bounded window
        # (days>0). Its few candidates are chosen before the shared storage locks are taken.
        backfill_ids=self._backfill_candidates(account,since,media_prefetch_days,tiers) if backfill and media_prefetch_days else []
        with self._media_sweep(account) as sweep, self.store.pool.connection() as c:
            from .file_lifecycle import lifecycle_lock
            lifecycle_lock(c)
            StorageAdmission.lock_and_reap(c)
            if collector_lease is not None:
                if account is None: raise ValueError('collector_account_required')
                self.fence(c,account,collector_lease)
            if sweep:
                # Attachment status already reports an expired fifth lease as
                # retry_exhausted until this sweep rewrites it.
                c.execute("""UPDATE media_jobs SET state='failed',error_code='retry_exhausted',lease_until=NULL
                    WHERE state='leased' AND attempts>=5 AND lease_until<clock_timestamp()
                    AND (%s::text IS NULL OR account=%s)""",(account,account))
            # One fresh time, taken after the lock waits, bounds the prefetch and live
            # windows; unlike per-row clock_timestamp() it can bound an index range.
            now=c.execute('SELECT clock_timestamp() AS at').fetchone()['at']
            candidates=''
            if media_prefetch_days is not None:
                # Dormant out-of-window jobs stay queued for explicit requests. Reach only
                # jobs that can be claimable now: unexpired requests (media_requested) and
                # messages inside the window (message_scope_date, or message_live_date
                # without an account, then media_message). Each ARRAY is an InitPlan, so
                # the job, message and chat joins start from these ids, never a backlog scan.
                arms=['SELECT r.id FROM media_jobs r WHERE r.requested_until>%(now)s'
                    +('' if account is None else ' AND r.account=%(account)s')]
                if media_prefetch_days>0:arms.append('''SELECT w.id FROM media_jobs w WHERE w.message=ANY(ARRAY(
                    SELECT wm.id FROM messages wm WHERE wm.sent_at<=%(now)s+interval '5 minutes'
                    AND wm.sent_at>=greatest(%(now)s-%(days)s::integer*interval '1 day',
                        coalesce(%(since)s::timestamptz,'-infinity'))'''
                    +(' AND NOT wm.deleted' if account is None else ' AND wm.account=%(account)s')+'))')
                if backfill_ids:arms.append('SELECT unnest(%(backfill_ids)s::text[])')
                candidates=' AND j.id=ANY(ARRAY('+' UNION ALL '.join(arms)+'))'
            # A cached generic plan of this repeated statement would not see the window bounds.
            c.execute("SET LOCAL plan_cache_mode=force_custom_plan")
            job=c.execute('''SELECT j.*,priority.live_media AS _live_media FROM media_jobs j JOIN account_policies p ON p.account=j.account
                JOIN messages m ON m.id=j.message AND m.account=j.account
                JOIN chats ch ON ch.id=m.chat AND ch.account=j.account
                CROSS JOIN LATERAL (SELECT COALESCE(%(live_max)s::bigint IS NOT NULL AND
                    (j.descriptor->>'bytes')::bigint BETWEEN 0 AND %(live_max)s::bigint AND
                    m.sent_at>=%(now)s-%(live_window)s*interval '1 second' AND
                    m.sent_at<=%(now)s+interval '5 minutes',false) AS live_media,
                    COALESCE(j.requested_until>clock_timestamp(),false) AS requested,'''+MEDIA_TIER+''' AS tier) priority
                WHERE p.enabled AND (%(account)s::text IS NULL OR j.account=%(account)s) AND j.attempts<5 AND
                NOT m.deleted AND NOT(ch.provider_chat=ANY(p.exclude_chats)) AND
                (p.discover_chats OR ch.provider_chat=ANY(p.include_chats)) AND
                (%(since)s::timestamptz IS NULL OR m.sent_at>=%(since)s OR
                    -- Below the bound only explicit demand in a chat whose row confirms this bound (ANYM-477);
                    -- an unconfirmed bound stays history_bound_unknown in media_policy and is not claimed.
                    j.requested_until>clock_timestamp() AND EXISTS(SELECT 1 FROM collector_chats bc
                        WHERE bc.account=j.account AND bc.provider_chat=ch.provider_chat
                        AND bc.history_since=%(since)s::timestamptz)) AND
                (%(days)s::integer IS NULL OR j.requested_until>clock_timestamp() OR
                    (%(days)s::integer>0 AND m.sent_at>=%(now)s-%(days)s*interval '1 day'
                        AND m.sent_at<=%(now)s+interval '5 minutes') OR
                    (j.id=ANY(%(backfill_ids)s::text[]) AND priority.tier<2)) AND
                ((j.state='queued' AND j.next_due<=clock_timestamp()) OR
                 (j.state='leased' AND j.lease_until<clock_timestamp()))'''+candidates+'''
                ORDER BY priority.requested DESC,CASE WHEN NOT priority.requested THEN j.id=ANY(%(backfill_ids)s::text[]) END,
                    priority.live_media DESC,
                    CASE WHEN priority.live_media THEN m.sent_at END DESC,
                    CASE WHEN NOT priority.requested THEN priority.tier END,
                    CASE WHEN NOT priority.requested THEN m.sent_at END DESC,j.next_due,j.id
                FOR UPDATE OF j SKIP LOCKED LIMIT 1''',
                {'live_max':live_max,'live_window':live_window_seconds,'account':account,'since':since,
                    'days':media_prefetch_days,'now':now,'backfill_ids':backfill_ids,**tiers}).fetchone()
            if not job: return None
            expires=c.execute("SELECT clock_timestamp()+%s*interval '1 second' AS at",(ttl,)).fetchone()['at']
            reservation=None
            if writer is not None:
                selected_writer=live_writer if job['_live_media'] else writer
                try:reservation=StorageAdmission(selected_writer).reserve(c,job,expires)
                except ValueError as exc:
                    if str(exc)!='media_size_limit':raise
                    c.execute("UPDATE media_jobs SET state='failed',error_code='size_limit',lease_until=NULL WHERE id=%s",
                        (job['id'],))
                    return None
                if reservation is None:
                    c.execute('''UPDATE media_jobs SET state='queued',error_code='paused_storage',lease_until=NULL,
                        next_due=clock_timestamp()+interval '60 seconds' WHERE id=%s''',(job['id'],))
                    return None
            claimed=c.execute('''UPDATE media_jobs SET state='leased',attempts=attempts+1,lease_owner=%s,
                lease_epoch=lease_epoch+1,lease_until=%s,error_code=NULL
                WHERE id=%s RETURNING *''',(owner,expires,job['id'])).fetchone()
            claimed['_live_media']=job['_live_media']
            if reservation is not None:claimed['_storage_reservation']=reservation
            return claimed

    def _backfill_candidates(self,account,since,days,tiers):
        # Read-only and lock-free: one bounded pass over the due queue that the
        # claim below re-checks under its locks and fences.
        with self.store.pool.connection() as c:
            return [r['id'] for r in c.execute('''SELECT j.id FROM media_jobs j
                JOIN account_policies p ON p.account=j.account
                JOIN messages m ON m.id=j.message AND m.account=j.account
                JOIN chats ch ON ch.id=m.chat AND ch.account=j.account
                CROSS JOIN LATERAL (SELECT '''+MEDIA_TIER+''' AS tier) priority
                WHERE ((j.state='queued' AND j.next_due<=clock_timestamp()) OR
                    (j.state='leased' AND j.lease_until<clock_timestamp())) AND j.attempts<5 AND
                (%(account)s::text IS NULL OR j.account=%(account)s) AND p.enabled AND NOT m.deleted AND
                NOT(ch.provider_chat=ANY(p.exclude_chats)) AND (p.discover_chats OR ch.provider_chat=ANY(p.include_chats)) AND
                (%(since)s::timestamptz IS NULL OR m.sent_at>=%(since)s) AND
                m.sent_at<clock_timestamp()-%(days)s*interval '1 day' AND priority.tier<2
                ORDER BY priority.tier,m.sent_at DESC,j.next_due,j.id LIMIT %(limit)s''',
                {'account':account,'since':since,'days':days,'limit':BACKFILL_CANDIDATES,**tiers})]

    @contextmanager
    def _media_sweep(self,account):
        # Takes a due sweep atomically, so concurrent claims do not repeat it; a
        # claim transaction that fails gives it back for the next claim.
        at=monotonic()
        with self._media_sweep_lock:
            last=self._media_sweeps.get(account)
            due=last is None or at-last>=MEDIA_SWEEP_SECONDS
            if due:self._media_sweeps[account]=at
        try:yield due
        except BaseException:
            if due:
                with self._media_sweep_lock:
                    if self._media_sweeps.get(account)==at:
                        if last is None:del self._media_sweeps[account]
                        else:self._media_sweeps[account]=last
            raise

    @staticmethod
    def job_fence(c,job):
        row=c.execute('''SELECT j.* FROM media_jobs j JOIN messages m ON m.id=j.message AND m.account=j.account
            JOIN chats ch ON ch.id=m.chat AND ch.account=j.account
            JOIN account_policies p ON p.account=j.account
            WHERE j.id=%s AND j.state='leased' AND j.lease_owner=%s
            AND j.lease_epoch=%s AND j.lease_until>clock_timestamp() AND NOT m.deleted AND p.enabled
            AND NOT(ch.provider_chat=ANY(p.exclude_chats))
            AND (p.discover_chats OR ch.provider_chat=ANY(p.include_chats)) FOR UPDATE OF j''',
            (job['id'],job['lease_owner'],job['lease_epoch'])).fetchone()
        if not row: raise RuntimeError('media_lease_lost')
        return row

    def renew_media(self,job,ttl=120,*,collector_lease=None):
        from .storage_admission import StorageAdmission
        if not 10<=ttl<=600: raise ValueError('invalid_lease')
        with self.store.pool.connection() as c:
            # Same lock order as admission: a concurrent reaper must not see
            # half of a renewed job/reservation pair and reclaim its capacity.
            StorageAdmission.lock(c)
            self.fence(c,job['account'],collector_lease)
            self.job_fence(c,job)
            row=c.execute("UPDATE media_jobs SET lease_until=clock_timestamp()+%s*interval '1 second' WHERE id=%s RETURNING lease_until",
                      (ttl,job['id'])).fetchone()
            changed=c.execute('''UPDATE media_reservations SET expires_at=%s WHERE job=%s AND lease_epoch=%s''',
                (row['lease_until'],job['id'],job['lease_epoch'])).rowcount
            if job.get('_storage_reservation') is not None and changed!=1:
                raise RuntimeError('media_lease_lost')
        if job.get('_storage_reservation') is not None:
            job['_storage_reservation'].expires_at=row['lease_until']

    def media_source(self,job,*,collector_lease=None):
        with self.store.pool.connection() as c:
            self.fence(c,job['account'],collector_lease)
            self.job_fence(c,job)
            row=c.execute('''SELECT r.snapshot FROM messages m JOIN message_revisions r
                ON r.message=m.id AND r.revision=m.revision WHERE m.id=%s AND NOT m.deleted''',
                (job['message'],)).fetchone()
            if not row: raise RuntimeError('media_lease_lost')
            return row['snapshot']

    def finish_media(self,job,blob,writer,*,collector_lease=None):
        # Keep the verified inode identity across the serving/lifecycle lock.
        # A different occurrence of the same CAS object may be evicted while
        # this newly downloaded occurrence has no registered digest yet.
        try:
            before_identity=os.stat(writer.root/blob.digest,follow_symlinks=False)
        except OSError as error:raise RuntimeError('blob_corrupt') from error
        verified_path=writer.verify(blob)  # Hash before holding the shared lock.
        identity=lambda info:(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
        try:
            verified_identity=os.stat(verified_path,follow_symlinks=False)
            if identity(before_identity)!=identity(verified_identity):raise RuntimeError('blob_corrupt')
            if not stat.S_ISREG(verified_identity.st_mode) or verified_identity.st_size!=blob.bytes:
                raise RuntimeError('blob_corrupt')
        except OSError as error:raise RuntimeError('blob_corrupt') from error
        with self.store.pool.connection() as c:
            from .file_lifecycle import lifecycle_lock,register_resident_original
            lifecycle_lock(c)
            try:current_identity=os.stat(self.store.files/blob.digest,follow_symlinks=False)
            except OSError as error:raise RuntimeError('blob_corrupt') from error
            if identity(current_identity)!=identity(verified_identity) or not stat.S_ISREG(current_identity.st_mode):
                raise RuntimeError('blob_corrupt')
            # Same account lock as message edits: no stale file publication after edit/delete.
            c.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('archive:'+job['account'],))
            self.fence(c,job['account'],collector_lease)
            current=self.job_fence(c,job); d=current['descriptor']
            if d['bytes'] is not None and blob.bytes!=d['bytes']: raise ValueError('media_size_mismatch')
            retained=c.execute('SELECT digest,bytes,active FROM attachments WHERE id=%s FOR UPDATE',(job['id'],)).fetchone()
            if retained and (retained['digest']!=blob.digest or retained['bytes']!=blob.bytes):
                raise ValueError('media_identity_mismatch')
            if not retained:
                c.execute('''INSERT INTO attachments(id,message,digest,filename,mime,bytes,active)
                    VALUES(%s,%s,%s,%s,%s,%s,true)''',
                    (job['id'],job['message'],blob.digest,d['filename'],d['mime'],blob.bytes))
            elif not retained['active']:
                c.execute('UPDATE attachments SET active=true WHERE id=%s',(job['id'],))
            # Rehydration changes residency only; no no-op attachment UPDATE
            # may invalidate the retained text, ASR or description triggers.
            register_resident_original(c,blob.digest,blob.bytes)
            c.execute("UPDATE media_jobs SET state='ready',error_code=NULL,lease_until=NULL WHERE id=%s",(job['id'],))
            source=c.execute('''SELECT m.revision,r.snapshot->>'origin' AS origin FROM messages m
                JOIN message_revisions r ON r.message=m.id AND r.revision=m.revision WHERE m.id=%s''',
                (job['message'],)).fetchone()
            # Original readiness is independent from OCR/model readiness. The
            # opaque version is salted with its occurrence, never a blob hash.
            version='original_'+hashlib.sha256(canonical(
                [job['id'],source['revision'],blob.digest])).hexdigest()
            self.store.append_event(c,job['account'],job['message'],'attachment.ready',
                source['origin'] or 'reconcile',
                stable_id('event',job['id'],blob.digest,str(job['lease_epoch'])),
                snapshot_metadata={'processing':{'attachment_id':job['id'],'stage':'original',
                    'state':'ready','artifact_version':version,'basis':'downloaded',
                    'source_revision':source['revision']}})
            from .storage_admission import StorageAdmission
            StorageAdmission.release(c,job)

    def release_media_reservation(self,job):
        """Idempotent cleanup even after an edit/delete invalidates job ownership."""
        from .storage_admission import StorageAdmission
        with self.store.pool.connection() as c:StorageAdmission.release(c,job)

    def fail_media(self,job,code,*,permanent=False,retry_after=None,collector_lease=None):
        from .operational import LOCAL_STORAGE_ERRORS,MEDIA_ERRORS
        allowed={'unavailable','size_limit','rate_limited','network_error','disk_reserve','paused_storage',
            'integrity_error','storage_unavailable'}
        if code not in allowed|MEDIA_ERRORS: raise ValueError('invalid_error_code')
        if retry_after is not None and (type(retry_after) is not int or not 1<=retry_after<2**31):
            raise ValueError('invalid_retry_delay')
        with self.store.pool.connection() as c:
            self.fence(c,job['account'],collector_lease)
            row=self.job_fence(c,job)
            local_pause=code in ('disk_reserve','paused_storage')
            # Unwritable local storage is operator-fixable: it never consumes a
            # provider attempt or fails the original terminally.
            local_storage=code in LOCAL_STORAGE_ERRORS
            attempts=max(0,row['attempts']-1) if local_pause or local_storage else row['attempts']
            terminal=not (local_pause or local_storage) and (permanent or attempts>=5)
            stored='paused_storage' if local_pause else 'storage_unavailable' if code=='media_storage_unavailable' else code
            c.execute('''UPDATE media_jobs SET state=%s,error_code=%s,lease_until=NULL,attempts=%s,
                next_due=clock_timestamp()+%s*interval '1 second' WHERE id=%s''',
                ('failed' if terminal else 'queued',stored,attempts,
                 60 if local_pause else 300 if local_storage else max(min(3600,10*2**attempts),retry_after or 0),job['id']))
            from .storage_admission import StorageAdmission
            StorageAdmission.release(c,job)
