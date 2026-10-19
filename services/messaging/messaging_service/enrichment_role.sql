-- Operator-only installation. No credentials/session/message mutation rights.
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_enricher') THEN
 CREATE ROLE messaging_enricher NOLOGIN;
 END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_enricher;
GRANT SELECT ON attachments,messages,attachment_text TO messaging_enricher;
GRANT SELECT ON attachment_residency,attachment_eviction_journal TO messaging_enricher;
GRANT SELECT,UPDATE ON attachment_enrichment TO messaging_enricher;
GRANT SELECT,INSERT,UPDATE ON enrichment_budget TO messaging_enricher;

GRANT SELECT,INSERT,UPDATE ON processing_profiles TO messaging_enricher;

GRANT SELECT,INSERT,UPDATE ON processing_resource_leases TO messaging_enricher;
GRANT SELECT,INSERT,UPDATE ON processing_budget,processing_control TO messaging_enricher;
-- Occurrence-scoped readiness publication; no subscription or key access.
GRANT SELECT ON message_metadata,message_revisions,events TO messaging_enricher;
-- Readiness events only through the fenced model publication (ANYM-254). Reapply this
-- template after the schema-37 code runs: it revokes the earlier direct journal writes.
REVOKE INSERT,UPDATE,DELETE ON events FROM messaging_enricher;
REVOKE ALL ON feed_counter FROM messaging_enricher;
GRANT EXECUTE ON FUNCTION public.publish_file_enrichment_event(text,text,text,text,jsonb) TO messaging_enricher;
