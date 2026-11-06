-- Operator-only original residency. Mount only the original CAS read/write;
-- no sessions, provider keys, Docker socket, dumps or database ownership.
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_file_lifecycle') THEN
  CREATE ROLE messaging_file_lifecycle NOLOGIN;
 END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_file_lifecycle;
GRANT SELECT ON probe_metadata,attachments,messages,media_jobs,download_grants,
 attachment_text,attachment_enrichment,attachment_understanding,attachment_backup_fences
 TO messaging_file_lifecycle;
GRANT EXECUTE ON FUNCTION public.lock_lifecycle_sources(text) TO messaging_file_lifecycle;
GRANT SELECT,INSERT,UPDATE ON attachment_residency,attachment_eviction_journal
 TO messaging_file_lifecycle;
GRANT EXECUTE ON FUNCTION public.touch_original(text,bigint) TO messaging_file_lifecycle;
-- No backup fence administration: an eviction operator cannot release a sender's
-- persistent capture fence to force deletion.
-- Eviction reads cold-copy proof only; it can never mark a copy verified.
GRANT SELECT ON attachment_cold_copies TO messaging_file_lifecycle;
-- ANYM-805 (owner 2026-11-06): which chats are groups or channels that no active subscription names
-- (media_policy.ON_REQUEST_CHATS); these columns only.
GRANT SELECT(id,account,provider_chat,chat_type) ON chats TO messaging_file_lifecycle;
GRANT SELECT(account,provider_chat,peer_kind) ON collector_chats TO messaging_file_lifecycle;
GRANT SELECT(subscription,account,chat) ON subscription_sources TO messaging_file_lifecycle;
GRANT SELECT(id,state,expires_at,closed_at) ON subscriptions TO messaging_file_lifecycle;
