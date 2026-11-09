-- rollout: expand
-- Risk tier: non-critical (the audit CHECK widening pair only; no table, row, column or grant changes;
-- docs/operations/production.md "Migration risk tiers").
-- ANYM-909: the audit CHECK gains list_negotiations, the read of ANYM-907's negotiations. The widening pair is the
-- expand grammar's (as 073), so migrate-expand applies it unattended, at once; it stays NOT VALID as 036 left it.
-- The API role's SELECT on the read tables is gateway_role.sql's "Schema 76" block (ANYM-907). A schema-79 runtime
-- never names the operation; until this file is applied a list_negotiations call cannot write its audit row and fails.
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
 'delete_delivery_endpoint','test_delivery_endpoint','list_delivery_attempts',
 'register_wake_target','list_wake_targets','update_wake_target','delete_wake_target','confirm_wake_target',
 'list_negotiations')) NOT VALID;
