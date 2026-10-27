-- rollout: manual
-- Agent self-registered webhook endpoints (ANYM-519). Expand only: new tables, a nullable endpoint column
-- beside receiver on the binding and the outbox, and the permission and audit CHECKs with 'delivery-write'
-- and the five endpoint operations. A schema-48 runtime refuses this schema anyway. No GRANT here: the
-- column-private grants live in gateway_role.sql and delivery_role.sql, installed by the operator and
-- reviewed (an API role that may INSERT and UPDATE header values and secrets but never SELECT them).
-- Nothing stages an endpoint hint until the operator enables delivery_features (probe_metadata).
SET LOCAL lock_timeout='2s';
ALTER TABLE principals DROP CONSTRAINT supported_permissions;
ALTER TABLE principals ADD CONSTRAINT supported_permissions CHECK(permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write']::text[]);
ALTER TABLE api_keys DROP CONSTRAINT key_supported_permissions;
ALTER TABLE api_keys ADD CONSTRAINT key_supported_permissions CHECK(permissions IS NULL OR permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write']::text[]);
-- The audit CHECK stays NOT VALID as 036 left it (see 039).
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
 'delete_delivery_endpoint','test_delivery_endpoint')) NOT VALID;
CREATE TABLE delivery_endpoints (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 principal text NOT NULL REFERENCES principals(id),
 key_id uuid NOT NULL REFERENCES api_keys(id),
 request_id text NOT NULL CHECK(length(request_id) BETWEEN 8 AND 128),
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
 url text NOT NULL CHECK(length(url) BETWEEN 1 AND 2048),
 host text NOT NULL CHECK(length(host) BETWEEN 1 AND 253),
 label text NOT NULL DEFAULT '' CHECK(length(label)<=128),
 header_names text[] NOT NULL DEFAULT '{}' CHECK(cardinality(header_names)<=4),
 headers_fingerprint text CHECK(headers_fingerprint ~ '^[a-f0-9]{8}$'),
 secret_fingerprint text CHECK(secret_fingerprint ~ '^[a-f0-9]{8}$'),
 pending_fingerprint text CHECK(pending_fingerprint ~ '^[a-f0-9]{8}$'),
 rotation_started_at timestamptz,
 -- Write-only for the API role (INSERT and UPDATE, no SELECT); only the dispatcher reads them.
 header_values text[] CHECK(header_values IS NULL OR cardinality(header_values)<=4),
 signing_secret text CHECK(signing_secret IS NULL OR length(signing_secret)<=128),
 pending_secret text CHECK(pending_secret IS NULL OR length(pending_secret)<=128),
 state text NOT NULL DEFAULT 'enabled' CHECK(state IN ('enabled','agent_paused','auto_paused','operator_disabled','deleted')),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(key_id,request_id)
);
CREATE INDEX delivery_endpoint_owner ON delivery_endpoints(principal,key_id,created_at,id);
CREATE TABLE delivery_endpoint_tests (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 endpoint uuid NOT NULL REFERENCES delivery_endpoints(id),
 request_id text NOT NULL CHECK(length(request_id) BETWEEN 8 AND 128),
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','delivered','failed','cancelled')),
 lease_token uuid, lease_until timestamptz,
 http_status smallint CHECK(http_status BETWEEN 100 AND 599),
 latency_ms integer CHECK(latency_ms>=0),
 error_code text CHECK(length(error_code)<=64),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 finished_at timestamptz,
 UNIQUE(endpoint,request_id)
);
CREATE INDEX delivery_endpoint_test_dispatch ON delivery_endpoint_tests(created_at,id) WHERE state IN ('pending','leased');
-- A binding and a hint target exactly one receiver or one endpoint. The binding table is small and
-- validated; the outbox CHECK is NOT VALID as 038 left its kind CHECK (every existing row has a receiver).
ALTER TABLE subscription_delivery ADD COLUMN endpoint uuid REFERENCES delivery_endpoints(id),
 ALTER COLUMN receiver DROP NOT NULL,
 ADD CONSTRAINT subscription_delivery_one_target CHECK(num_nonnulls(receiver,endpoint)=1);
ALTER TABLE subscription_outbox ADD COLUMN endpoint uuid REFERENCES delivery_endpoints(id),
 ALTER COLUMN receiver DROP NOT NULL,
 ADD CONSTRAINT subscription_outbox_one_target CHECK(num_nonnulls(receiver,endpoint)=1) NOT VALID;
-- The dispatcher builds an endpoint hint's references under its fence, yet never reads message text:
-- this definer function returns only IDs, timing and the native wake evidence, scope-filtered, at most 20
-- rows with the filtered total. Relations are schema-qualified and pg_temp is searched last, so a caller's
-- temporary objects cannot stand in for them. EXECUTE is the operator's grant (delivery_role.sql); migrate() closes
-- it to PUBLIC again after a restore.
CREATE FUNCTION subscription_hint_references(p_subscription uuid, p_after bigint, p_through bigint,
  p_accounts text[], p_chats text[])
 RETURNS TABLE(event bigint, kind text, origin text, account text, created_at timestamptz, evidence jsonb, total bigint)
 LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
 SELECT e.id,e.kind,e.origin,e.account,e.created_at,
  jsonb_strip_nulls(jsonb_build_object('chat',e.snapshot->'chat','sender',e.snapshot->'sender',
   'message',e.snapshot->'message','direction',e.snapshot->'direction',
   'metadata',jsonb_strip_nulls(jsonb_build_object('mentions_owner',e.snapshot->'metadata'->'mentions_owner',
    'reply_to_owner',e.snapshot->'metadata'->'reply_to_owner',
    'reply_to_provider_id',e.snapshot->'metadata'->'reply_to_provider_id')),
   'reply_evidence',jsonb_strip_nulls(jsonb_build_object('basis',e.snapshot->'reply_evidence'->'basis')))),
  count(*) OVER ()
 FROM public.subscription_matches m JOIN public.events e ON e.id=m.event
 WHERE m.subscription=p_subscription AND m.event>p_after AND m.event<=p_through AND e.account=ANY(p_accounts)
  AND (p_chats IS NULL OR e.snapshot->>'chat'=ANY(p_chats))
 ORDER BY m.event LIMIT 20 $$;
REVOKE ALL ON FUNCTION subscription_hint_references(uuid,bigint,bigint,text[],text[]) FROM PUBLIC;
