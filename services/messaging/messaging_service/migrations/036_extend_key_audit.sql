-- Operator renewal of a live key in place (ANYM-422). Expand-only: every schema-35
-- operation name stays allowed; deployed schema-35 code never writes 'extend_key'.
-- A short lock wait fails the upgrade instead of stalling requests that write audit_log
-- (their statement timeout is 5 s). NOT VALID skips the history scan under the lock:
-- the replaced, validated constraint allowed a subset of the new names.
DO $$ BEGIN
 IF NOT (SELECT convalidated FROM pg_constraint
         WHERE conrelid='audit_log'::regclass AND conname='audit_log_operation_check') THEN
  RAISE EXCEPTION 'audit_log_operation_check_not_validated';
 END IF;
END $$;
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
 'extend_key')) NOT VALID;
SET LOCAL lock_timeout=DEFAULT;
