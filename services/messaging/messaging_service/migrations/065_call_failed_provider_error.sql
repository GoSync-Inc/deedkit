-- rollout: manual
-- ANYM-695: call.failed accepts two optional data keys, provider_cause (a token) and provider_code (an integer
-- from 1 to 999999, only beside a cause), so a subscriber sees why Twilio refused or dropped a call from the event
-- itself. One function is replaced with its signature unchanged, so its owner and ACL stay; nothing else changes
-- (no table, column, index, row or role). Risk tier: medium (docs/operations/production.md "Migration risk tiers": CREATE OR REPLACE FUNCTION no index, constraint or trigger uses; a hand release).
-- The 1024-byte data bound of 056 stays. Applied migration 056 remains immutable. A schema-64 runtime refuses this
-- schema anyway, and an old runtime's Python check refuses a five-key event (the router re-fetches it).
SET LOCAL lock_timeout='2s';
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
 -- ANYM-695: Twilio's code is only meaningful beside its cause.
 IF p_type='call.failed' AND p_data ? 'provider_code' AND NOT p_data ? 'provider_cause' THEN RETURN false; END IF;
 IF p_type='call.owner_request' AND (p_urgency<>'now' OR p_expires_at IS NULL)
   OR p_type='calendar.event.starting' AND (x IS NULL
     OR x<>(p_data->>'start_at')::timestamptz) THEN
  RETURN false;
 END IF;
 RETURN true;
END $$;
