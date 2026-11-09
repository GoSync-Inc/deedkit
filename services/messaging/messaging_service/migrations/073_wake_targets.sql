-- rollout: expand
-- Risk tier: non-critical (two new tables, grants only on them, and the audit CHECK widening pair; no existing row,
-- column or grant changes; docs/operations/production.md "Migration risk tiers").
-- ANYM-848: an agent's own wake targets. A key-owned row names a local chat (kind codex_thread, the chat's own thread
-- ID or session name) that a host waker on the owner's machine wakes when one of the key's own bound subscriptions has
-- new events (packages/wake-relay, ANYM-849). DeedKit never contacts the chat. `confirmation` is the one-use code the
-- chat returned from the waker's challenge; DeedKit only stores and returns it. A subscription wakes at most one chat.
-- The audit CHECK gains the five tool operations (the widening pair; it stays NOT VALID as 036 left it). A schema-71
-- runtime never names the tables; a schema-72 runtime on a schema-71 database answers the tools delivery_disabled and
-- shows every subscription with wake_target_id null. Grants: the API role only (gateway_role.sql). No column default
-- calls a function (the expand grammar takes literals only): wake_targets.py writes id and both times.
SET LOCAL lock_timeout='2s';
CREATE TABLE IF NOT EXISTS wake_targets (
 id uuid PRIMARY KEY,
 principal text NOT NULL REFERENCES principals(id),
 key_id uuid NOT NULL REFERENCES api_keys(id),
 request_id text NOT NULL CHECK(length(request_id) BETWEEN 8 AND 128),
 request_hash text NOT NULL CHECK(request_hash ~ '^[a-f0-9]{64}$'),
 kind text NOT NULL CHECK(kind IN ('codex_thread')),
 thread text NOT NULL CHECK(length(thread) BETWEEN 1 AND 64),
 label text NOT NULL DEFAULT '' CHECK(length(label)<=80),
 hourly_budget integer NOT NULL DEFAULT 12 CHECK(hourly_budget BETWEEN 1 AND 60),
 state text NOT NULL DEFAULT 'enabled' CHECK(state IN ('enabled','agent_paused','operator_disabled','deleted')),
 disabled_reason text CHECK(disabled_reason IN ('operator','restore')),
 confirmation text CHECK(confirmation ~ '^[0-9a-f]{32}$'),
 version bigint NOT NULL DEFAULT 1 CHECK(version>0),
 created_at timestamptz NOT NULL,
 updated_at timestamptz NOT NULL,
 UNIQUE(key_id,request_id),
 CHECK((state='operator_disabled')=(disabled_reason IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS wake_target_owner ON wake_targets(principal,key_id,created_at,id);
CREATE TABLE IF NOT EXISTS wake_target_subscriptions (
 wake_target uuid NOT NULL REFERENCES wake_targets(id),
 subscription uuid NOT NULL REFERENCES subscriptions(id),
 PRIMARY KEY(wake_target,subscription),
 UNIQUE(subscription)
);
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
 'register_wake_target','list_wake_targets','update_wake_target','delete_wake_target','confirm_wake_target')) NOT VALID;
