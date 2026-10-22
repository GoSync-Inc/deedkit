-- Operator-only cold archiver. Mount only the original CAS read-only, its own
-- SSH key and the cold identity; no provider sessions, R2 credentials or dumps.
DO $$ BEGIN
 IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='messaging_cold_archive') THEN
  CREATE ROLE messaging_cold_archive NOLOGIN;
 END IF;
END $$;
GRANT USAGE ON SCHEMA public TO messaging_cold_archive;
GRANT SELECT ON probe_metadata,attachment_residency TO messaging_cold_archive;
GRANT SELECT,INSERT,UPDATE ON attachment_cold_copies TO messaging_cold_archive;
-- Cold-first rehydration (ANYM-350): read the eviction journal, mark a restored
-- original resident and hand a failed restore to the provider, both only
-- through the SECURITY DEFINER functions of migration 033. The restore
-- service mounts the CAS writable; the archive service keeps it read-only.
GRANT SELECT ON attachment_eviction_journal TO messaging_cold_archive;
-- Restores are admitted against the collectors' capacity reservations.
GRANT SELECT ON media_reservations,file_uploads TO messaging_cold_archive;
GRANT EXECUTE ON FUNCTION public.register_resident_original(text,bigint),
    public.queue_managed_original(text) TO messaging_cold_archive;
-- No direct residency, journal, fence or archive mutation: archiving cannot evict.
