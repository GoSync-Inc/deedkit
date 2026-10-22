-- rollout: manual
-- Reply-queue controls (ANYM-281): a dismissal reason, key-local snoozes and principal mutes.
-- Additive: snoozes and mutes live only in new tables, so a schema-40 runtime over this schema
-- ignores them and shows those rows again; nothing stays hidden longer than under schema 40.
-- Its re-dismissal may keep an older reason, which never changes what the queue shows.
-- Manual only because of the grants below. A short lock wait fails the upgrade instead of
-- stalling requests that write audit_log (their statement timeout is 5 s).
SET LOCAL lock_timeout='2s';
ALTER TABLE reply_dismissals
 ADD COLUMN reason text CONSTRAINT reply_dismissals_reason_check CHECK(reason IN (
  'no_reply_needed','spam_or_mailing','handled_elsewhere','other')),
 ADD COLUMN note text CONSTRAINT reply_dismissals_note_check
  CHECK(length(note) BETWEEN 1 AND 200 AND note !~ '[\x01-\x1f\x7f]');
-- One snooze per key and chat names the exact hint it hides, like a dismissal.
CREATE TABLE reply_snoozes (
 key_id uuid NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
 chat text NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
 message text NOT NULL REFERENCES messages(id),
 revision integer NOT NULL CHECK(revision>0),
 incoming_head text NOT NULL REFERENCES messages(id),
 snoozed_until timestamptz NOT NULL,
 note text CHECK(length(note) BETWEEN 1 AND 200 AND note !~ '[\x01-\x1f\x7f]'),
 snoozed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(key_id,chat)
);
-- A mute belongs to the principal: its keys share it, a rotation keeps it, other agents never see it.
CREATE TABLE reply_mutes (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 principal text NOT NULL REFERENCES principals(id) ON DELETE CASCADE,
 account text NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
 chat text,
 sender text CHECK(length(sender) BETWEEN 1 AND 512),
 reason text CHECK(reason IN ('no_reply_needed','spam_or_mailing','handled_elsewhere','other')),
 note text CHECK(length(note) BETWEEN 1 AND 200 AND note !~ '[\x01-\x1f\x7f]'),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK((chat IS NULL)<>(sender IS NULL)),
 FOREIGN KEY(chat,account) REFERENCES chats(id,account) ON DELETE CASCADE
);
CREATE UNIQUE INDEX reply_mutes_chat ON reply_mutes(principal,chat) WHERE chat IS NOT NULL;
CREATE UNIQUE INDEX reply_mutes_sender ON reply_mutes(principal,account,sender) WHERE sender IS NOT NULL;
CREATE INDEX reply_mutes_list ON reply_mutes(principal,created_at,id);
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
 'unmute_reply_source','list_reply_dismissals')) NOT VALID;
SET LOCAL lock_timeout=DEFAULT;

-- The existing gateway login gets the new state with the schema, so no deployed state runs the
-- schema-41 code without it (the 034 precedent). gateway_role.sql repeats these for new installs.
DO $$ BEGIN
 IF EXISTS(SELECT FROM pg_roles WHERE rolname='messaging_gateway') THEN
  GRANT DELETE ON reply_dismissals TO messaging_gateway;
  GRANT SELECT,INSERT,UPDATE,DELETE ON reply_snoozes TO messaging_gateway;
  GRANT SELECT,INSERT,DELETE,UPDATE(reason,note,updated_at) ON reply_mutes TO messaging_gateway;
 END IF;
END $$;
