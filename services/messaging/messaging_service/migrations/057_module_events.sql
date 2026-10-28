-- rollout: manual
-- ANYM-562: module events (calendar, calls) get a journal of their own, next to the message journal
-- and never inside it (design a2, docs/plans/cross-module-subscriptions.md). The module journal has
-- its own gapless counter, so a module publish never takes the feed_counter lock every message hint
-- shares; the message matcher, get_updates, retention and older runtimes never read these tables.
-- A subscription's module part is a second stream with its own positions next to matched_through.
-- Manual because of the definer functions, the rollback fence trigger and the grants.
SET LOCAL lock_timeout='2s';

CREATE TABLE module_event_counter (
 id smallint PRIMARY KEY CHECK(id=1),
 value bigint NOT NULL CHECK(value>=0)
);
INSERT INTO module_event_counter VALUES(1,0);

-- One row per CloudEvents event; references only (the publish function checks the per-type keys).
-- event_key is the CloudEvents id '<module>:<module seq>' and the idempotency key of a retry.
CREATE TABLE module_events (
 seq bigint PRIMARY KEY CHECK(seq>0),
 event_key text NOT NULL UNIQUE CHECK(event_key ~ '^(calendar|calls):[1-9][0-9]{0,17}$'),
 source text NOT NULL CHECK(source IN ('deedkit:calendar','deedkit:calls')),
 type text NOT NULL CHECK(type IN ('calendar.event.created','calendar.event.changed','calendar.event.cancelled',
  'calendar.event.starting','call.finished','call.failed','call.not_answered','call.interrupted','call.owner_request')),
 subject text NOT NULL CHECK(subject ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
 scope_ref text NOT NULL CHECK(scope_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$'),
 occurred_at timestamptz NOT NULL,
 data jsonb NOT NULL CHECK(jsonb_typeof(data)='object' AND octet_length(data::text)<=2048),
 urgency text NOT NULL CHECK(urgency IN ('now','batch')),
 expires_at timestamptz,
 published_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(split_part(event_key,':',1)=split_part(source,':',2)),
 CHECK(split_part(type,'.',1)=CASE source WHEN 'deedkit:calendar' THEN 'calendar' ELSE 'call' END)
);
REVOKE ALL ON module_events,module_event_counter FROM PUBLIC;

-- Module sources of a subscription: a module's scope reference and the selected types. Written only
-- by messaging code (ANYM-564 adds the link check that lets a key name them).
CREATE TABLE subscription_module_sources (
 subscription uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 source text NOT NULL CHECK(source IN ('deedkit:calendar','deedkit:calls')),
 scope_ref text NOT NULL CHECK(scope_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$'),
 types text[] NOT NULL CHECK(cardinality(types) BETWEEN 1 AND 16),
 PRIMARY KEY(subscription,source,scope_ref)
);
-- The module stream of a subscription: the module journal's positions, beside the message ones.
-- rollback_hold: set by the fence trigger below when code that does not know module streams
-- moves the subscription to active; only module-aware code clears it.
CREATE TABLE subscription_module_streams (
 subscription uuid PRIMARY KEY REFERENCES subscriptions(id) ON DELETE CASCADE,
 start_position bigint NOT NULL CHECK(start_position>=0),
 matched_through bigint NOT NULL CHECK(matched_through>=start_position),
 acked_through bigint NOT NULL CHECK(acked_through>=0),
 hinted_through bigint NOT NULL CHECK(hinted_through>=0),
 retention_floor bigint NOT NULL DEFAULT 0 CHECK(retention_floor>=0),
 blocked_reason text CHECK(blocked_reason IN ('match_budget','journal_gap')),
 gap jsonb CHECK(gap IS NULL OR (jsonb_typeof(gap)='object' AND octet_length(gap::text)<=4096)),
 missing_evidence bigint NOT NULL DEFAULT 0 CHECK(missing_evidence>=0),
 rollback_hold boolean NOT NULL DEFAULT false,
 last_matched_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE subscription_module_matches (
 subscription uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
 event bigint NOT NULL REFERENCES module_events(seq),
 matched_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(subscription,event)
);
-- A module the router cannot reach: per source, self-clearing, never the matcher's blocked_reason.
CREATE TABLE module_source_health (
 source text PRIMARY KEY CHECK(source IN ('deedkit:calendar','deedkit:calls')),
 unreachable_since timestamptz,
 observed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
REVOKE ALL ON subscription_module_sources,subscription_module_streams,subscription_module_matches,
 module_source_health FROM PUBLIC;

-- Receipts and the outbox carry the module stream's positions apart from the message ones.
ALTER TABLE subscription_receipts ADD COLUMN module_after_position bigint CHECK(module_after_position>=0);
ALTER TABLE subscription_receipts ADD COLUMN module_through_position bigint
 CHECK(module_through_position>=module_after_position);
-- kind 'module_events': a hint whose through_position is a module journal position. NOT VALID: the
-- new list contains the old one, so no history scan under the lock.
ALTER TABLE subscription_outbox DROP CONSTRAINT subscription_outbox_kind_check;
ALTER TABLE subscription_outbox ADD CONSTRAINT subscription_outbox_kind_check
 CHECK(kind IN ('events','test','silence','module_events')) NOT VALID;
SET LOCAL lock_timeout=DEFAULT;

-- The only module journal write. Fixed search_path, no PUBLIC execute; it inserts one module_events
-- row and touches no message table, link, source or subscription. A retry with the same id and the
-- same fields returns the stored seq; the same id with other fields is refused. The counter row is
-- locked first, so concurrent publishers serialize on it and a failed insert rolls the step back:
-- the sequence stays gapless.
CREATE FUNCTION public.publish_module_event(p_id text, p_source text, p_type text, p_subject text,
  p_scope_ref text, p_time timestamptz, p_data jsonb, p_urgency text, p_expires_at timestamptz)
 RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE allowed text[]; stored public.module_events; seq bigint;
BEGIN
 allowed := CASE p_type
  WHEN 'calendar.event.created' THEN ARRAY['calendar_ref','event_ref','start_at','version']
  WHEN 'calendar.event.changed' THEN ARRAY['calendar_ref','event_ref','start_at','version','changed']
  WHEN 'calendar.event.cancelled' THEN ARRAY['calendar_ref','event_ref','start_at']
  WHEN 'calendar.event.starting' THEN ARRAY['calendar_ref','event_ref','start_at','lead_minutes']
  WHEN 'call.finished' THEN ARRAY['call_id','status','end_reason','duration_s','outcome','transcript']
  WHEN 'call.failed' THEN ARRAY['call_id','status','end_reason']
  WHEN 'call.not_answered' THEN ARRAY['call_id','end_reason']
  WHEN 'call.interrupted' THEN ARRAY['call_id','end_reason']
  WHEN 'call.owner_request' THEN ARRAY['call_id','decision_id','state'] END;
 IF allowed IS NULL OR p_data IS NULL OR jsonb_typeof(p_data)<>'object'
   OR EXISTS(SELECT FROM jsonb_object_keys(p_data) k WHERE k<>ALL(allowed))
   OR EXISTS(SELECT FROM jsonb_each(p_data) e WHERE jsonb_typeof(e.value) NOT IN ('string','number','array')
     OR jsonb_typeof(e.value)='string' AND length(e.value#>>'{}')>128)
   OR p_type='call.owner_request' AND (p_urgency IS DISTINCT FROM 'now' OR p_expires_at IS NULL)
   OR p_type='calendar.event.starting' AND (p_expires_at IS NULL
     OR NOT pg_input_is_valid(coalesce(p_data->>'start_at',''),'timestamptz')
     OR p_expires_at<>(p_data->>'start_at')::timestamptz) THEN
  RAISE EXCEPTION 'invalid_module_event' USING ERRCODE='check_violation';
 END IF;
 PERFORM 1 FROM public.module_event_counter WHERE id=1 FOR UPDATE;
 SELECT * INTO stored FROM public.module_events WHERE event_key=p_id;
 IF FOUND THEN
  IF (stored.source,stored.type,stored.subject,stored.scope_ref,stored.occurred_at,stored.data,stored.urgency,stored.expires_at)
    IS NOT DISTINCT FROM (p_source,p_type,p_subject,p_scope_ref,p_time,p_data,p_urgency,p_expires_at) THEN
   RETURN stored.seq;
  END IF;
  RAISE EXCEPTION 'module_event_conflict' USING ERRCODE='unique_violation';
 END IF;
 UPDATE public.module_event_counter SET value=value+1 WHERE id=1 RETURNING value INTO seq;
 INSERT INTO public.module_events(seq,event_key,source,type,subject,scope_ref,occurred_at,data,urgency,expires_at)
  VALUES(seq,p_id,p_source,p_type,p_subject,p_scope_ref,p_time,p_data,p_urgency,p_expires_at);
 RETURN seq;
END $$;
REVOKE ALL ON FUNCTION public.publish_module_event(text,text,text,text,text,timestamptz,jsonb,text,timestamptz)
 FROM PUBLIC;

-- The router's reachability report: the first failure keeps its time, a success clears it.
CREATE FUNCTION public.report_module_reachability(p_source text, p_reachable boolean) RETURNS void
 LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 INSERT INTO public.module_source_health(source,unreachable_since)
  VALUES(p_source,CASE WHEN p_reachable THEN NULL ELSE clock_timestamp() END)
 ON CONFLICT(source) DO UPDATE SET observed_at=clock_timestamp(),
  unreachable_since=CASE WHEN p_reachable THEN NULL
   ELSE coalesce(public.module_source_health.unreachable_since,clock_timestamp()) END $$;
REVOKE ALL ON FUNCTION public.report_module_reachability(text,boolean) FROM PUBLIC;

-- Rollback fence: code that knows module streams sets messaging.module_aware for the transaction
-- that makes a subscription active. Any other transition to active (an older runtime's resume)
-- holds the module stream, so the older runtime cannot bring a module definition back to life.
CREATE FUNCTION public.subscription_module_fence() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
BEGIN
 IF current_setting('messaging.module_aware',true) IS DISTINCT FROM 'on' THEN
  UPDATE public.subscription_module_streams SET rollback_hold=true WHERE subscription=NEW.id;
 END IF;
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.subscription_module_fence() FROM PUBLIC;
CREATE TRIGGER subscription_module_fence AFTER UPDATE OF state ON subscriptions
 FOR EACH ROW WHEN (NEW.state='active' AND OLD.state IS DISTINCT FROM 'active')
 EXECUTE FUNCTION public.subscription_module_fence();

-- Worker grants for an installed role (the role files repeat them for a new installation).
DO $$ BEGIN
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_subscriptions') THEN
  GRANT SELECT ON module_events,module_event_counter,subscription_module_sources,module_source_health
   TO messaging_subscriptions;
  GRANT SELECT,INSERT,DELETE ON subscription_module_matches TO messaging_subscriptions;
  GRANT SELECT ON subscription_module_streams TO messaging_subscriptions;
  GRANT UPDATE(matched_through,hinted_through,retention_floor,blocked_reason,gap,missing_evidence,last_matched_at)
   ON subscription_module_streams TO messaging_subscriptions;
 END IF;
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT SELECT ON module_events,module_event_counter,subscription_module_sources,subscription_module_matches,
   module_source_health TO messaging_gateway;
  GRANT SELECT,INSERT ON subscription_module_streams TO messaging_gateway;
  GRANT UPDATE(matched_through,acked_through,gap,blocked_reason,rollback_hold) ON subscription_module_streams
   TO messaging_gateway;
 END IF;
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_event_delivery') THEN
  GRANT SELECT ON module_events,subscription_module_matches,subscription_module_streams TO messaging_event_delivery;
 END IF;
END $$;
