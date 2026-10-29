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

-- The contract of module_events.validate_event (KINDS), enforced again inside the privileged
-- boundary: every key of the type present (optional ones aside) and no other, each value within
-- its rule (ref, time, token, enumeration, integer range, changed list), the subject equal to the
-- type's subject key, the id's module equal to the source's, data at most 1024 bytes, the
-- urgency and expiry rules, and every time (occurrence, expiry, data) finite and between
-- 1970-01-01 and 9999-12-30 UTC, so a reader can always decode it. Every time arrives as text and
-- passes the text rule of module_events._TIME before any cast (hours 00-23, minutes and seconds
-- 00-59, offsets up to 15:59), so both validators accept the same strings. References only: no
-- free text fits any rule.
CREATE FUNCTION public.module_event_valid(p_id text, p_source text, p_type text, p_subject text,
  p_time text, p_data jsonb, p_urgency text, p_expires_at text) RETURNS boolean
 LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE spec jsonb; rule text; item record; v jsonb; parts text[]; t timestamptz; x timestamptz;
 pattern constant text := '^(19[7-9][0-9]|[2-9][0-9]{3})-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?(Z|[+-](0\d|1[0-5]):[0-5]\d)$';
 specs constant jsonb := '{
  "calendar.event.created": {"subject": "event_ref", "optional": [], "keys": {"calendar_ref": "ref",
   "event_ref": "ref", "start_at": "time", "version": "int:0:9007199254740991"}},
  "calendar.event.changed": {"subject": "event_ref", "optional": [], "keys": {"calendar_ref": "ref",
   "event_ref": "ref", "start_at": "time", "version": "int:0:9007199254740991", "changed": "changed"}},
  "calendar.event.cancelled": {"subject": "event_ref", "optional": [], "keys": {"calendar_ref": "ref",
   "event_ref": "ref", "start_at": "time"}},
  "calendar.event.starting": {"subject": "event_ref", "optional": [], "keys": {"calendar_ref": "ref",
   "event_ref": "ref", "start_at": "time", "lead_minutes": "int:1:1440"}},
  "call.finished": {"subject": "call_id", "optional": ["duration_s", "outcome"], "keys": {"call_id": "ref",
   "status": "enum:finished", "end_reason": "token", "duration_s": "int:0:86400", "outcome": "token",
   "transcript": "enum:final"}},
  "call.failed": {"subject": "call_id", "optional": [], "keys": {"call_id": "ref",
   "status": "enum:failed|finished", "end_reason": "token"}},
  "call.not_answered": {"subject": "call_id", "optional": [], "keys": {"call_id": "ref",
   "end_reason": "enum:no_answer|busy|canceled"}},
  "call.interrupted": {"subject": "call_id", "optional": [], "keys": {"call_id": "ref",
   "end_reason": "enum:service_restart"}},
  "call.owner_request": {"subject": "call_id", "optional": [], "keys": {"call_id": "ref",
   "decision_id": "ref", "state": "enum:pending"}}}';
