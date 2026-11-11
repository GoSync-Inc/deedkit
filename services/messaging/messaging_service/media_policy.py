"""Shared original-prefetch and disk-floor configuration for all collectors."""
from .storage_admission import MEDIA_RESERVE_BYTES


MEDIA_POLICY_FIELDS=frozenset({'media_disk_reserve_bytes','media_live_disk_reserve_bytes',
    'media_live_max_bytes','media_live_window_seconds','media_prefetch_days'})
DEFAULT_MEDIA_PREFETCH_DAYS=7


def validate_prefetch_days(value):
    # Explicit null is the compatibility opt-in for unbounded available history.
    if value is not None and (type(value) is not int or not 0<=value<=36500):
        raise ValueError('invalid_media_prefetch_days')
    return value


def configure_media_policy(cfg):
    """Validate after the caller has validated capture/max-file limits."""
    value=cfg.setdefault('media_disk_reserve_bytes',max(MEDIA_RESERVE_BYTES,cfg['disk_reserve_bytes']))
    if type(value) is not int or not cfg['disk_reserve_bytes']<=value<=100*1024**3:
        raise ValueError('invalid_media_disk_reserve_bytes')
    value=cfg.setdefault('media_live_disk_reserve_bytes',None)
    if value is not None and (type(value) is not int or
            not cfg['disk_reserve_bytes']<=value<=cfg['media_disk_reserve_bytes']):
        raise ValueError('invalid_media_live_disk_reserve_bytes')
    for key,default,lo,hi in [
            ('media_live_max_bytes',min(32*1024**2,cfg['max_media_bytes']),1,cfg['max_media_bytes']),
            ('media_live_window_seconds',86400,600,7*86400)]:
        value=cfg.setdefault(key,default)
        if type(value) is not int or not lo<=value<=hi:raise ValueError('invalid_'+key)
    validate_prefetch_days(cfg.setdefault('media_prefetch_days',DEFAULT_MEDIA_PREFETCH_DAYS))
    return cfg


# The window and history bound the account's current collector run published in
# collector_health.operations.media_policy (ANYM-284). A record counts only when its
# epoch is the account's lease epoch, so a run that could not publish never inherits
# an earlier run's window. Malformed values count as unpublished, never as a cast error.
def published_window(account):
    return '''LEFT JOIN LATERAL (SELECT true AS published,
        CASE WHEN jsonb_typeof(h.operations->'media_policy'->'prefetch_days')='number'
            THEN (h.operations->'media_policy'->>'prefetch_days')::numeric END AS days,
        CASE WHEN jsonb_typeof(h.operations->'media_policy'->'history_since')='string'
            THEN (h.operations->'media_policy'->>'history_since')::timestamptz END AS since
        FROM collector_health h JOIN collector_leases l ON l.account=h.account
        WHERE h.account='''+account+''' AND h.operations->'media_policy'->'epoch'=to_jsonb(l.epoch)
        AND (jsonb_typeof(h.operations->'media_policy'->'prefetch_days')='null' OR
            jsonb_typeof(h.operations->'media_policy'->'prefetch_days')='number'
            AND (h.operations->'media_policy'->>'prefetch_days') ~ '^[0-9]{1,5}$')
        AND (jsonb_typeof(h.operations->'media_policy'->'history_since')='null' OR
            jsonb_typeof(h.operations->'media_policy'->'history_since')='string'
            AND pg_input_is_valid(h.operations->'media_policy'->>'history_since','timestamptz'))) mp ON true'''


