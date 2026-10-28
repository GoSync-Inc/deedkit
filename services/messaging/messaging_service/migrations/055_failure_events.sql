-- rollout: manual
-- ANYM-535: send.failed and source.health journal rows. Neither names an archived message: every
-- reader outside the subscription path joins messages (update_queries.py), so these rows stay
-- visible only to subscriptions that select them. Additive: older runtimes write and read neither
-- kind. Manual because of the definer functions, the collector guard and the matcher grant.
SET LOCAL lock_timeout='2s';
ALTER TABLE events ALTER COLUMN message DROP NOT NULL;

-- The matcher's memory of the last source.health status journaled per account, with the connection
-- change time and lease epoch it saw. Only journal_source_health writes it, never a collector, so
-- no collector row is locked for this bookkeeping. Derived state: not in the backup manifest's table
-- inventory; a full-database dump carries it, and a restore's quarantine closes every subscription.
CREATE TABLE source_health_journal (
 account text PRIMARY KEY REFERENCES accounts(id) ON DELETE CASCADE,
 status text NOT NULL CHECK(status IN ('connected','disconnected','auth_required','stale')),
 connection_changed_at text,
 lease_epoch bigint,
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON source_health_journal FROM PUBLIC;

-- The account's health class from its collector record and lease. auth_required: the connection is
-- blocked until the owner relinks; disconnected: no live lease, a stopped collector or a connection
-- that is not connected; stale: connected, but no provider event (a committed live capture: message,
-- receipt, read state or deletion) since the later of the connect and the last event for longer
-- than the threshold; else connected.
CREATE FUNCTION public.source_health_status(p_operations jsonb, p_status text, p_lease_expires timestamptz,
  p_stale_seconds integer) RETURNS text
 LANGUAGE sql VOLATILE SET search_path=pg_catalog AS $$
 SELECT CASE
  WHEN p_operations->'connection'->>'state'='blocked'
   AND p_operations->'connection'->>'next_action'='relink_in_owner_terminal' THEN 'auth_required'
  WHEN p_lease_expires IS NULL OR p_lease_expires<=clock_timestamp() OR p_status='stopped'
   OR p_operations->'connection'->>'state' IS DISTINCT FROM 'connected' THEN 'disconnected'
  WHEN coalesce(greatest(
    CASE WHEN pg_input_is_valid(coalesce(p_operations->'connection'->>'changed_at',''),'timestamptz')
     THEN (p_operations->'connection'->>'changed_at')::timestamptz END,
    CASE WHEN pg_input_is_valid(coalesce(p_operations->'live_capture'->'last_commit'->>'committed_by',''),'timestamptz')
     THEN (p_operations->'live_capture'->'last_commit'->>'committed_by')::timestamptz END),
   '-infinity')<clock_timestamp()-make_interval(secs=>p_stale_seconds) THEN 'stale'
  ELSE 'connected' END $$;
REVOKE ALL ON FUNCTION public.source_health_status(jsonb,text,timestamptz,integer) FROM PUBLIC;

-- The matcher's only journal write: source.health rows (account and status only) for each account
-- whose class changed since the last call. A connection that changed (its change time or the lease
-- epoch moved) while both samples are connected or stale lost its connection in between: that is
-- journaled as disconnected, then the current class. Any other change reverted between two calls
-- collapses: the status is sampled (every 30 s by the matcher). The first observation is a baseline. Reads no
-- row lock a collector holds and locks no collector row; the feed counter is the publishers' shared
-- serialization, and the caller bounds the whole transaction (subscriptions.journal_source_health).
CREATE FUNCTION public.journal_source_health(p_stale_seconds integer) RETURNS integer
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE r record; seq bigint; status text; statuses text[]; journaled integer := 0;
BEGIN
 IF p_stale_seconds IS NULL OR p_stale_seconds NOT BETWEEN 60 AND 86400 THEN
  RAISE EXCEPTION 'invalid_stale_threshold';
 END IF;
 -- One caller at a time; a concurrent matcher skips this call.
 IF NOT pg_try_advisory_xact_lock(hashtextextended('source-health-journal',0)) THEN
  RETURN 0;
 END IF;
 FOR r IN SELECT h.account,j.status AS previous,j.connection_changed_at AS previous_change,
   j.lease_epoch AS previous_epoch,h.operations->'connection'->>'changed_at' AS change,l.epoch,
   public.source_health_status(h.operations,h.status,l.expires_at,p_stale_seconds) AS current
  FROM public.collector_health h LEFT JOIN public.collector_leases l ON l.account=h.account
  LEFT JOIN public.source_health_journal j ON j.account=h.account ORDER BY h.account LOOP
  CONTINUE WHEN r.previous IS NOT DISTINCT FROM r.current AND r.previous_change IS NOT DISTINCT FROM r.change
   AND r.previous_epoch IS NOT DISTINCT FROM r.epoch;
  statuses := '{}';
  IF r.previous IS NOT NULL THEN
   IF r.previous IN ('connected','stale') AND r.current IN ('connected','stale')
     AND (r.previous_change IS DISTINCT FROM r.change OR r.previous_epoch IS DISTINCT FROM r.epoch) THEN
    statuses := ARRAY['disconnected',r.current];
   ELSIF r.previous IS DISTINCT FROM r.current THEN
    statuses := ARRAY[r.current];
   END IF;
  END IF;
  FOREACH status IN ARRAY statuses LOOP
   UPDATE public.feed_counter SET value=value+1 WHERE id=1 RETURNING value INTO seq;
   INSERT INTO public.events(id,account,message,kind,revision,origin,snapshot)
    VALUES(seq,r.account,NULL,'source.health',1,'live',jsonb_build_object('account',r.account,'status',status));
   journaled := journaled+1;
  END LOOP;
  INSERT INTO public.source_health_journal(account,status,connection_changed_at,lease_epoch)
   VALUES(r.account,r.current,r.change,r.epoch) ON CONFLICT(account) DO UPDATE SET status=excluded.status,
   connection_changed_at=excluded.connection_changed_at,lease_epoch=excluded.lease_epoch,
   observed_at=clock_timestamp();
 END LOOP;
 RETURN journaled;
END $$;
REVOKE ALL ON FUNCTION public.journal_source_health(integer) FROM PUBLIC;

-- 037's guard, plus one shape: a bound collector may journal a message-less send.failed row only for
-- a send of its own account that is failed or unknown, under that send's own principal and key, so
-- it cannot wake another key or account. Every other check is unchanged.
CREATE OR REPLACE FUNCTION public.collector_account_guard() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $$
DECLARE bounds text[] := array_remove(ARRAY[public.collector_bound_account(current_user::text),
  public.collector_bound_account(session_user::text)],NULL);
 bound text; side record; pass integer;
BEGIN
 FOREACH bound IN ARRAY bounds LOOP
  FOR pass IN 1..2 LOOP
   IF pass=1 THEN
    CONTINUE WHEN TG_OP='INSERT'; side := OLD;
   ELSE
    CONTINUE WHEN TG_OP='DELETE'; side := NEW;
   END IF;
   -- Nested branches: PL/pgSQL resolves every record field named in one expression.
   IF TG_ARGV[0] IN ('account','account_message') THEN
    IF side.account IS DISTINCT FROM bound THEN
     RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
    END IF;
   END IF;
   IF TG_ARGV[0] IN ('message','account_message') THEN
    IF TG_TABLE_NAME='events' AND TG_OP='INSERT' AND side.message IS NULL THEN
     IF side.kind IS DISTINCT FROM 'send.failed' OR NOT EXISTS(SELECT FROM public.outgoing_messages o
       WHERE o.id=side.snapshot->>'send_id' AND o.account=bound AND o.status IN ('failed','unknown')
       AND o.chat=side.snapshot->>'chat' AND o.principal=side.snapshot->>'principal'
       AND o.key_id::text=side.snapshot->>'key_id') THEN
      RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
     END IF;
    ELSIF NOT EXISTS(SELECT FROM public.messages m WHERE m.id=side.message AND m.account=bound) THEN
     RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
    END IF;
   ELSIF TG_ARGV[0]='job' THEN
    IF NOT EXISTS(SELECT FROM public.media_jobs j WHERE j.id=side.job AND j.account=bound) THEN
     RAISE EXCEPTION 'collector_account_mismatch' USING ERRCODE='insufficient_privilege';
    END IF;
   END IF;
  END LOOP;
 END LOOP;
 IF TG_OP='DELETE' THEN RETURN OLD; END IF;
 RETURN NEW;
END $$;

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
