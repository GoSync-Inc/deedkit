-- Operator-only messenger deletion purge (ANYM-511). Run as the migration owner after migrations;
-- grant the NOLOGIN role to a dedicated runtime login, or SET ROLE to it (`--role messaging_purge`).
-- No sessions, provider keys, Docker socket, dumps, DDL or database ownership; never DELETE on
-- messages or events, never INSERT on reviews.
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_purge') THEN
  CREATE ROLE messaging_purge NOLOGIN;
 END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_purge;
GRANT SELECT ON probe_metadata,accounts,ingest_receipt_links TO messaging_purge;
GRANT SELECT,UPDATE(purged_at) ON chats TO messaging_purge;
GRANT INSERT ON ingest_receipt_links TO messaging_purge;
GRANT SELECT,INSERT,UPDATE ON message_purges,chat_tombstones TO messaging_purge;
GRANT SELECT,UPDATE(value) ON purge_state TO messaging_purge;
-- Content columns only: identifiers, revisions, positions and digests stay.
GRANT SELECT,UPDATE(body,purged) ON messages TO messaging_purge;
GRANT SELECT,UPDATE(filename) ON attachments TO messaging_purge;
GRANT SELECT,UPDATE(snapshot) ON message_revisions,events TO messaging_purge;
GRANT SELECT,UPDATE(envelope) ON ingest_receipts TO messaging_purge;
GRANT SELECT,UPDATE(descriptor,state,lease_owner,lease_until,lease_epoch,requested_until,request_key_digest)
 ON media_jobs TO messaging_purge;
GRANT SELECT,UPDATE(result_ref) ON reviews TO messaging_purge;
GRANT SELECT,UPDATE(body,source_text,render_artifact) ON outgoing_messages TO messaging_purge;
GRANT SELECT,UPDATE(payload,result) ON messenger_commands TO messaging_purge;
GRANT SELECT,UPDATE(note) ON reply_dismissals,reply_snoozes TO messaging_purge;
-- Derived content, chunks and open download grants are deleted.
GRANT SELECT,DELETE ON message_metadata,attachment_text,attachment_text_chunks,attachment_enrichment,
 attachment_enrichment_chunks,attachment_understanding,download_grants TO messaging_purge;
-- Original deletion follows the eviction journal protocol (file_lifecycle.py:213-220, ANYM-512).
GRANT SELECT,INSERT,UPDATE(state) ON attachment_eviction_journal TO messaging_purge;
GRANT SELECT,UPDATE(state) ON attachment_residency TO messaging_purge;
GRANT INSERT(actor,operation,outcome,resource_ids) ON audit_log TO messaging_purge;