# ANYM-805 (owner 2026-11-06): media of a group or channel chat that no active agent subscription names waits for an
# agent's request; personal chats, chats of unknown kind and subscribed chats keep automatic download and recognition.
# Kind: chats.chat_type when private, group or channel, else collector_chats.peer_kind (user, chat, channel), else
# unknown, which counts as personal. Subscribed: listed in subscription_sources of a subscription that is active with
# expires_at unset or less than 24 hours past (expiry is lazy), or closed, expired or access_changed less than 24
# hours ago, so a renewal gap does not flip the chat. A dynamic subscription (all visible chats) lists no chat.
# The 24 hours are counted from one fixed instant, the start of the statement that reads the rule: a volatile
# clock_timestamp() in the NOT EXISTS kept it a subscription scan per chat (perf audit 2026-11-09: 18.7k scans/s,
# 0.2-0.3 s each); the stable statement_timestamp() lets the planner run it as one anti-join. Not now(): callers
# start their transaction and then wait for the residency lock, and a grace that ends during that wait must have
# ended for the claim, request or eviction check that follows it. It reads and never writes, and follows
# subscriptions as they start and end without rewriting a row. The worker roles read only these columns
# (*_role.sql, column grants).
ON_REQUEST_CHATS='''SELECT oc.id AS chat,CASE okind.kind WHEN 'channel' THEN 'channel_on_request'
        ELSE 'group_chat_on_request' END AS reason
    FROM chats oc LEFT JOIN collector_chats occ ON occ.account=oc.account AND occ.provider_chat=oc.provider_chat
    CROSS JOIN LATERAL (SELECT CASE WHEN oc.chat_type IN ('private','group','channel') THEN oc.chat_type
        WHEN occ.peer_kind='user' THEN 'private' WHEN occ.peer_kind='chat' THEN 'group'
        WHEN occ.peer_kind='channel' THEN 'channel' END AS kind) okind
    WHERE okind.kind IN ('group','channel') AND NOT EXISTS(SELECT FROM subscription_sources oss
        JOIN subscriptions os ON os.id=oss.subscription WHERE oss.chat=oc.id AND oss.account=oc.account
        AND (os.state='active' AND (os.expires_at IS NULL OR os.expires_at>statement_timestamp()-interval '24 hours')
            OR os.state IN ('closed','expired','access_changed') AND os.closed_at>statement_timestamp()-interval '24 hours'))'''
def on_request_chat(alias='m'):
    """`<alias>.chat` (a messages alias) is an on-request chat."""
    return alias+'.chat IN (SELECT orc.chat FROM ('+ON_REQUEST_CHATS+') orc)'


ON_REQUEST_CHAT=on_request_chat()
ON_REQUEST_REASONS=('group_chat_on_request','channel_on_request')
# Recognition (text extraction, OCR, transcription, description) of an attachment (alias a, message m) waits while it
# is in an on-request chat and its media job has no request: every accepted request_file_download sets the job's
# request key digest and nothing clears it (media_requests.py). An attachment without a media job (a legacy or
# restored original) is not withheld: no request could be recorded for it. The enricher and the indexer read only
# media_jobs id and request_key_digest.
RECOGNITION_WITHHELD=('('+ON_REQUEST_CHAT+' AND EXISTS(SELECT FROM media_jobs rj WHERE rj.id=a.id'
    ' AND rj.request_key_digest IS NULL))')
RECOGNITION_REASON='(SELECT orr.reason FROM ('+ON_REQUEST_CHATS+') orr WHERE orr.chat=m.chat)'
# Owner 2026-11-09 («картинки только новые ... только новые обрабатывали в реал тайме»; «отсеять из очереди совсем не
# нужное»): an image (alias a, message m) is described by itself only while it is new. One sent more than
# IMAGE_DESCRIPTION_DAYS ago, or a sticker, waits for an agent's request, recorded as for ANYM-805 in the job's request
# key digest. Description only: text extraction and OCR do not read this rule. Nothing is changed, failed or deleted.
# An attachment without a media job keeps the old behaviour (no request could be recorded for it). No '%' in the text,
# so it is valid in parameterized and plain statements alike.
IMAGE_DESCRIPTION_DAYS=7
IMAGE_STICKER=("EXISTS(SELECT FROM message_metadata smd WHERE smd.message=m.id"
    " AND smd.metadata->>'media_kind'='sticker')")
# ANYM-890 (owner 11-09, «отсеять из очереди совсем не нужное»; groups and channels: «их лучше не надо»): in a group or
# channel chat (ANYM-805's chat kind, subscribed chats included) an audio file whose message names a media kind other
# than voice (music, a recording sent as audio or as a file) is transcribed when an agent asks. Voice notes, personal
# chats, chats of unknown kind and audio whose message names no media kind keep automatic transcription. Video, round
# notes included (media_kind video in both adapters), is not audio and is not read by this rule.
# The chat kind is one set per statement (as ON_REQUEST_CHAT), and it is tested before the per-message metadata probe,
# so that probe runs only for audio in a group or channel (CI #792: a per-row probe of a near-empty message_metadata
# planned as a sequential scan per file).
AUDIO_GROUP_CHAT=("m.chat IN (SELECT gc.id FROM chats gc LEFT JOIN collector_chats gcc ON gcc.account=gc.account"
    " AND gcc.provider_chat=gc.provider_chat WHERE gc.chat_type IN ('group','channel')"
    " OR NOT coalesce(gc.chat_type IN ('private','group','channel'),false) AND gcc.peer_kind IN ('chat','channel'))")
