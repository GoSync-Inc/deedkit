-- Operator-only installation. No credentials/session/message mutation rights.
-- One transaction that refuses before any change below schema 37 (ANYM-254): with psql
-- autocommit, a failed statement aborts the block, so no revoke commits without its grant.
BEGIN;
DO $$ BEGIN
 IF to_regprocedure('public.publish_processing_event(text[],text,text,text,text,jsonb)') IS NULL THEN
  RAISE EXCEPTION 'schema_37_required';
 END IF;
END $$;
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
-- A restore without ACLs reopens the publishers to PUBLIC; close them before granting one.
REVOKE ALL ON FUNCTION public.publish_processing_event(text[],text,text,text,text,jsonb),
 public.publish_file_text_event(text,text,text,text,jsonb),
 public.publish_file_enrichment_event(text,text,text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.publish_file_enrichment_event(text,text,text,text,jsonb) TO messaging_enricher;
COMMIT;
-- ANYM-805 (owner 2026-11-06): which chats are groups or channels that no active subscription names
-- (media_policy.ON_REQUEST_CHATS), and whether an agent asked for a file; these columns only.
GRANT SELECT(id,account,provider_chat,chat_type) ON chats TO messaging_enricher;
GRANT SELECT(account,provider_chat,peer_kind) ON collector_chats TO messaging_enricher;
GRANT SELECT(subscription,account,chat) ON subscription_sources TO messaging_enricher;
GRANT SELECT(id,state,expires_at,closed_at) ON subscriptions TO messaging_enricher;
GRANT SELECT(id,request_key_digest) ON media_jobs TO messaging_enricher;
-- Schema 77 (ANYM-851): a document's structured (Markdown) version, which this worker discovers, converts on the
-- mini and stores, and its per-account publication; no DELETE (rows go with their attachment_text row).
GRANT SELECT,INSERT,UPDATE ON attachment_structure,structure_profiles TO messaging_enricher;
