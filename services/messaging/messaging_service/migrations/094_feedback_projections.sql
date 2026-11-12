-- rollout: manual
-- ANYM-988: a maintainer key reads every agent's feedback metadata and the feedback loop's released summary.
-- Three additive pieces, no row read, rewritten or deleted and no GRANT here (the API role's SELECT is gateway_role.sql's
-- "Schema 94" block; the operator's owner connection writes the table):
--  1. The two permission CHECKs accept 'feedback-maintainer-read' (088's pair, validated as 088's). No key or principal
--     receives the permission here: a grant is DevOps' separate step after the release. `manual`, not `expand`, because
--     the api_keys CHECK keeps 088's `permissions IS NULL OR ...` text, which `migration_rollout.classify` does not read
--     (tests/test_migration_rollout.py compares a widening by that text); everything else in this file is expand-class.
--  2. The audit CHECK gains export_feedback_metadata (the key-gated read) and record_feedback_projection (the operator
--     command), 080's widening pair (NOT VALID as 036 left it).
--  3. feedback_projections: one row per feedback item that the reader decided, holding the closed answer and the released
--     brief (a scrubbed paraphrase, never the text): classification, reason_code, primary_spec, new_card, released_brief,
--     released_at, reader_pin. The classification and reason lists are scripts/autoloop.py's CLASSES and REASONS
--     (tests/platform/test_autoloop_projection.py pins them together); a deleted feedback row takes its projection with it.
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": CHECK widening pairs, each proven a
-- superset of the old list, and a CREATE TABLE; no ALTER of an older table's columns, no DROP of data, no data change).
-- Repeatable (module tests re-run migrations): the pairs re-run and the table is IF NOT EXISTS. A schema-93 runtime
-- refuses this schema (database_newer_than_runtime); a schema-93 API never names the new tool or command.
SET LOCAL lock_timeout='2s';
ALTER TABLE principals DROP CONSTRAINT supported_permissions;
ALTER TABLE principals ADD CONSTRAINT supported_permissions CHECK(permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open','groups-moderate',
 'stories-post','feedback-maintainer-read']::text[]);
ALTER TABLE api_keys DROP CONSTRAINT key_supported_permissions;
ALTER TABLE api_keys ADD CONSTRAINT key_supported_permissions CHECK(permissions IS NULL OR permissions <@ ARRAY[
 'cursor-write','download','read','review-write','send','read-live','contacts-read','profile-read',
 'groups-read','message-edit','message-delete','message-react','message-pin','read-state',
 'chat-manage','contacts-manage','groups-manage','profile-manage','subscription-write','upload',
 'subscription-ack','feedback-write','delivery-write','bot-press','webview-open','groups-moderate',
 'stories-post','feedback-maintainer-read']::text[]);
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
 'list_negotiations','export_feedback_metadata','record_feedback_projection')) NOT VALID;
SET LOCAL lock_timeout=DEFAULT;

CREATE TABLE IF NOT EXISTS feedback_projections (
 feedback_id uuid PRIMARY KEY REFERENCES agent_feedback(id) ON DELETE CASCADE,
 classification text NOT NULL CHECK(classification IN ('reported-bug','feature','question','not-actionable','already-live','enable-only')),
 reason_code text NOT NULL CHECK(reason_code IN ('matches-card','new-idea','duplicate','already-live','needs-opt-in','question-answered','not-actionable','out-of-scope','unclear')),
 primary_spec text CHECK(primary_spec ~ '^ANYM-[1-9][0-9]{0,5}$'),
 new_card text CHECK(new_card ~ '^[a-z0-9][a-z0-9-]{2,39}$'),
 released_brief text CHECK(length(released_brief) BETWEEN 1 AND 600 AND released_brief !~ '[\x01-\x1f\x7f]'),
 released_at timestamptz NOT NULL,
 reader_pin text NOT NULL CHECK(length(reader_pin) BETWEEN 1 AND 128 AND reader_pin !~ '[\x01-\x1f\x7f]'),
 recorded_at timestamptz NOT NULL DEFAULT now()
);
