-- rollout: manual
-- ANYM-992: a maintainer key moves a feedback item to accepted, declined or duplicate through one guarded write, so a status
-- change of its task in BB reaches the agent that reported it. Three additive pieces, no row read, rewritten or deleted:
--  1. The two permission CHECKs accept 'feedback-maintainer-write' (094's pair, validated as 094's) and the audit CHECK gains
--     set_feedback_triage (NOT VALID as 036 left it). No key or principal receives the permission here: a grant is DevOps'
--     separate step after the release. `manual`, not `expand`, because the api_keys CHECK keeps 088's `permissions IS NULL OR ...`
--     text, which `migration_rollout.classify` does not read (as 094); everything else in this file is expand-class.
--  2. feedback_triage_requests: the idempotency record of the write (key, request ID, payload hash, the answer's fields).
--  3. public.set_feedback_triage(...): the API role's only way to change a status. The role keeps SELECT and the seven-column
--     INSERT of agent_feedback and no UPDATE (039, gateway_role.sql); the function is a definer function that accepts only
--     the three statuses and the operator's rules for them, compare-and-sets on the stored status and its change time,
--     is idempotent per (key, request ID), refuses to overwrite fixed, verify_requested or verified, and publishes the
--     ANYM-962 status event in the same transaction. PUBLIC loses EXECUTE here; gateway_role.sql grants it to the API role.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": CHECK widening pairs, each a superset of the
-- old list, a CREATE TABLE and a new definer function; no ALTER of an older table's columns, no DROP of data, no data change).
-- Repeatable (module tests re-run migrations): the pairs re-run, the table is IF NOT EXISTS and the function is OR REPLACE.
-- A schema-95 runtime refuses this schema (database_newer_than_runtime) and never names the tool or the function.
SET LOCAL lock_timeout='2s';
ALTER TABLE principals DROP CONSTRAINT supported_permissions;
ALTER TABLE principals ADD CONSTRAINT supported_permissions CHECK(permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open','groups-moderate',
 'stories-post','feedback-maintainer-read','feedback-maintainer-write']::text[]);
ALTER TABLE api_keys DROP CONSTRAINT key_supported_permissions;
ALTER TABLE api_keys ADD CONSTRAINT key_supported_permissions CHECK(permissions IS NULL OR permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open','groups-moderate',
 'stories-post','feedback-maintainer-read','feedback-maintainer-write']::text[]);
ALTER TABLE audit_log DROP CONSTRAINT audit_log_operation_check;
ALTER TABLE audit_log ADD CONSTRAINT audit_log_operation_check CHECK(operation IN (
 'capabilities','list_chats','search','get_messages','get_context','get_updates',
 'get_attachment','download','ack_updates','issue_key','revoke_key','set_access','http_boundary',
 'get_reviews','record_review','search_people','search_files','get_file_text',
 'get_file_occurrences','get_file_segments','get_sync_status','resolve_reference',
 'send_message','get_send_status','send_dispatch','set_sending','preview_message',
 'set_key_permissions','set_operations','list_messenger_operations','preview_messenger_operation',
 'execute_messenger_operation','get_messenger_operation','messenger_dispatch',
 'create_subscription','list_subscriptions','get_subscription','update_subscription','get_subscription_events',
 'dismiss_reply','prepare_file_upload','upload_file_chunk','get_file_upload','request_file_download',
 'fetch_subscription_events','ack_subscription_events','wait_for_subscription_events',
 'configure_subscription_delivery','test_subscription','submit_feedback','list_feedback',
 'extend_key','set_feedback_status','snooze_reply','restore_reply','mute_reply_source',
 'unmute_reply_source','list_reply_dismissals','message_purge',
 'register_delivery_endpoint','list_delivery_endpoints','update_delivery_endpoint',
 'delete_delivery_endpoint','test_delivery_endpoint','list_delivery_attempts',
 'register_wake_target','list_wake_targets','update_wake_target','delete_wake_target','confirm_wake_target',
 'list_negotiations','export_feedback_metadata','record_feedback_projection','set_feedback_triage')) NOT VALID;
SET LOCAL lock_timeout=DEFAULT;
-- The request record of the guarded write: one row per (maintainer key, request ID), written in the same transaction as the
-- status it records and read by the function alone (no role has a table privilege). A deleted item takes its rows with it.
CREATE TABLE IF NOT EXISTS feedback_triage_requests (
 key_id uuid NOT NULL REFERENCES api_keys(id),
 request_id text NOT NULL CHECK(length(request_id) BETWEEN 8 AND 128),
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
 feedback_id uuid NOT NULL REFERENCES agent_feedback(id) ON DELETE CASCADE,
 status text NOT NULL CHECK(status IN ('accepted','declined','duplicate')),
 spec_ref text CHECK(spec_ref ~ '^ANYM-[1-9][0-9]{0,5}$'),
 duplicate_of uuid,
 status_changed_at timestamptz NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(key_id,request_id)
);
CREATE INDEX IF NOT EXISTS feedback_triage_requests_item ON feedback_triage_requests(feedback_id);

