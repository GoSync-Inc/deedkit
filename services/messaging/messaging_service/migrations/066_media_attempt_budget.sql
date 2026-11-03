-- rollout: manual
-- ANYM-699: a media original gets 8 attempts instead of 5 (media_retry.py MEDIA_MAX_ATTEMPTS), and an explicit
-- request wakes a job that waits out a long retry backoff. Two functions, both written as CREATE OR REPLACE so the
-- migration is repeatable; no table, column, index, row or role changes.
-- Risk tier: medium (docs/operations/production.md "Migration risk tiers": CREATE OR REPLACE FUNCTION that no index,
-- constraint or trigger uses, and a new SECURITY DEFINER function; the grant on the new function is in
-- gateway_role.sql, an object this change creates). A hand release.
-- 1. queue_managed_original (033) keeps its signature, owner, ACL and body except the attempt cap: 5 -> 8.
-- 2. wake_media_job(attachment_id) sets next_due to now for one queued job whose explicit request window is open.
--    It never touches another state, never a provider cooldown (error_code rate_limited), never attempts, lease,
--    descriptor or source. The gateway role holds only EXECUTE on it, not UPDATE(next_due).
-- Applied migration 033 remains immutable. A schema-65 runtime keeps working against this schema (the old cap of 5
-- is lower than the new one, so it only claims fewer retries); a schema-66 runtime that finds no function skips the
-- wake and keeps today's behaviour.
SET LOCAL lock_timeout='2s';
CREATE OR REPLACE FUNCTION public.queue_managed_original(attachment_id text) RETURNS boolean
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(hashtextextended('attachment-residency-v1',0));
 UPDATE public.media_jobs j SET state='queued'
 FROM public.attachments a,public.messages m,public.attachment_residency r,
      public.attachment_eviction_journal e
 WHERE j.id=attachment_id AND j.id=a.id AND a.message=m.id AND a.active AND NOT m.deleted
  AND j.state='ready' AND j.attempts<8 AND j.requested_until>clock_timestamp()
  AND j.request_key_digest IS NOT NULL AND a.digest=r.digest AND a.bytes=r.bytes
  AND r.state='evicted' AND e.id=r.eviction_id AND e.state='completed'
  AND e.digest=r.digest AND e.bytes=r.bytes;
 RETURN FOUND;
END $$;
REVOKE ALL ON FUNCTION public.queue_managed_original(text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION public.wake_media_job(attachment_id text) RETURNS timestamptz
 LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $$
DECLARE due timestamptz;
BEGIN
 UPDATE public.media_jobs j SET next_due=clock_timestamp()
 WHERE j.id=attachment_id AND j.state='queued' AND j.error_code IS DISTINCT FROM 'rate_limited'
  AND j.next_due>clock_timestamp() AND j.requested_until>clock_timestamp()
 RETURNING j.next_due INTO due;
 RETURN due;
END $$;
REVOKE ALL ON FUNCTION public.wake_media_job(text) FROM PUBLIC;
