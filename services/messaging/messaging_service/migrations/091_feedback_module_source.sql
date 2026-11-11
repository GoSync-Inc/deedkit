-- rollout: manual
-- ANYM-962: the module journal admits DeedKit's own feedback source (source deedkit:feedback, ids feedback:<n>, types
-- feedback.<status> for the six operator statuses), so an agent is woken when its own report changes status. The
-- publisher is in-process (feedback.set_feedback_status on the owner connection); no router fetches this source.
-- Inventory, as 067's (every place that names the modules; eight CHECKs and one function), now with 090's type:
--  056 module_events: event_key_check (id pattern), source_check, type_check (090's list), check1 (source-to-type
--      correspondence); check (id module = source module) names no module: kept.
--  056 subscription_module_sources_source_check, module_source_health_source_check.
--  058 module_router_cursors_source_check, module_router_gaps_source_check.
--  module_event_valid, rebuilt from 090's text: the feedback specs, the two rules their values need (spec, release),
--      the id pattern, the type-to-source rule and one urgency rule are new; every other rule is 090's, unchanged.
--      publish_module_event (090's text) names no module: kept (same ACL, same text).
--  report_module_reachability, module_hints_ready, subscription_module_fence, admit_module_source, set_module_link,
--      the grants of 056, 058 and 090 and every *_role.sql name tables and functions, not sources: kept.
-- No sequence: the <n> of feedback:<n> is the event's own journal number (the publisher holds module_event_counter's
-- row lock, which publish_module_event takes next), so a restore of the journal and its counter (backup.py copies
-- tables, never sequences) can never hand out a feedback ID twice.
-- Each replacement keeps its constraint's name and widens its list: every row the old conjunction admitted is
-- admitted now (tests/test_feedback_events.py checks the module_events conjunction over 067's grid, and that no CHECK
-- or function names the other modules without feedback). check1 alone is not a superset (source deedkit:feedback
-- with a call.* type passed the old one and fails the new one), but the old source CHECK shut that row out, so the
-- table's checks together are. Removing the old constraint and adding the widened one share this transaction under a
-- 2 s lock timeout; the tables are small.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": constraint replacement pairs,
-- and a CREATE OR REPLACE of a PL/pgSQL function that writes nothing; no row is read, rewritten or deleted; no
-- table, column, sequence, role or privilege changes). Repeatable (module tests re-run migrations): the pairs re-run and
-- the function is OR REPLACE. A schema-90 runtime refuses this schema
-- (database_newer_than_runtime) and never writes a feedback row; applied 056, 058, 065, 067 and 090 stay as they are.
SET LOCAL lock_timeout='2s';

ALTER TABLE module_events DROP CONSTRAINT module_events_event_key_check;
ALTER TABLE module_events ADD CONSTRAINT module_events_event_key_check CHECK(event_key ~ '^(calendar|calls|recordings|feedback):[1-9][0-9]{0,17}$');
ALTER TABLE module_events DROP CONSTRAINT module_events_source_check;
ALTER TABLE module_events ADD CONSTRAINT module_events_source_check CHECK(source IN ('deedkit:calendar','deedkit:calls','deedkit:recordings','deedkit:feedback'));
ALTER TABLE module_events DROP CONSTRAINT module_events_type_check;
ALTER TABLE module_events ADD CONSTRAINT module_events_type_check CHECK(type IN ('calendar.event.created','calendar.event.changed','calendar.event.cancelled',
  'calendar.event.starting','call.finished','call.failed','call.not_answered','call.interrupted','call.owner_request',
  'recording.text.archived','recording.access.changed','call.access.changed',
  'feedback.received','feedback.accepted','feedback.declined','feedback.duplicate','feedback.fixed','feedback.verify_requested'));
ALTER TABLE module_events DROP CONSTRAINT module_events_check1;
ALTER TABLE module_events ADD CONSTRAINT module_events_check1 CHECK(split_part(type,'.',1)=CASE source WHEN 'deedkit:calendar' THEN 'calendar'
  WHEN 'deedkit:recordings' THEN 'recording' WHEN 'deedkit:feedback' THEN 'feedback' ELSE 'call' END);
ALTER TABLE subscription_module_sources DROP CONSTRAINT subscription_module_sources_source_check;
ALTER TABLE subscription_module_sources ADD CONSTRAINT subscription_module_sources_source_check CHECK(source IN ('deedkit:calendar','deedkit:calls','deedkit:recordings','deedkit:feedback'));
ALTER TABLE module_source_health DROP CONSTRAINT module_source_health_source_check;
ALTER TABLE module_source_health ADD CONSTRAINT module_source_health_source_check CHECK(source IN ('deedkit:calendar','deedkit:calls','deedkit:recordings','deedkit:feedback'));
ALTER TABLE module_router_cursors DROP CONSTRAINT module_router_cursors_source_check;
ALTER TABLE module_router_cursors ADD CONSTRAINT module_router_cursors_source_check CHECK(source IN ('deedkit:calendar','deedkit:calls','deedkit:recordings','deedkit:feedback'));
ALTER TABLE module_router_gaps DROP CONSTRAINT module_router_gaps_source_check;
ALTER TABLE module_router_gaps ADD CONSTRAINT module_router_gaps_source_check CHECK(source IN ('deedkit:calendar','deedkit:calls','deedkit:recordings','deedkit:feedback'));
SET LOCAL lock_timeout=DEFAULT;

-- 090's text; only the feedback specs, the spec and release rules, the id pattern, the feedback type-to-source rule
-- and the feedback urgency rule are new.
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
   -- ANYM-962: a status change is urgent, never expires and always changes the status.
   OR p_type LIKE 'feedback.%' AND (p_urgency<>'now' OR p_expires_at IS NOT NULL
     OR p_data->>'status'=p_data->>'previous_status') THEN
  RETURN false;
 END IF;
 RETURN true;
END $$;
