-- Operator-only grant template. Run in the archive database after migrations.
-- Set the password separately through a private operator connection; no secret here.
-- Account isolation (schema 37): install one copy per account under its own role name and
-- bind it with INSERT INTO collector_account_roles(role_name,account). A bound role writes
-- only its account's rows; an unbound role keeps these grants for every account.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='messaging_collector') THEN
        CREATE ROLE messaging_collector LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOREPLICATION NOBYPASSRLS NOINHERIT;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_collector;
GRANT SELECT ON accounts,account_policies,probe_metadata,archive_migrations TO messaging_collector;
GRANT SELECT,INSERT,UPDATE ON collector_leases,collector_chats,provider_tombstones,whatsapp_checkpoints,
    chats,messages,attachments,ingest_receipts,message_revisions,media_jobs,sender_profiles TO messaging_collector;
GRANT SELECT,UPDATE ON source_status,feed_counter TO messaging_collector;
GRANT SELECT,INSERT ON events TO messaging_collector;
-- No access to token hashes, consumers, grants, arbitrary DDL or role administration.
GRANT SELECT,INSERT,UPDATE ON message_metadata,collector_health TO messaging_collector;
GRANT SELECT,INSERT,UPDATE ON message_conflicts TO messaging_collector;
GRANT SELECT,INSERT,UPDATE ON whatsapp_identity_observations TO messaging_collector;
GRANT SELECT,INSERT,UPDATE,DELETE ON media_reservations TO messaging_collector;
-- Outbound dispatch rechecks only policy and public key metadata, never token hashes.
GRANT SELECT(id,accounts,chat_ids,permissions,enabled,scope_version) ON principals TO messaging_collector;
GRANT SELECT(id,principal,revoked,expires_at,permissions,permission_version) ON api_keys TO messaging_collector;
-- Rich payloads are immutable to the collector; it dispatches stored output only.
GRANT SELECT ON outgoing_messages TO messaging_collector;
GRANT UPDATE(status,started_at,finished_at,lease_epoch,provider_message_id,error_code,archive_reference)
    ON outgoing_messages TO messaging_collector;
GRANT INSERT(principal,key_id,operation,outcome,error_code,resource_ids,actor) ON audit_log TO messaging_collector;
-- Provider proofs are immutable; only bounded publication cursors may advance.
GRANT SELECT,INSERT ON message_receipts,receipt_event_jobs,receipt_event_publications TO messaging_collector;
GRANT UPDATE(cursor_id,complete) ON receipt_event_jobs TO messaging_collector;
GRANT UPDATE(receipt,state,published_at) ON receipt_event_publications TO messaging_collector;
GRANT SELECT ON messenger_commands TO messaging_collector;
GRANT UPDATE(status,started_at,finished_at,lease_epoch,result,error_code)
    ON messenger_commands TO messaging_collector;
GRANT SELECT,INSERT,UPDATE ON chat_read_observations TO messaging_collector;
GRANT SELECT ON file_uploads TO messaging_collector;
-- Media admission counts the capacity leases of in-flight cold restores (ANYM-350).
GRANT SELECT ON attachment_cold_copies TO messaging_collector;

-- Verified original publication changes residency, never retained CAS identity.
GRANT EXECUTE ON FUNCTION public.register_resident_original(text,bigint) TO messaging_collector;
-- Schema 37: a restore without ACLs reopens the owner-powered readiness publishers to
-- PUBLIC, which would let a bound collector write another account's events; close them.
DO $$ BEGIN
    IF to_regprocedure('public.publish_processing_event(text[],text,text,text,text,jsonb)') IS NOT NULL THEN
        REVOKE ALL ON FUNCTION public.publish_processing_event(text[],text,text,text,text,jsonb),
            public.publish_file_text_event(text,text,text,text,jsonb),
            public.publish_file_enrichment_event(text,text,text,text,jsonb) FROM PUBLIC;
    END IF;
END $$;
-- Schema 42: WhatsApp owner read receipts (ANYM-275).
GRANT SELECT,INSERT,UPDATE ON owner_message_reads TO messaging_collector;
