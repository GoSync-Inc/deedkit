"""Account-local, provider-cache namespace observations; names are never proof."""
import re

from .envelope import utc,stable_id
from .policy import scope_filter

PROVENANCE='webjs_contact_cache_v1'
PROVENANCES=frozenset((PROVENANCE,'whatsmeow_message_alt_v1','whatsmeow_lid_store_v1'))


def personal_id(value):
    match=re.fullmatch(r'([0-9]{1,20})(?::[0-9]{1,5})?@(c\.us|lid)',value) if isinstance(value,str) else None
    return match[1]+'@'+match[2] if match else None


def observations(value,envelope):
    if not isinstance(value,list) or len(value)>2:raise ValueError('invalid_identity_observations')
    result=[]
    for item in value:
        if not isinstance(item,dict) or set(item)!={'identity_id','pn','lid','provenance','observed_at'}:
            raise ValueError('invalid_identity_observation')
        identity=personal_id(item['identity_id'])
        if envelope['platform']!='whatsapp' or item['identity_id'] not in (envelope['sender'],envelope['chat']):
            raise ValueError('identity_source_mismatch')
        if (not isinstance(item['pn'],str) or not isinstance(item['lid'],str) or
            personal_id(item['pn'])!=item['pn'] or not item['pn'].endswith('@c.us') or
            personal_id(item['lid'])!=item['lid'] or not item['lid'].endswith('@lid') or
            identity not in (item['pn'],item['lid']) or item['provenance'] not in PROVENANCES):
            raise ValueError('invalid_identity_observation')
        result.append({**item,'observed_at':utc(item['observed_at'])})
    return result


def record(archive,envelope,values,lease):
    if not values:return
    with archive.store.pool.connection() as c:
        archive.fence(c,envelope['account'],lease)
        mid=stable_id('msg',envelope['account'],envelope['chat'],envelope['provider_id'])
        # The independently committed message may have changed since this capture.
        row=c.execute('SELECT m.account,m.sender,m.deleted,ch.provider_chat FROM messages m '
            'JOIN chats ch ON ch.id=m.chat WHERE m.id=%s',(mid,)).fetchone()
        if not row or row['deleted'] or row['account']!=envelope['account']:return
        if envelope['sender'] and any(v['identity_id']==row['sender'] for v in values):
            # Verified namespace data can provide a directory entry even when the
            # provider has no display name. Do not replace a name observation.
            c.execute('''INSERT INTO sender_profiles(id,account,chat,sender,source_message,source_sent_at)
                SELECT %s,m.account,m.chat,m.sender,m.id,m.sent_at FROM messages m WHERE m.id=%s
                ON CONFLICT(account,chat,sender) DO NOTHING''',
                (stable_id('person',envelope['account'],stable_id('chat',envelope['account'],envelope['chat']),row['sender']),mid))
        for item in values:
            if item['identity_id'] not in (row['sender'],row['provider_chat']):continue
            c.execute('''INSERT INTO whatsapp_identity_observations(account,source_message,identity_id,
                pn,lid,provenance,first_observed_at,observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(account,source_message,identity_id,pn,lid,provenance) DO UPDATE
                SET first_observed_at=least(whatsapp_identity_observations.first_observed_at,excluded.first_observed_at),
                    observed_at=greatest(whatsapp_identity_observations.observed_at,excluded.observed_at)''',
                (envelope['account'],mid,item['identity_id'],item['pn'],item['lid'],item['provenance'],
                 item['observed_at'],item['observed_at']))


def visible_cte(p):
    """Source message authorizes exposure. Conflicts anywhere in account block links.

    A deleted/restricted source cannot supply a phone number. A conflicting historic
    observation remains a safety fence; it is never replaced by the last writer.
    """
    where,args=scope_filter(p,'m')
    return '''visible_whatsapp_identity AS NOT MATERIALIZED (
        SELECT wi.*,m.chat, messenger_whatsapp_personal_id(wi.identity_id) AS normalized_id,
          NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other
            WHERE other.account=wi.account AND
              ((other.pn=wi.pn AND other.lid<>wi.lid) OR (other.lid=wi.lid AND other.pn<>wi.pn))) AS verified
        FROM whatsapp_identity_observations wi JOIN messages m ON m.id=wi.source_message
        JOIN chats ic ON ic.id=m.chat JOIN accounts ia ON ia.id=m.account
        WHERE wi.account=m.account AND ia.platform='whatsapp' AND NOT m.deleted
          AND (wi.identity_id=m.sender OR wi.identity_id=ic.provider_chat) AND '''+where+')',args


def people_join():
    return ''' LEFT JOIN LATERAL (
        SELECT jsonb_build_object('state',CASE WHEN v.verified THEN 'verified' ELSE 'conflict' END,
          'confidence','provider_cache_observation','historical_identity_verified',false,
          'pn',CASE WHEN v.verified THEN v.pn END,'lid',CASE WHEN v.verified THEN v.lid END,
          'provenance',v.provenance,'observed_at',v.observed_at,'source_message',v.source_message) AS metadata,
          CASE WHEN v.verified THEN lower(v.pn || ' ' || v.lid) ELSE '' END AS search_text
        FROM visible_whatsapp_identity v WHERE v.account=sp.account AND v.chat=sp.chat
          AND v.normalized_id=messenger_whatsapp_personal_id(sp.sender)
        ORDER BY v.observed_at DESC,v.source_message,v.pn,v.lid LIMIT 1
        ) identity ON true '''


