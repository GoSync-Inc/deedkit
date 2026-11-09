-- rollout: manual
-- Risk tier: non-critical (docs/operations/production.md "Migration risk tiers": CREATE INDEX and SET LOCAL only).
-- An original's identity lookup gets an index. register_resident_original (033), which the attachments INSERT
-- trigger calls for every new file, checks `attachments WHERE digest=? AND bytes=?`, and lock_lifecycle_sources
-- (033) reads `attachments WHERE digest=?`; both run while they hold the global 'attachment-residency-v1'
-- advisory lock. The only digest index (013 attachment_occurrences) is partial on `active`, so both read the whole
-- table under that lock (production 2026-11-09: 56,523 rows, 7 ms per lookup, 3 to 9 sessions waiting for it).
-- Additive: no row, column, function, grant or writer changes; older runtimes plan their queries with it. "manual"
-- only because it indexes an existing table (classify); unattended_class still admits it (as 052).
-- Built inside the migration transaction: the SHARE lock holds attachment INSERTs for the build (production's
-- 12.5 MB heap: see the PR's measurement), lock_timeout 2s refuses to queue behind a long writer.
SET LOCAL lock_timeout='2s';
CREATE INDEX IF NOT EXISTS attachment_digest_bytes ON attachments(digest,bytes);
SET LOCAL lock_timeout=DEFAULT;
