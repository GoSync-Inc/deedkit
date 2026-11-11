-- rollout: manual
-- ANYM-577: calendar events reach a key only while its module permission holds. The calendar service journals
-- module.access.changed (subject principal_ref: the calendar principal; scope_ref: `calendar:<calendar_ref>`, stamped by the
-- calendar service; data: access details|none, a reason and an optional until) whenever a principal's effective access
-- (the full agenda:read predicate with details visibility) to one calendar changes; messaging reads entitlement from that
-- journal alone (module_links.py: computed, never written, no remote call). A calendar link is module_links(module
-- 'calendar'): its module_principal is the calendar principal, and a calendar source is live only while the link is
-- active with the generation the source was admitted under and the journal holds no access:none event for
-- (principal, calendar) after the subscription's stream start, and its latest event for them is access:details and
-- unexpired.
-- Inventory (every place that names a module or a type; eight CHECKs and one function, as 091's, now with 091's text):
--  module_events: type_check (091's list plus module.access.changed) and check1 (source-to-type: the new type belongs to
--      deedkit:calendar); event_key, source: unchanged (no new source). module_links: module CHECK widened to
--      ('calls','calendar'). module_event_valid rebuilt from 091's text: the module.access.changed spec, its type-to-source
--      rule and its urgency rule (now, no expiry) are new; every other rule is 091's, unchanged. A partial index finds
--      the access events by scope. publish_module_event names no type: kept.
-- Each replacement keeps its constraint's name and widens its list: every row the old constraints admitted is admitted
-- now. Removing the old constraint and adding the widened one share this transaction under a 2 s lock timeout.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": constraint replacement pairs, one
-- partial index on a small table, a CREATE OR REPLACE of a PL/pgSQL function that writes nothing and an AFTER INSERT
-- trigger on module_events with its definer function, which only ends open receipts; no row is read, rewritten or
-- deleted by the migration; no table, column, role or privilege changes). Repeatable (module tests re-run migrations).
-- A schema-92 runtime refuses this schema (database_newer_than_runtime); no producer journals the type before the
-- calendar's schema 10 is released.
SET LOCAL lock_timeout='2s';

ALTER TABLE module_events DROP CONSTRAINT module_events_type_check;
ALTER TABLE module_events ADD CONSTRAINT module_events_type_check CHECK(type IN ('calendar.event.created','calendar.event.changed','calendar.event.cancelled',
  'calendar.event.starting','call.finished','call.failed','call.not_answered','call.interrupted','call.owner_request',
  'recording.text.archived','recording.access.changed','call.access.changed','module.access.changed',
  'feedback.received','feedback.accepted','feedback.declined','feedback.duplicate','feedback.fixed','feedback.verify_requested'));
ALTER TABLE module_events DROP CONSTRAINT module_events_check1;
ALTER TABLE module_events ADD CONSTRAINT module_events_check1 CHECK(split_part(type,'.',1)=CASE source WHEN 'deedkit:calendar' THEN 'calendar'
  WHEN 'deedkit:recordings' THEN 'recording' WHEN 'deedkit:feedback' THEN 'feedback' ELSE 'call' END
  OR type='module.access.changed' AND source='deedkit:calendar');
ALTER TABLE module_links DROP CONSTRAINT IF EXISTS module_links_module_check;
ALTER TABLE module_links ADD CONSTRAINT module_links_module_check CHECK(module IN ('calls','calendar'));
SET LOCAL lock_timeout=DEFAULT;

CREATE INDEX IF NOT EXISTS module_events_module_access ON module_events(scope_ref,seq) WHERE type='module.access.changed';

