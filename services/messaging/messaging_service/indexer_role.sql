-- Run as owner after schema37. No provider credentials, API keys or archive writes.
-- One transaction that refuses before any change below schema 37 (ANYM-254): with psql
-- autocommit, a failed statement aborts the block, so no revoke commits without its grant.
BEGIN;
DO $$ BEGIN
 IF to_regprocedure('public.publish_processing_event(text[],text,text,text,text,jsonb)') IS NULL THEN
  RAISE EXCEPTION 'schema_37_required';
 END IF;
END $$;
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
-- A restore without ACLs reopens the publishers to PUBLIC; close them before granting one.
REVOKE ALL ON FUNCTION public.publish_processing_event(text[],text,text,text,text,jsonb),
 public.publish_file_text_event(text,text,text,text,jsonb),
 public.publish_file_enrichment_event(text,text,text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.publish_file_text_event(text,text,text,text,jsonb) TO messaging_indexer;
COMMIT;
