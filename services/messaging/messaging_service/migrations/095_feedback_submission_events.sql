-- rollout: manual
-- ANYM-989: a maintainer key is woken the moment an agent submits feedback or the loop records its summary. Three
-- additive pieces and nothing that reads, rewrites or deletes a row:
--  1. module_events_type_check gains feedback.projection_recorded (093's list plus one type; check1 already maps every
--     `feedback.*` type to the feedback source, so it stays as 093 left it).
--  2. module_event_valid is rebuilt from 093's text. feedback.received gains a second shape for a submission:
--     data {feedback_id, status: received, category} with previous_status absent. On the operator's events it is still
--     required and different from the status (an operator change back to received included), and no other key may
--     join either shape, so every event 091 admitted stays valid. feedback.projection_recorded is new: data
--     {feedback_id, classification} with the reader's closed classification list (feedback_projection.CLASSES).
--     Every other rule is 093's, unchanged.
--  3. public.publish_feedback_received(uuid): the one definer function through which the API role journals a
--     submission (the API role cannot execute publish_module_event or lock the journal counter). It reads the row's
--     category itself, so the caller supplies no data; its scope_ref is the fixed `feedback:submitted`, never a key ID,
--     so the own-key option of ANYM-962 sees exactly the events it saw before; the counter's row lock is taken first
--     and the event's id is its journal number (as 091); a second call for the same item returns the stored event.
--     PUBLIC loses EXECUTE here; gateway_role.sql grants it to the API role and no other.
-- No table, column, role or grant changes; the maintainer permission is 094's (ANYM-988), and the maintainer source
-- (`feedback:all`) is a subscription_module_sources row the 091 constraints already admit. `manual`, not `expand`: the
-- replacement pair and the function body are classified by hand, like 091 and 093.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": one constraint replacement pair that
-- widens a list, a CREATE OR REPLACE of a PL/pgSQL function that writes nothing, and a new definer function; no row is
-- read, rewritten or deleted). Repeatable (module tests re-run migrations): the pair re-runs and both functions are OR
-- REPLACE. A schema-94 runtime refuses this schema (database_newer_than_runtime) and never names the function.
SET LOCAL lock_timeout='2s';

ALTER TABLE module_events DROP CONSTRAINT module_events_type_check;
ALTER TABLE module_events ADD CONSTRAINT module_events_type_check CHECK(type IN ('calendar.event.created','calendar.event.changed','calendar.event.cancelled',
  'calendar.event.starting','call.finished','call.failed','call.not_answered','call.interrupted','call.owner_request',
  'recording.text.archived','recording.access.changed','call.access.changed','module.access.changed',
  'feedback.received','feedback.accepted','feedback.declined','feedback.duplicate','feedback.fixed','feedback.verify_requested',
  'feedback.projection_recorded'));
SET LOCAL lock_timeout=DEFAULT;

-- 093's text; only the feedback.received submission shape, the feedback.projection_recorded spec and the rules they need are new.
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
  "feedback.received": {"subject": "feedback_id", "optional": ["previous_status", "spec_ref", "fixed_in", "category"], "keys": {"feedback_id": "ref",
   "status": "enum:received", "previous_status": "enum:received|accepted|declined|duplicate|fixed|verify_requested|verified|reopened",
   "spec_ref": "spec", "fixed_in": "release", "category": "enum:bug|feature|quality|other"}},
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
   "spec_ref": "spec", "fixed_in": "release"}},
  "feedback.projection_recorded": {"subject": "feedback_id", "optional": [], "keys": {"feedback_id": "ref",
   "classification": "enum:reported-bug|feature|question|not-actionable|already-live|enable-only"}}}';
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
   -- ANYM-962: a status change is urgent, never expires and always changes the status (ANYM-989: a submission and a
   -- recorded projection have no previous status, so the comparison is null for them and the rules below judge them).
   OR p_type LIKE 'feedback.%' AND (p_urgency<>'now' OR p_expires_at IS NOT NULL
     OR p_data->>'status'=p_data->>'previous_status') THEN
  RETURN false;
 END IF;
 -- ANYM-989: a received event is one of two shapes: the operator's (previous status present, no category) or a
 -- submission's (category present, no previous status, spec or release).
 IF p_type='feedback.received' AND NOT (
   p_data ? 'previous_status' AND NOT p_data ? 'category'
   OR NOT p_data ? 'previous_status' AND p_data ? 'category' AND NOT p_data ? 'spec_ref' AND NOT p_data ? 'fixed_in') THEN
  RETURN false;
 END IF;
 RETURN true;
END $$;

-- The API role's only way to journal a submission (ANYM-989). See the header.
CREATE OR REPLACE FUNCTION public.publish_feedback_received(p_feedback uuid) RETURNS bigint
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE item_category text; existing bigint; next_seq bigint; stamp text;
BEGIN
 SELECT f.category INTO item_category FROM public.agent_feedback f WHERE f.id=p_feedback;
 IF NOT FOUND THEN RAISE EXCEPTION 'unknown_feedback' USING ERRCODE='no_data_found'; END IF;
 -- The counter's row lock first (publish_module_event takes it next): the event's number is its journal seq.
 SELECT value+1 INTO next_seq FROM public.module_event_counter WHERE id=1 FOR UPDATE;
 SELECT e.seq INTO existing FROM public.module_events e WHERE e.type='feedback.received' AND e.subject=p_feedback::text
  AND NOT e.data ? 'previous_status' ORDER BY e.seq LIMIT 1;
 IF FOUND THEN RETURN existing; END IF;
 stamp := to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"');
 RETURN public.publish_module_event('feedback:'||next_seq::text,'deedkit:feedback','feedback.received',p_feedback::text,
  'feedback:submitted',stamp,jsonb_build_object('feedback_id',p_feedback::text,'status','received','category',item_category),
  'now',NULL);
END $$;
REVOKE ALL ON FUNCTION public.publish_feedback_received(uuid) FROM PUBLIC;