AUDIO_NOT_VOICE=("EXISTS(SELECT FROM message_metadata vmd WHERE vmd.message=m.id"
    " AND vmd.metadata->>'media_kind'<>'voice')")
def audio_mime(mime):
    return "(left("+mime+",6)='audio/' OR "+mime+"='application/ogg')"
def description_shape(mime):
    """An image (by the SQL text `mime`) of message m that is old or a sticker, or non-voice audio in a group (ANYM-890)."""
    return ("(left("+mime+",6)='image/' AND (m.sent_at<statement_timestamp()-interval '"
        +str(IMAGE_DESCRIPTION_DAYS)+" days' OR "+IMAGE_STICKER+") OR "+audio_mime(mime)+" AND "+AUDIO_GROUP_CHAT
        +" AND "+AUDIO_NOT_VOICE+")")


DESCRIPTION_SHAPE=description_shape('a.mime')
# A media job j still downloading has no attachment row yet: its descriptor names the mime (Astra r1 M1).
JOB_DESCRIPTION_SHAPE=description_shape("coalesce(j.descriptor->>'mime','')")
DESCRIPTION_SKIPPED=("("+DESCRIPTION_SHAPE+" AND EXISTS(SELECT FROM media_jobs dj WHERE dj.id=a.id"
    " AND dj.request_key_digest IS NULL))")
# The same rule over an already joined media job `job` (sync status and the health check join it by id).
def description_skipped_row(job):
    return "("+job+".id IS NOT NULL AND "+job+".request_key_digest IS NULL AND "+DESCRIPTION_SHAPE+")"
DESCRIPTION_REASON=("CASE WHEN left(a.mime,6)<>'image/' THEN 'audio_not_a_voice_note' WHEN "+IMAGE_STICKER
    +" THEN 'sticker' ELSE 'image_outside_description_window' END")
# The rule as it applies to one enrichment row e: only work that has not started (unconfigured or queued) waits; a
# lease, a result or a failure keeps its state and its retries (claim, sync status and health apply the same states).
# For statuses, also only where a worker could describe it: queued, or unconfigured with an enabled image (audio)
# profile (with none, the status stays not_configured).
DESCRIPTION_WAITS=("((left(a.mime,6)='image/' OR "+audio_mime('a.mime')+") AND (e.state='queued'"
    " OR coalesce(e.state,'unconfigured')='unconfigured' AND EXISTS(SELECT FROM processing_profiles dpp"
    " WHERE dpp.account=m.account AND dpp.enabled AND dpp.kind=CASE WHEN left(a.mime,6)='image/' THEN 'image'"
    " ELSE 'audio' END)) AND "+DESCRIPTION_SKIPPED+")")
ENRICHMENT_REASON="CASE WHEN "+DESCRIPTION_WAITS+" THEN "+DESCRIPTION_REASON+" END AS description_reason"
# ANYM-987: the vision model's visible text of an image attachment `a` is its enrichment row `e`'s text when that row is
# ready and the text is not blank. The caller's join fences the source (digest and message revision). For an image it
# takes precedence over the OCR text, which applies only without it. Decided at read time, never stored. No '%' in the
# text, so it is valid in parameterized and plain statements alike.
def visible_text_ready(attachment='a',enrichment='e'):
    return ("(left("+attachment+".mime,6)='image/' AND "+enrichment+".state='ready' AND "
        +enrichment+r".text_content ~ '\S')")
VISIBLE_TEXT_READY=visible_text_ready()
# Columns the policy reads (the worker role files grant exactly these).
POLICY_COLUMNS=(('chats','id'),('chats','account'),('chats','provider_chat'),('chats','chat_type'),
    ('collector_chats','account'),('collector_chats','provider_chat'),('collector_chats','peer_kind'),
    ('subscription_sources','subscription'),('subscription_sources','account'),('subscription_sources','chat'),
    ('subscriptions','id'),('subscriptions','state'),('subscriptions','expires_at'),('subscriptions','closed_at'),
    ('media_jobs','id'),('media_jobs','request_key_digest'))


