-- Agent-visible feedback disposition (ANYM-343). Expand-only: every existing row reads 'received';
-- schema-38 code never reads the new columns. Only the operator's owner connection sets them
-- (`set-feedback-status`); the agent-facing role keeps column-level INSERT of the submission fields.
-- A short lock wait fails the upgrade instead of stalling requests that write audit_log or feedback
-- (their statement timeout is 5 s). The audit CHECK stays NOT VALID as 036 left it: Postgres enforces
-- it on every row written since, and every older row passed a validated subset of the new names.
SET LOCAL lock_timeout='2s';
ALTER TABLE agent_feedback
 ADD COLUMN status text NOT NULL DEFAULT 'received' CONSTRAINT agent_feedback_status_check CHECK(status IN (
  'received','accepted','declined','duplicate','fixed','verify_requested','verified','reopened')),
 ADD COLUMN status_note text CONSTRAINT agent_feedback_status_note_check
  CHECK(length(status_note) BETWEEN 1 AND 280 AND status_note !~ '[\x01-\x1f\x7f]'),
 ADD COLUMN fixed_in text CONSTRAINT agent_feedback_fixed_in_check CHECK(length(fixed_in)<=80 AND
  fixed_in ~ '^(deedkit@[0-9a-f]{40}|anymsg@(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*)(\.(0|[1-9][0-9]*|[0-9]*[A-Za-z-][0-9A-Za-z-]*))*)?(\+[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?)$'),
 ADD COLUMN spec_ref text CONSTRAINT agent_feedback_spec_ref_check CHECK(spec_ref ~ '^ANYM-[1-9][0-9]{0,5}$'),
 ADD COLUMN duplicate_of uuid REFERENCES agent_feedback(id) ON DELETE SET NULL,
 ADD COLUMN status_changed_at timestamptz,
 ADD CONSTRAINT agent_feedback_duplicate_of_check CHECK(duplicate_of<>id);
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
 'extend_key','set_feedback_status')) NOT VALID;
SET LOCAL lock_timeout=DEFAULT;

-- The existing gateway login loses table-wide INSERT with the schema, so no deployed state lets an
-- agent-side insert set a maintainer field (the 034 precedent). A table-level REVOKE also drops
-- column privileges, so the column grant follows it. gateway_role.sql repeats this for new installs.
DO $$ BEGIN
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  REVOKE INSERT,UPDATE ON agent_feedback FROM messaging_gateway;
  GRANT INSERT(principal,key_id,request_id,request_hash,category,description,operation,refs)
   ON agent_feedback TO messaging_gateway;
 END IF;
END $$;