-- An access:none event ends every open receipt (one with a module range) of a subscription whose principal links the event's
-- calendar principal, in the publisher's transaction, so a receipt issued before the loss never ACKs after it
-- (the call link's _write does the same for a revocation). The publisher is the router's role: only this definer
-- function writes subscription_receipts, and only that one column of rows of one link.
CREATE OR REPLACE FUNCTION public.module_access_expire_receipts() RETURNS trigger
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 UPDATE subscription_receipts r SET expires_at=clock_timestamp()
  FROM subscriptions s JOIN module_links ml ON ml.principal=s.principal AND ml.module='calendar'
   AND ml.module_principal=NEW.subject
  WHERE r.subscription=s.id AND r.ack_result IS NULL AND r.module_through_position IS NOT NULL
   AND r.expires_at>clock_timestamp();
 RETURN NULL;
END $$;
REVOKE ALL ON FUNCTION public.module_access_expire_receipts() FROM PUBLIC;
DROP TRIGGER IF EXISTS module_access_none ON module_events;
CREATE TRIGGER module_access_none AFTER INSERT ON module_events FOR EACH ROW
 WHEN (NEW.type='module.access.changed' AND NEW.data->>'access'='none')
 EXECUTE FUNCTION public.module_access_expire_receipts();

-- 091's text; only the module.access.changed spec, its type-to-source rule and its urgency rule are new.
CREATE OR REPLACE FUNCTION public.module_event_valid(p_id text, p_source text, p_type text, p_subject text,
  p_time text, p_data jsonb, p_urgency text, p_expires_at text) RETURNS boolean
 LANGUAGE plpgsql STABLE SET search_path=pg_catalog AS $$
DECLARE spec jsonb; rule text; item record; v jsonb; parts text[]; t timestamptz; x timestamptz;
 pattern constant text := '^(19[7-9][0-9]|[2-9][0-9]{3})-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d{1,6})?(Z|[+-](0\d|1[0-5]):[0-5]\d)$';
 -- ANYM-962: feedback.SPEC_REF and feedback.FIXED_IN (at most 80 characters), anchored.
 spec_ref constant text := '^ANYM-[1-9][0-9]{0,5}$';
 release constant text := '^(deedkit@[0-9a-f]{40}|anymsg@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?)$';
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
   "reason": "enum:permission_removed|principal_removed|owner_changed"}},
  "module.access.changed": {"subject": "principal_ref", "optional": ["until"], "keys": {"principal_ref": "ref",
   "access": "enum:details|none", "reason": "enum:granted|permission_removed|principal_removed|member_removed|connection_disabled|visibility_reduced|expired",
   "until": "time"}},
  "feedback.received": {"subject": "feedback_id", "optional": ["spec_ref", "fixed_in"], "keys": {"feedback_id": "ref",
   "status": "enum:received", "previous_status": "enum:received|accepted|declined|duplicate|fixed|verify_requested|verified|reopened",
   "spec_ref": "spec", "fixed_in": "release"}},
  "feedback.accepted": {"subject": "feedback_id", "optional": ["spec_ref", "fixed_in"], "keys": {"feedback_id": "ref",
   "status": "enum:accepted", "previous_status": "enum:received|accepted|declined|duplicate|fixed|verify_requested|verified|reopened",
   "spec_ref": "spec", "fixed_in": "release"}},
  "feedback.declined": {"subject": "feedback_id", "optional": ["spec_ref", "fixed_in"], "keys": {"feedback_id": "ref",
   "status": "enum:declined", "previous_status": "enum:received|accepted|declined|duplicate|fixed|verify_requested|verified|reopened",
   "spec_ref": "spec", "fixed_in": "release"}},
  "feedback.duplicate": {"subject": "feedback_id", "optional": ["spec_ref", "fixed_in"], "keys": {"feedback_id": "ref",
   "status": "enum:duplicate", "previous_status": "enum:received|accepted|declined|duplicate|fixed|verify_requested|verified|reopened",
   "spec_ref": "spec", "fixed_in": "release"}},
  "feedback.fixed": {"subject": "feedback_id", "optional": ["spec_ref", "fixed_in"], "keys": {"feedback_id": "ref",
   "status": "enum:fixed", "previous_status": "enum:received|accepted|declined|duplicate|fixed|verify_requested|verified|reopened",
   "spec_ref": "spec", "fixed_in": "release"}},
  "feedback.verify_requested": {"subject": "feedback_id", "optional": ["spec_ref", "fixed_in"], "keys": {"feedback_id": "ref",
   "status": "enum:verify_requested", "previous_status": "enum:received|accepted|declined|duplicate|fixed|verify_requested|verified|reopened",
   "spec_ref": "spec", "fixed_in": "release"}}}';
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
   OR p_id IS NULL OR p_id !~ '^(calendar|calls|recordings|feedback):[1-9][0-9]{0,15}$'
   -- At most 2^53-1: 16 digits without a leading zero compare as text like numbers (no cast, no THEN here).
   OR length(split_part(p_id,':',2))=16 AND split_part(p_id,':',2) COLLATE "C">'9007199254740991'
   OR split_part(p_id,':',1) IS DISTINCT FROM split_part(p_source,':',2)
   -- A recording type belongs to the recordings source and only to it; the calls and calendar rules above are 065's.
   OR (p_type LIKE 'recording.%') IS DISTINCT FROM (p_source = 'deedkit:recordings')
   -- ANYM-577: a module.access.changed event belongs to the calendar source and only to it.
   OR (p_type = 'module.access.changed') AND p_source IS DISTINCT FROM 'deedkit:calendar'
   -- ANYM-962: a feedback type belongs to the feedback source and only to it.
   OR (p_type LIKE 'feedback.%') IS DISTINCT FROM (p_source = 'deedkit:feedback')
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
  IF rule IN ('ref','time','token','spec','release') OR rule LIKE 'enum:%' THEN
   IF jsonb_typeof(v)<>'string' THEN RETURN false; END IF;
  END IF;
  IF rule='ref' AND v#>>'{}' !~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'
    OR rule='token' AND v#>>'{}' !~ '^[a-z][a-z0-9_]{0,47}$'
    OR rule='spec' AND v#>>'{}' !~ spec_ref
    OR rule='release' AND (length(v#>>'{}')>80 OR v#>>'{}' !~ release)
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
   OR p_type='call.access.changed' AND (p_urgency<>'now' OR p_expires_at IS NOT NULL)
   OR p_type='module.access.changed' AND (p_urgency<>'now' OR p_expires_at IS NOT NULL)
   -- ANYM-962: a status change is urgent, never expires and always changes the status.
   OR p_type LIKE 'feedback.%' AND (p_urgency<>'now' OR p_expires_at IS NOT NULL
     OR p_data->>'status'=p_data->>'previous_status') THEN
  RETURN false;
 END IF;
 RETURN true;
END $$;