def recognition_withheld(c):
    """RECOGNITION_WITHHELD when this login can read the policy's columns, else 'false' (recognize as before).

    A worker running under its previous grants or on a schema before 62 (no subscriptions.closed_at) keeps
    working instead of failing every claim with a permission or column error; the same fallback as
    media_requests.cold_copy. CASE keeps has_column_privilege from seeing a column that does not exist.
    """
    readable=c.execute('''SELECT bool_and(CASE WHEN EXISTS(SELECT FROM pg_attribute pa
            WHERE pa.attrelid=to_regclass('public.'||v.t) AND pa.attname=v.col AND NOT pa.attisdropped)
        THEN has_column_privilege('public.'||v.t,v.col,'SELECT') ELSE false END) AS readable
        FROM unnest(%s::text[],%s::text[]) v(t,col)''',
        ([t for t,_ in POLICY_COLUMNS],[col for _,col in POLICY_COLUMNS])).fetchone()['readable']
    return RECOGNITION_WITHHELD if readable else 'false'


def description_skipped(withheld):
    """DESCRIPTION_SKIPPED for a worker whose recognition_withheld(c) reads the policy, else 'false' (as before)."""
    return 'false' if withheld=='false' else DESCRIPTION_SKIPPED


# Requires aliases j (media_jobs), m (messages) and ch (chats); adds mp, ecc, ecp, eor and
# el. A chat's history bound is confirmed only when its collector_chats row records
# the bound this run published (NULL=NULL is a confirmed unbounded history): a row a
# live callback created without a bound, one left from an earlier configuration, or
# a missing row is unknown evidence and never yields outside_history_bounds.
# el.claim mirrors Archive.claim_media (what the run downloads by itself; status
# counts). el.occurrence is what one attachment and its request may promise: it is
# unknown whenever the chat's bound is unconfirmed, also for a repeated request.
# Below the bound only an explicit request is claimed (owner 2026-10-23, ANYM-477). In an on-request chat only an
# unexpired request is claimed (ANYM-805); its reason is what the occurrence promises, like collection_excluded.
ELIGIBILITY_JOIN=published_window('m.account')+'''
    LEFT JOIN collector_chats ecc ON ecc.account=m.account AND ecc.provider_chat=ch.provider_chat
        AND ecc.history_since IS NOT DISTINCT FROM mp.since
    LEFT JOIN account_policies ecp ON ecp.account=m.account
    LEFT JOIN ('''+ON_REQUEST_CHATS+''') eor ON eor.chat=m.chat
    CROSS JOIN LATERAL (SELECT claim,CASE WHEN claim IN ('collection_excluded','outside_history_bounds',
            'group_chat_on_request','channel_on_request')
            OR mp.published AND ecc.account IS NOT NULL THEN claim ELSE 'unknown' END AS occurrence,
        CASE WHEN mp.published AND ecc.account IS NOT NULL THEN 'confirmed' ELSE 'unknown' END AS history_bound
        FROM (SELECT CASE WHEN ecp.account IS NULL OR NOT ecp.enabled OR ch.provider_chat=ANY(ecp.exclude_chats)
                OR NOT (ecp.discover_chats OR ch.provider_chat=ANY(ecp.include_chats)) THEN 'collection_excluded'
            WHEN mp.published IS NULL THEN 'unknown'
            WHEN ecc.history_since IS NOT NULL AND m.sent_at<ecc.history_since THEN
                CASE WHEN j.requested_until>clock_timestamp() THEN 'eligible' ELSE 'outside_history_bounds' END
            WHEN mp.since IS NOT NULL AND m.sent_at<mp.since THEN 'history_bound_unknown'
            WHEN eor.chat IS NOT NULL THEN
                CASE WHEN j.requested_until>clock_timestamp() THEN 'eligible' ELSE eor.reason END
            WHEN j.requested_until>clock_timestamp() OR mp.days IS NULL OR mp.days>0
                AND m.sent_at>=clock_timestamp()-mp.days*interval '1 day'
                AND m.sent_at<=clock_timestamp()+interval '5 minutes' THEN 'eligible'
            ELSE 'outside_prefetch_window' END AS claim) classified) el'''