BEGIN
 -- The casts only after the text is known to parse (no OR evaluation order is relied on).
 IF p_time IS NULL OR p_time !~ pattern OR NOT pg_input_is_valid(p_time,'timestamptz') THEN RETURN false; END IF;
 t := p_time::timestamptz;
 IF p_expires_at IS NOT NULL THEN
  IF p_expires_at !~ pattern OR NOT pg_input_is_valid(p_expires_at,'timestamptz') THEN RETURN false; END IF;
  x := p_expires_at::timestamptz;
 END IF;
 spec := specs->p_type;
 IF spec IS NULL OR p_data IS NULL OR jsonb_typeof(p_data)<>'object' OR octet_length(p_data::text)>1024
   OR p_id IS NULL OR p_id !~ '^(calendar|calls):[1-9][0-9]{0,15}$'
   -- At most 2^53-1: 16 digits without a leading zero compare as text like numbers (no cast, no THEN here).
   OR length(split_part(p_id,':',2))=16 AND split_part(p_id,':',2) COLLATE "C">'9007199254740991'
   OR split_part(p_id,':',1) IS DISTINCT FROM split_part(p_source,':',2)
   OR p_urgency IS NULL OR p_urgency NOT IN ('now','batch')
   OR t NOT BETWEEN '1970-01-01Z' AND '9999-12-30Z'
   OR x NOT BETWEEN '1970-01-01Z' AND '9999-12-30Z'
   OR p_subject IS DISTINCT FROM p_data->>(spec->>'subject')
   OR jsonb_typeof(p_data->(spec->>'subject')) IS DISTINCT FROM 'string' THEN
  RETURN false;
 END IF;
 FOR item IN SELECT k FROM jsonb_object_keys(p_data) k LOOP
  IF NOT spec->'keys' ? item.k THEN RETURN false; END IF;
 END LOOP;
 FOR item IN SELECT e.key,e.value AS r FROM jsonb_each_text(spec->'keys') e LOOP
  v := p_data->item.key;
  IF v IS NULL THEN
   IF NOT spec->'optional' ? item.key THEN RETURN false; END IF;
   CONTINUE;
  END IF;
  rule := item.r;
  IF rule IN ('ref','time','token') OR rule LIKE 'enum:%' THEN
   IF jsonb_typeof(v)<>'string' THEN RETURN false; END IF;
  END IF;
  IF rule='ref' AND v#>>'{}' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    OR rule='token' AND v#>>'{}' !~ '^[a-z][a-z0-9_]{0,47}$'
    OR rule LIKE 'enum:%' AND NOT (v#>>'{}')=ANY(string_to_array(substr(rule,6),'|')) THEN
   RETURN false;
  END IF;
  IF rule='time' THEN
   -- The cast only after the text is known to parse (no OR evaluation order is relied on).
   IF v#>>'{}' !~ pattern
     OR NOT pg_input_is_valid(v#>>'{}','timestamptz') THEN
    RETURN false;
   END IF;
   IF (v#>>'{}')::timestamptz NOT BETWEEN '1970-01-01Z' AND '9999-12-30Z' THEN RETURN false; END IF;
  END IF;
  IF rule LIKE 'int:%' THEN
   parts := string_to_array(rule,':');
   IF jsonb_typeof(v)<>'number' OR (v#>>'{}') !~ '^-?[0-9]+$'
     OR (v#>>'{}')::numeric NOT BETWEEN parts[2]::numeric AND parts[3]::numeric THEN
    RETURN false;
   END IF;
  END IF;
  IF rule='changed' AND (jsonb_typeof(v)<>'array' OR jsonb_array_length(v) NOT BETWEEN 1 AND 3
    OR EXISTS(SELECT FROM jsonb_array_elements(v) e WHERE jsonb_typeof(e)<>'string'
      OR e#>>'{}' NOT IN ('start','end','details'))
    OR (SELECT count(DISTINCT e) FROM jsonb_array_elements(v) e)<>jsonb_array_length(v)) THEN
   RETURN false;
  END IF;
 END LOOP;
 IF p_type='call.owner_request' AND (p_urgency<>'now' OR p_expires_at IS NULL)
   OR p_type='calendar.event.starting' AND (x IS NULL
     OR x<>(p_data->>'start_at')::timestamptz) THEN
  RETURN false;
 END IF;
 RETURN true;
END $$;
REVOKE ALL ON FUNCTION public.module_event_valid(text,text,text,text,text,jsonb,text,text) FROM PUBLIC;

-- The only module journal write. Fixed search_path, no PUBLIC execute; it inserts one module_events
-- row and touches no message table, link, source or subscription. A retry with the same id and the
-- same fields returns the stored seq; the same id with other fields is refused. The counter row is
-- locked first, so concurrent publishers serialize on it and a failed insert rolls the step back:
-- the sequence stays gapless.
CREATE FUNCTION public.publish_module_event(p_id text, p_source text, p_type text, p_subject text,
  p_scope_ref text, p_time text, p_data jsonb, p_urgency text, p_expires_at text)
 RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE stored public.module_events; seq bigint;
BEGIN
 IF NOT public.module_event_valid(p_id,p_source,p_type,p_subject,p_time,p_data,p_urgency,p_expires_at) THEN
  RAISE EXCEPTION 'invalid_module_event' USING ERRCODE='check_violation';
 END IF;
 PERFORM 1 FROM public.module_event_counter WHERE id=1 FOR UPDATE;
 SELECT * INTO stored FROM public.module_events WHERE event_key=p_id;
 IF FOUND THEN
  IF (stored.source,stored.type,stored.subject,stored.scope_ref,stored.occurred_at,stored.data,stored.urgency,stored.expires_at)
    IS NOT DISTINCT FROM (p_source,p_type,p_subject,p_scope_ref,p_time::timestamptz,p_data,p_urgency,
      p_expires_at::timestamptz) THEN
   RETURN stored.seq;
  END IF;
  RAISE EXCEPTION 'module_event_conflict' USING ERRCODE='unique_violation';
 END IF;
 UPDATE public.module_event_counter SET value=value+1 WHERE id=1 RETURNING value INTO seq;
 INSERT INTO public.module_events(seq,event_key,source,type,subject,scope_ref,occurred_at,data,urgency,expires_at)
  VALUES(seq,p_id,p_source,p_type,p_subject,p_scope_ref,p_time::timestamptz,p_data,p_urgency,
   p_expires_at::timestamptz);
 RETURN seq;
END $$;
REVOKE ALL ON FUNCTION public.publish_module_event(text,text,text,text,text,text,jsonb,text,text)
 FROM PUBLIC;

-- The router's reachability report: the first failure keeps its time, a success clears it.
CREATE FUNCTION public.report_module_reachability(p_source text, p_reachable boolean) RETURNS void
 LANGUAGE sql STRICT SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 INSERT INTO public.module_source_health(source,unreachable_since)
  VALUES(p_source,CASE WHEN p_reachable THEN NULL ELSE clock_timestamp() END)
 ON CONFLICT(source) DO UPDATE SET observed_at=clock_timestamp(),
  unreachable_since=CASE WHEN p_reachable THEN NULL
   ELSE coalesce(public.module_source_health.unreachable_since,clock_timestamp()) END $$;
REVOKE ALL ON FUNCTION public.report_module_reachability(text,boolean) FROM PUBLIC;

-- The matcher stages module_events hints only while a dispatcher that knows the kind beats: its
-- heartbeat names its runtime schema (component_heartbeats, schema 50), and an older dispatcher would
-- read a module position as a message position. The matcher cannot read that row (row-level security).
CREATE FUNCTION public.module_hints_ready() RETURNS boolean
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT EXISTS(SELECT FROM public.component_heartbeats WHERE component='dispatcher' AND schema_version>=56
  AND beat_at>clock_timestamp()-interval '120 seconds') $$;
REVOKE ALL ON FUNCTION public.module_hints_ready() FROM PUBLIC;

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
  GRANT EXECUTE ON FUNCTION public.module_hints_ready() TO messaging_subscriptions;
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