-- The API role's only way to change a status (ANYM-992). It re-checks everything the Python tool checked, so a direct call
-- is no wider than the tool: three statuses, the operator's rules for them, compare-and-set on the stored status and its
-- change time, idempotency by (key, request ID, payload hash), and the ANYM-962 event in the same transaction (an event
-- that fails to publish is swallowed in a sub-block, as the operator's savepoint does, and reported in `published`).
-- An item in an evidence-driven state (fixed, verify_requested, verified) is never overwritten: version_conflict.
CREATE OR REPLACE FUNCTION public.set_feedback_triage(p_key uuid, p_request_id text, p_request_hash text, p_feedback uuid,
  p_status text, p_expected_status text, p_expected_changed_at timestamptz, p_spec_ref text, p_duplicate_of uuid, p_note text)
 RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
DECLARE item public.agent_feedback; done public.feedback_triage_requests; target public.agent_feedback;
 changed timestamptz; next_seq bigint; published boolean;
BEGIN
 IF p_key IS NULL OR p_feedback IS NULL OR p_status IS NULL OR p_status NOT IN ('accepted','declined','duplicate')
   OR p_expected_status IS NULL OR p_expected_status NOT IN ('received','accepted','declined','duplicate','fixed',
     'verify_requested','verified','reopened')
   OR p_request_id IS NULL OR length(p_request_id) NOT BETWEEN 8 AND 128
   OR p_request_hash IS NULL OR p_request_hash !~ '^[a-f0-9]{64}$'
   OR p_spec_ref IS NOT NULL AND p_spec_ref !~ '^ANYM-[1-9][0-9]{0,5}$'
   OR p_status='accepted' AND p_spec_ref IS NULL
   OR p_status='duplicate' AND p_duplicate_of IS NULL AND p_spec_ref IS NULL
   OR p_status<>'duplicate' AND p_duplicate_of IS NOT NULL
   OR p_note IS NOT NULL AND (length(p_note) NOT BETWEEN 1 AND 280 OR p_note ~ '[\x01-\x1f\x7f]' OR btrim(p_note)='') THEN
  RAISE EXCEPTION 'invalid_feedback';
 END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('feedback-triage:'||p_key::text||':'||p_request_id,0));
 SELECT * INTO done FROM public.feedback_triage_requests WHERE key_id=p_key AND request_id=p_request_id;
 IF FOUND THEN
  IF done.request_hash<>p_request_hash THEN RAISE EXCEPTION 'idempotency_conflict'; END IF;
  RETURN jsonb_build_object('feedback_id',done.feedback_id,'status',done.status,'status_changed_at',done.status_changed_at,
   'spec_ref',done.spec_ref,'duplicate_of',done.duplicate_of,'replayed',true,'published',NULL);
 END IF;
 SELECT * INTO item FROM public.agent_feedback WHERE id=p_feedback FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 IF item.status IS DISTINCT FROM p_expected_status OR item.status_changed_at IS DISTINCT FROM p_expected_changed_at
   OR item.status IN ('fixed','verify_requested','verified') THEN
  RAISE EXCEPTION 'version_conflict';
 END IF;
 IF p_duplicate_of IS NOT NULL THEN
  SELECT * INTO target FROM public.agent_feedback WHERE id=p_duplicate_of;
  IF p_duplicate_of=p_feedback OR NOT FOUND OR target.key_id<>item.key_id THEN RAISE EXCEPTION 'invalid_feedback'; END IF;
 END IF;
 UPDATE public.agent_feedback SET status=p_status,status_note=p_note,fixed_in=NULL,spec_ref=p_spec_ref,
  duplicate_of=p_duplicate_of,status_changed_at=clock_timestamp() WHERE id=p_feedback RETURNING status_changed_at INTO changed;
 IF item.status<>p_status THEN
  BEGIN
   SELECT value+1 INTO next_seq FROM public.module_event_counter WHERE id=1 FOR UPDATE;
   PERFORM public.publish_module_event('feedback:'||next_seq::text,'deedkit:feedback','feedback.'||p_status,p_feedback::text,
    item.key_id::text,to_char(changed AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    jsonb_strip_nulls(jsonb_build_object('feedback_id',p_feedback::text,'status',p_status,'previous_status',item.status,
     'spec_ref',p_spec_ref)),'now',NULL);
   published := true;
  EXCEPTION WHEN OTHERS THEN
   published := false;
  END;
 END IF;
 INSERT INTO public.feedback_triage_requests(key_id,request_id,request_hash,feedback_id,status,spec_ref,duplicate_of,
  status_changed_at) VALUES(p_key,p_request_id,p_request_hash,p_feedback,p_status,p_spec_ref,p_duplicate_of,changed);
 RETURN jsonb_build_object('feedback_id',p_feedback,'status',p_status,'status_changed_at',changed,'spec_ref',p_spec_ref,
  'duplicate_of',p_duplicate_of,'replayed',false,'published',published);
END $$;
REVOKE ALL ON FUNCTION public.set_feedback_triage(uuid,text,text,uuid,text,text,timestamptz,text,uuid,text) FROM PUBLIC;