def resolve_alias(c,p,account,provider_chat_id):
    """Return one visible original chat; never derive/replace an opaque message ID."""
    identity=personal_id(provider_chat_id)
    if not identity or account not in p['accounts']:return None
    source_where,source_args=scope_filter(p,'m')
    where,chat_args=scope_filter(p,'c',chat_column='id')
    # A common owner pair can have thousands of source observations across
    # groups. Deduplicate exact account/identity candidates before proving one
    # visible source and checking account-wide conflicts. Materialize normalized
    # target chats once, rather than per chat × evidence join combination.
    rows=c.execute('''WITH candidate_pairs AS MATERIALIZED (
        SELECT DISTINCT pn,lid FROM whatsapp_identity_observations
        WHERE account=%s AND (pn=%s OR lid=%s)
        ), verified_pairs AS MATERIALIZED (
        SELECT pair.pn,pair.lid FROM candidate_pairs pair
        WHERE NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other
            WHERE other.account=%s AND other.pn=pair.pn AND other.lid<>pair.lid)
          AND NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other
            WHERE other.account=%s AND other.lid=pair.lid AND other.pn<>pair.pn)
          AND EXISTS(SELECT 1 FROM whatsapp_identity_observations wi
            JOIN messages m ON m.id=wi.source_message
            JOIN chats ic ON ic.id=m.chat JOIN accounts ia ON ia.id=m.account
            WHERE wi.account=%s AND wi.pn=pair.pn AND wi.lid=pair.lid
              AND wi.account=m.account AND ia.platform='whatsapp' AND NOT m.deleted
              AND (wi.identity_id=m.sender OR wi.identity_id=ic.provider_chat) AND '''+source_where+''')
        ), visible_chats AS MATERIALIZED (
        SELECT c.id,c.account,c.title,c.provider_chat,a.platform,
            messenger_whatsapp_personal_id(c.provider_chat) AS normalized_id
        FROM chats c JOIN accounts a ON a.id=c.account
        WHERE c.account=%s AND a.platform='whatsapp' AND '''+where+''')
        SELECT DISTINCT c.id,c.account,c.title,c.provider_chat,c.platform
        FROM visible_chats c JOIN verified_pairs pair ON c.normalized_id IN (pair.pn,pair.lid)
        ORDER BY c.id LIMIT 2''',
        [account,identity,identity,account,account,account,*source_args,account,*chat_args]).fetchall()
    if len(rows)>1:
        from .store import APIError
        raise APIError('ambiguous_reference',409)
    return rows[0] if rows else None


def chat_links(c,p,accounts=None):
    """Verified visible PN links of the caller's private LID chats (ANYM-278).

    The same evidence as resolve_alias: no conflicting observation anywhere in the
    account and one visible, undeleted source. `pn_chat` is the peer's private
    phone-number chat when the caller can read it; stored IDs are never changed.
    """
    lid_where,lid_args=scope_filter(p,'c',chat_column='id')
    pn_where,pn_args=scope_filter(p,'c',chat_column='id')
    source_where,source_args=scope_filter(p,'m')
    account_where=' AND c.account=ANY(%s)' if accounts is not None else ''
    return c.execute('''WITH lid_chats AS MATERIALIZED (
        SELECT c.id,c.account,messenger_whatsapp_personal_id(c.provider_chat) AS lid
        FROM chats c JOIN accounts a ON a.id=c.account
        WHERE a.platform='whatsapp' AND c.chat_type='private' AND right(c.provider_chat,4)='@lid' AND '''+lid_where+account_where+'''
        ), candidate AS MATERIALIZED (
        SELECT DISTINCT l.id AS lid_chat,l.account,wi.pn,wi.lid FROM lid_chats l
        JOIN whatsapp_identity_observations wi ON wi.account=l.account AND wi.lid=l.lid
        ), verified AS MATERIALIZED (
        SELECT pair.* FROM candidate pair
        WHERE NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other
            WHERE other.account=pair.account AND other.pn=pair.pn AND other.lid<>pair.lid)
          AND NOT EXISTS(SELECT 1 FROM whatsapp_identity_observations other
            WHERE other.account=pair.account AND other.lid=pair.lid AND other.pn<>pair.pn)
        ), sourced AS MATERIALIZED (
        SELECT v.*,(SELECT wi.provenance FROM whatsapp_identity_observations wi
            JOIN messages m ON m.id=wi.source_message JOIN chats ic ON ic.id=m.chat
            WHERE wi.account=v.account AND wi.pn=v.pn AND wi.lid=v.lid AND m.account=wi.account AND NOT m.deleted
              AND (wi.identity_id=m.sender OR wi.identity_id=ic.provider_chat) AND '''+source_where+'''
            ORDER BY wi.observed_at DESC,wi.source_message LIMIT 1) AS provenance
        FROM verified v)
        SELECT s.lid_chat,s.lid,s.pn,s.provenance,pc.id AS pn_chat FROM sourced s
        LEFT JOIN LATERAL (SELECT c.id FROM chats c WHERE c.account=s.account AND c.provider_chat=s.pn
            AND c.chat_type='private' AND '''+pn_where+''' LIMIT 1) pc ON true
        WHERE s.provenance IS NOT NULL ORDER BY s.lid_chat''',
        [*lid_args,*([accounts] if accounts is not None else []),*source_args,*pn_args]).fetchall()
