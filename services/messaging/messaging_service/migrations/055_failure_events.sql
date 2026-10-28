-- ANYM-535: send.failed and source.health journal rows. Neither names an archived message: every
-- reader outside the subscription path joins messages (update_queries.py), so these rows stay
-- visible only to subscriptions that select them.
ALTER TABLE events ALTER COLUMN message DROP NOT NULL;
-- The last source.health status journaled for the account; NULL until the first observation, which
-- is a baseline and journals nothing.
ALTER TABLE collector_health ADD COLUMN journaled_health text
 CHECK(journaled_health IN ('connected','disconnected','auth_required','stale'));

-- The account's health class from its collector record and lease. auth_required: the connection is
-- blocked until the owner relinks; disconnected: no live lease, a stopped collector or a connection
-- that is not connected; stale: connected, but neither the connection nor the live capture has
-- confirmed the provider session for longer than the threshold (the capture refreshes about every
-- 15 s while the session is open; the connect time counts, so a reconnect is not stale at once).
CREATE FUNCTION public.source_health_status(p_operations jsonb, p_status text, p_lease_expires timestamptz,
  p_stale_seconds integer) RETURNS text
 LANGUAGE sql VOLATILE SET search_path=pg_catalog AS $$
 SELECT CASE
  WHEN p_operations->'connection'->>'state'='blocked'
   AND p_operations->'connection'->>'next_action'='relink_in_owner_terminal' THEN 'auth_required'
  WHEN p_lease_expires IS NULL OR p_lease_expires<=clock_timestamp() OR p_status='stopped'
   OR p_operations->'connection'->>'state' IS DISTINCT FROM 'connected' THEN 'disconnected'
  WHEN coalesce(greatest(
    CASE WHEN pg_input_is_valid(coalesce(p_operations->'connection'->>'observed_at',''),'timestamptz')
     THEN (p_operations->'connection'->>'observed_at')::timestamptz END,
    CASE WHEN pg_input_is_valid(coalesce(p_operations->'live_capture'->>'observed_at',''),'timestamptz')
     THEN (p_operations->'live_capture'->>'observed_at')::timestamptz END),
   '-infinity')<clock_timestamp()-make_interval(secs=>p_stale_seconds) THEN 'stale'
  ELSE 'connected' END $$;
REVOKE ALL ON FUNCTION public.source_health_status(jsonb,text,timestamptz,integer) FROM PUBLIC;

-- The matcher's only journal write: one source.health row (account and status only) per changed
-- account. Returns the rows journaled. The counter is locked before any health row and those rows
-- are taken with SKIP LOCKED, so a collector never waits on this function; a skipped account is
-- journaled on a later call.
CREATE FUNCTION public.journal_source_health(p_stale_seconds integer) RETURNS integer
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r record; seq bigint; journaled integer := 0;
BEGIN
 IF p_stale_seconds IS NULL OR p_stale_seconds NOT BETWEEN 60 AND 86400 THEN
  RAISE EXCEPTION 'invalid_stale_threshold';
 END IF;
 IF NOT EXISTS(SELECT FROM public.collector_health h LEFT JOIN public.collector_leases l ON l.account=h.account
   WHERE public.source_health_status(h.operations,h.status,l.expires_at,p_stale_seconds)
    IS DISTINCT FROM h.journaled_health) THEN
  RETURN 0;
 END IF;
 PERFORM FROM public.feed_counter WHERE id=1 FOR UPDATE;
 FOR r IN SELECT h.account,h.journaled_health AS previous,
   public.source_health_status(h.operations,h.status,l.expires_at,p_stale_seconds) AS current
  FROM public.collector_health h LEFT JOIN public.collector_leases l ON l.account=h.account
  ORDER BY h.account FOR UPDATE OF h SKIP LOCKED LOOP
  CONTINUE WHEN r.current IS NOT DISTINCT FROM r.previous;
  IF r.previous IS NOT NULL THEN
   UPDATE public.feed_counter SET value=value+1 WHERE id=1 RETURNING value INTO seq;
   INSERT INTO public.events(id,account,message,kind,revision,origin,snapshot)
    VALUES(seq,r.account,NULL,'source.health',1,'live',jsonb_build_object('account',r.account,'status',r.current));
   journaled := journaled+1;
  END IF;
  UPDATE public.collector_health SET journaled_health=r.current WHERE account=r.account;
 END LOOP;
 RETURN journaled;
END $$;
REVOKE ALL ON FUNCTION public.journal_source_health(integer) FROM PUBLIC;

-- 049's reference function, plus the send ID and status code of the ANYM-535 kinds (no error code,
-- no text). CREATE OR REPLACE keeps the owner and the delivery role's EXECUTE grant.
CREATE OR REPLACE FUNCTION subscription_hint_references(p_subscription uuid, p_after bigint, p_through bigint,
  p_accounts text[], p_chats text[])
 RETURNS TABLE(event bigint, kind text, origin text, account text, created_at timestamptz, evidence jsonb, total bigint)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT e.id,e.kind,e.origin,e.account,e.created_at,
  jsonb_strip_nulls(jsonb_build_object('chat',e.snapshot->'chat','sender',e.snapshot->'sender',
   'message',e.snapshot->'message','direction',e.snapshot->'direction',
   'metadata',jsonb_strip_nulls(jsonb_build_object('mentions_owner',e.snapshot->'metadata'->'mentions_owner',
    'reply_to_owner',e.snapshot->'metadata'->'reply_to_owner',
    'reply_to_provider_id',e.snapshot->'metadata'->'reply_to_provider_id')),
   'reply_evidence',jsonb_strip_nulls(jsonb_build_object('basis',e.snapshot->'reply_evidence'->'basis')),
   'send_id',CASE WHEN e.kind='send.failed' THEN e.snapshot->'send_id' END,
   'status',CASE WHEN e.kind IN ('send.failed','source.health') THEN e.snapshot->'status' END)),
  count(*) OVER ()
 FROM public.subscription_matches m JOIN public.events e ON e.id=m.event
 WHERE m.subscription=p_subscription AND m.event>p_after AND m.event<=p_through AND e.account=ANY(p_accounts)
  AND (p_chats IS NULL OR e.snapshot->>'chat'=ANY(p_chats))
 ORDER BY m.event LIMIT 20 $$;
REVOKE ALL ON FUNCTION subscription_hint_references(uuid,bigint,bigint,text[],text[]) FROM PUBLIC;
