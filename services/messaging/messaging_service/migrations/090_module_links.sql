-- rollout: manual
-- ANYM-564: a key sees call events only through a link the operator set for its principal (calls:own).
-- module_links holds one link per principal and module (module 'calls' only in this card): the calls principal whose
-- calls the key may be woken by, a generation and a state. subscription_module_links records the generation each call
-- source was admitted under; a call source is live only while its principal's link is active with that generation
-- and names the source's scope_ref (module_links.py). Revoking, marking stale or regranting moves the generation, so a
-- source, its matches and its queued hints never come back, even after a regrant. Generations come from the clock
-- (microseconds), never below the previous one plus one, so a regrant after a restore from backup still passes every
-- generation issued before the restore.
-- Functions (definer, fixed search path, no PUBLIC execute):
--  set_module_link: the only writer of module_links; takes the principal's ACL lock (policy.lock_principal), so a
--      delivery that checked the link under the shared lock finishes first, and expires the principal's open receipts
--      with a module range, so no receipt issued before a change ACKs past it. Owner CLI only; no role is granted it.
--  admit_module_source: the API's only way to give a subscription a call source: it checks the link of the
--      subscription's own principal and records the generation. Granted to the gateway role (gateway_role.sql).
--  publish_module_event (056's text, same signature and ACL) additionally marks the active links naming the event's
--      scope stale when it stores a call.access.changed event (the calls-side journal of an ownership or permission
--      change); module_event_valid (067's text) admits that type: subject principal_ref, urgency now, no expiry.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": one CHECK widening pair on one column,
-- a superset of 067's list, as 064 and 085; two new tables; CREATE OR REPLACE of two functions with the same
-- signatures; no row is read, rewritten or deleted). Grants only on the new objects, in the role files. A schema-89
-- runtime never names the new tables or type; no producer journals call.access.changed yet.
SET LOCAL lock_timeout='2s';

ALTER TABLE module_events DROP CONSTRAINT module_events_type_check;
ALTER TABLE module_events ADD CONSTRAINT module_events_type_check CHECK(type IN ('calendar.event.created','calendar.event.changed','calendar.event.cancelled',
  'calendar.event.starting','call.finished','call.failed','call.not_answered','call.interrupted','call.owner_request',
  'recording.text.archived','recording.access.changed','call.access.changed'));
SET LOCAL lock_timeout=DEFAULT;

CREATE TABLE module_links (
 principal text NOT NULL REFERENCES principals(id),
 module text NOT NULL CHECK(module='calls'),
 module_principal text NOT NULL CHECK(module_principal ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$'),
 generation bigint NOT NULL CHECK(generation>0),
 state text NOT NULL CHECK(state IN ('active','revoked','stale')),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(principal,module)
);
CREATE INDEX module_links_scope ON module_links(module,module_principal);
CREATE TABLE subscription_module_links (
 subscription uuid NOT NULL,
 source text NOT NULL,
 scope_ref text NOT NULL,
 generation bigint NOT NULL CHECK(generation>0),
 PRIMARY KEY(subscription,source,scope_ref),
 FOREIGN KEY(subscription,source,scope_ref) REFERENCES subscription_module_sources(subscription,source,scope_ref)
  ON DELETE CASCADE
);

-- 067's text; only the call.access.changed spec and its urgency rule are new.
CREATE OR REPLACE FUNCTION public.module_event_valid(p_id text, p_source text, p_type text, p_subject text,
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
  "call.failed": {"subject": "call_id", "optional": ["provider_cause", "provider_code"], "keys": {"call_id": "ref",
   "status": "enum:failed|finished", "end_reason": "token", "provider_cause": "token",
   "provider_code": "int:1:999999"}},
  "call.not_answered": {"subject": "call_id", "optional": [], "keys": {"call_id": "ref",
   "end_reason": "enum:no_answer|busy|canceled"}},
  "call.interrupted": {"subject": "call_id", "optional": [], "keys": {"call_id": "ref",
   "end_reason": "enum:service_restart"}},
  "call.owner_request": {"subject": "call_id", "optional": [], "keys": {"call_id": "ref",
   "decision_id": "ref", "state": "enum:pending"}},
  "recording.text.archived": {"subject": "recording_ref", "optional": [], "keys": {"recording_ref": "ref",
   "account_ref": "ref", "revision": "int:0:9007199254740991"}},
  "recording.access.changed": {"subject": "account_ref", "optional": [], "keys": {"account_ref": "ref",
   "principal_ref": "ref", "reason": "enum:permission_removed|account_removed|namespace_replaced"}},
  "call.access.changed": {"subject": "principal_ref", "optional": [], "keys": {"principal_ref": "ref",
   "reason": "enum:permission_removed|principal_removed|owner_changed"}}}';
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
   OR p_id IS NULL OR p_id !~ '^(calendar|calls|recordings):[1-9][0-9]{0,15}$'
   -- At most 2^53-1: 16 digits without a leading zero compare as text like numbers (no cast, no THEN here).
   OR length(split_part(p_id,':',2))=16 AND split_part(p_id,':',2) COLLATE "C">'9007199254740991'
   OR split_part(p_id,':',1) IS DISTINCT FROM split_part(p_source,':',2)
   -- A recording type belongs to the recordings source and only to it; the calls and calendar rules above are 065's.
   OR (p_type LIKE 'recording.%') IS DISTINCT FROM (p_source = 'deedkit:recordings')
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
 -- ANYM-695: Twilio's code is only meaningful beside its cause.
 IF p_type='call.failed' AND p_data ? 'provider_code' AND NOT p_data ? 'provider_cause' THEN RETURN false; END IF;
 IF p_type='call.owner_request' AND (p_urgency<>'now' OR p_expires_at IS NULL)
   OR p_type='calendar.event.starting' AND (x IS NULL
     OR x<>(p_data->>'start_at')::timestamptz)
   OR p_type='recording.text.archived' AND (p_urgency<>'batch' OR p_expires_at IS NOT NULL)
   OR p_type='recording.access.changed' AND (p_urgency<>'now' OR p_expires_at IS NOT NULL)
   OR p_type='call.access.changed' AND (p_urgency<>'now' OR p_expires_at IS NOT NULL) THEN
  RETURN false;
 END IF;
 RETURN true;
END $$;


CREATE FUNCTION public.set_module_link(p_principal text, p_module text, p_module_principal text, p_state text)
 RETURNS public.module_links LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE result public.module_links;
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('acl:'||p_principal,0));
 INSERT INTO public.module_links AS l(principal,module,module_principal,generation,state)
  VALUES(p_principal,p_module,p_module_principal,(extract(epoch FROM clock_timestamp())*1000000)::bigint,p_state)
  ON CONFLICT(principal,module) DO UPDATE SET module_principal=excluded.module_principal,
   generation=greatest(l.generation+1,excluded.generation),state=excluded.state,updated_at=clock_timestamp()
  RETURNING * INTO result;
 UPDATE public.subscription_receipts r SET expires_at=clock_timestamp() FROM public.subscriptions s
  WHERE r.subscription=s.id AND s.principal=p_principal AND r.ack_result IS NULL
  AND r.module_through_position IS NOT NULL AND r.expires_at>clock_timestamp();
 RETURN result;
END $$;
REVOKE ALL ON FUNCTION public.set_module_link(text,text,text,text) FROM PUBLIC;

CREATE FUNCTION public.admit_module_source(p_subscription uuid, p_source text, p_scope_ref text, p_types text[])
 RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE link public.module_links;
BEGIN
 IF p_types IS NULL OR cardinality(p_types) NOT BETWEEN 1 AND 16 OR array_position(p_types,NULL) IS NOT NULL
   OR NOT p_types <@ ARRAY['call.finished','call.failed','call.not_answered','call.interrupted','call.owner_request',
     'call.access.changed'] THEN
  RAISE EXCEPTION 'invalid_filter' USING ERRCODE='check_violation';
 END IF;
 SELECT l.* INTO link FROM public.subscriptions s JOIN public.module_links l ON l.principal=s.principal AND l.module='calls'
  WHERE s.id=p_subscription AND p_source='deedkit:calls' AND l.state='active' AND l.module_principal=p_scope_ref
  FOR SHARE OF l;
 IF NOT FOUND THEN
  RAISE EXCEPTION 'module_not_linked' USING ERRCODE='insufficient_privilege';
 END IF;
 INSERT INTO public.subscription_module_sources(subscription,source,scope_ref,types)
  VALUES(p_subscription,p_source,p_scope_ref,p_types)
  ON CONFLICT(subscription,source,scope_ref) DO UPDATE SET types=excluded.types;
 INSERT INTO public.subscription_module_links(subscription,source,scope_ref,generation)
  VALUES(p_subscription,p_source,p_scope_ref,link.generation)
  ON CONFLICT(subscription,source,scope_ref) DO UPDATE SET generation=excluded.generation;
 RETURN link.generation;
END $$;
REVOKE ALL ON FUNCTION public.admit_module_source(uuid,text,text,text[]) FROM PUBLIC;

-- 056's text; only the call.access.changed step after the insert is new. Same signature, so the ACL stays.
CREATE OR REPLACE FUNCTION public.publish_module_event(p_id text, p_source text, p_type text, p_subject text,
  p_scope_ref text, p_time text, p_data jsonb, p_urgency text, p_expires_at text)
 RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE stored public.module_events; seq bigint; link record;
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
 -- ANYM-564: the calls service journaled an ownership or permission change of this calls principal; every active
 -- link naming it goes stale (a new generation) until the operator links again.
 IF p_type='call.access.changed' THEN
  FOR link IN SELECT principal FROM public.module_links WHERE module='calls' AND module_principal=p_scope_ref
    AND state='active' ORDER BY principal LOOP
   PERFORM public.set_module_link(link.principal,'calls',p_scope_ref,'stale');
  END LOOP;
 END IF;
 RETURN seq;
END $$;
