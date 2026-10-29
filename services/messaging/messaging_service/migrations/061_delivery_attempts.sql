-- rollout: manual
-- ANYM-520: an agent sees each delivery to its webhook. delivery_attempts keeps one row per attempt of an endpoint hint:
-- time, hint, subscription, attempt number, outcome, the HTTP status or an error code and the latency. No response
-- body, header value or URL is stored. The dispatcher writes it and prunes rows older than 7 days; the API reads the
-- owning key's rows. delivery_endpoint_tests.purpose marks the signed test a resume sends while the endpoint is still
-- paused. The audit CHECK gains list_delivery_attempts (a DROP and ADD pair, so this file is manual); it stays NOT
-- VALID as 036 left it. No GRANT here: delivery_role.sql and gateway_role.sql, installed by the operator.
SET LOCAL lock_timeout='2s';
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
 'delete_delivery_endpoint','test_delivery_endpoint','list_delivery_attempts')) NOT VALID;
CREATE TABLE delivery_attempts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 endpoint uuid NOT NULL REFERENCES delivery_endpoints(id),
 -- No foreign key: retention may delete a hint or subscription before its attempt rows age out.
 hint uuid NOT NULL,
 subscription uuid NOT NULL,
 attempt integer NOT NULL CHECK(attempt BETWEEN 0 AND 1000),
 outcome text NOT NULL CHECK(outcome IN ('delivered','retry','dead_letter','cancelled','paused')),
 http_status smallint CHECK(http_status BETWEEN 100 AND 599),
 error_code text CHECK(length(error_code)<=64),
 latency_ms integer CHECK(latency_ms>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX delivery_attempt_endpoint ON delivery_attempts(endpoint,created_at DESC,id DESC);
CREATE INDEX delivery_attempt_age ON delivery_attempts(created_at);
ALTER TABLE delivery_endpoint_tests ADD COLUMN purpose text NOT NULL DEFAULT 'test' CHECK(purpose IN ('test','resume'));
