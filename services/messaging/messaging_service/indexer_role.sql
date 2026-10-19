-- Run as owner after schema37. No provider credentials, API keys or archive writes.
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_indexer') THEN
        CREATE ROLE messaging_indexer NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_indexer;
GRANT SELECT ON attachments,messages TO messaging_indexer;
GRANT SELECT ON attachment_residency,attachment_eviction_journal TO messaging_indexer;
GRANT SELECT,UPDATE ON attachment_text TO messaging_indexer;

GRANT SELECT,INSERT,UPDATE ON processing_resource_leases TO messaging_indexer;
-- Occurrence-scoped readiness publication; no subscription or key access.
GRANT SELECT ON message_metadata,message_revisions,events TO messaging_indexer;
-- Readiness events only through the fenced text publication (ANYM-254). Reapply this
-- template after the schema-37 code runs: it revokes the earlier direct journal writes.
REVOKE INSERT,UPDATE,DELETE ON events FROM messaging_indexer;
REVOKE ALL ON feed_counter FROM messaging_indexer;
GRANT EXECUTE ON FUNCTION public.publish_file_text_event(text,text,text,text,jsonb) TO messaging_indexer;
