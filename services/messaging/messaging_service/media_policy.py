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


# Requires aliases j (media_jobs), m (messages) and ch (chats); adds mp, ecc, ecp and
# el. A chat's history bound is confirmed only when its collector_chats row records
# the bound this run published (NULL=NULL is a confirmed unbounded history): a row a
# live callback created without a bound, one left from an earlier configuration, or
# a missing row is unknown evidence and never yields outside_history_bounds.
# el.claim mirrors Archive.claim_media (what the run downloads by itself; status
# counts). el.occurrence is what one attachment and its request may promise: it is
# unknown whenever the chat's bound is unconfirmed, also for a repeated request.
ELIGIBILITY_JOIN=published_window('m.account')+'''
    LEFT JOIN collector_chats ecc ON ecc.account=m.account AND ecc.provider_chat=ch.provider_chat
        AND ecc.history_since IS NOT DISTINCT FROM mp.since
    LEFT JOIN account_policies ecp ON ecp.account=m.account
    CROSS JOIN LATERAL (SELECT claim,CASE WHEN claim IN ('collection_excluded','outside_history_bounds')
            OR mp.published AND ecc.account IS NOT NULL THEN claim ELSE 'unknown' END AS occurrence,
        CASE WHEN mp.published AND ecc.account IS NOT NULL THEN 'confirmed' ELSE 'unknown' END AS history_bound
        FROM (SELECT CASE WHEN ecp.account IS NULL OR NOT ecp.enabled OR ch.provider_chat=ANY(ecp.exclude_chats)
                OR NOT (ecp.discover_chats OR ch.provider_chat=ANY(ecp.include_chats)) THEN 'collection_excluded'
            WHEN mp.published IS NULL THEN 'unknown'
            WHEN ecc.history_since IS NOT NULL AND m.sent_at<ecc.history_since THEN 'outside_history_bounds'
            WHEN mp.since IS NOT NULL AND m.sent_at<mp.since THEN 'history_bound_unknown'
            WHEN j.requested_until>clock_timestamp() OR mp.days IS NULL OR mp.days>0
                AND m.sent_at>=clock_timestamp()-mp.days*interval '1 day'
                AND m.sent_at<=clock_timestamp()+interval '5 minutes' THEN 'eligible'
            ELSE 'outside_prefetch_window' END AS claim) classified) el'''
